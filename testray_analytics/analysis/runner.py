"""
runner.py — drain the triage queue, whoever filled it.

Two producers, one drainer. `Run Triage` in Testray's build list writes a
`TriageRun` with `triageRunStatus: QUEUED` and three foreign keys, and nothing
more (see ARCHITECTURE "The queue contract"); `scan.py` registers the same work
for a routine's unexplained failures on a tick. This is what turns either
request into a run: claim it, execute the pipeline, and let `submit` write the
real result row.

**Which queue is not a preference, it is a property of the instance.** The
TriageRun Object exists only where the analytics client extension is deployed
— prod answers 404 for it today, and prod is where Stable (routine 79529)
lives. So the scanner falls back to marker files there, and a drainer that only
knew about rows would leave every auto-queued job untouched. Both backends are
drained through the same pipeline; only the bookkeeping differs:

    TriageRun rows   QUEUED -> RUNNING -> deleted / FAILED / INCONCLUSIVE
                     (visible to the build-list diamond)
    file markers     present -> absent
                     (invisible to Testray; the next scan re-queues if the
                     failure is still unexplained)

The pipeline itself is `scripts/triage_pipeline.sh`, not three subprocess calls
from here. That script is the single definition of the sequence — Jenkins runs
it, a human runs it, and this runner runs it — so the order of steps and the
per-step log layout exist in exactly one place.

    testray-analysis watch                # prepare only, then hand over
    testray-analysis watch --classify     # prepare -> classify -> submit
    testray-analysis watch --once         # drain what is queued now and exit

**`--classify` is opt-in on purpose.** Classification is the expensive step —
minutes of wall clock and real model usage — and a click in a browser is a very
small gesture to attach that to silently. Without the flag the runner does the
slow, free part (REST reads, the git diff, hunk filtering) and prints the two
commands that finish the job.

Row-queue state transitions, which exist so the build-list diamond never
lies (the file queue has nowhere to record any of this — see _drain_files):

    QUEUED  --claim-->  RUNNING  --ok-->    (row deleted; submit wrote the real one)
                                 --fail-->  FAILED + errorMessage

The queued row is deleted rather than completed on success because `submit`
writes its own row keyed by the *bundle* id, and leaving both would give one
build two runs — with the column free to show either.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

from .config import cli_command
from .prepare import load_config, testray_target
from .queue import FileQueue, TestrayQueue, queue_path
from .testray_writer import RUN_ENDPOINT, _Session, _run_erc_path

# The pipeline script echoes this after prepare, precisely so a caller can learn
# what was built without scraping prepare's own output out of a log file.
_BUNDLE_RE = re.compile(r"^BUNDLE=(\S+)\s*$", re.M)

# scripts/ sits beside the package, two levels up from this module.
_PIPELINE = (Path(__file__).resolve().parents[2] / "scripts"
             / "triage_pipeline.sh")

POLL_SECONDS = 10

# triage_pipeline.sh exits 3 when it completed cleanly but produced no verdicts
# for a build that has failures. Distinct from 0 and from a step failure,
# because under unattended operation "explained nothing" must not look like
# "explained everything".
_RC_NO_VERDICTS = 3

# Status written for that case. Falls back to FAILED when the Object's picklist
# does not have the key yet: a wrong-but-visible state beats a green diamond
# over an unexplained red build.
STATUS_INCONCLUSIVE = "INCONCLUSIVE"


class NoVerdicts(Exception):
    """The pipeline ran clean and explained nothing."""


def _queued(session: _Session) -> list[dict]:
    """QUEUED runs, oldest first so a backlog drains in request order."""
    body = session.request(
        "GET",
        f"{RUN_ENDPOINT}?pageSize=50&filter="
        # A picklist field filters on its key directly; `triageRunStatus/key`
        # is rejected with "Expected token 'QualifiedName' not found".
        + "triageRunStatus%20eq%20%27QUEUED%27",
    )
    return body.get("items") or []


def _set_status(session: _Session, erc: str, status: str,
                error: str | None = None) -> None:
    payload: dict = {"triageRunStatus": {"key": status}}
    if error:
        # Trimmed: the field is for a human reading the diamond's tooltip, not
        # for a full traceback, and an over-long value risks the write itself.
        payload["errorMessage"] = error[:900]
    session.request("PATCH", _run_erc_path(erc), payload)


def _delete(session: _Session, erc: str) -> None:
    try:
        session.request("DELETE", _run_erc_path(erc))
    except urllib.error.HTTPError as e:
        # Already gone is the outcome we wanted.
        if e.code not in (404, 410):
            raise


def _run(cmd: list[str], label: str) -> str:
    """Run a pipeline step, streaming it, and return its stdout.

    Streamed because `prepare` and `classify` take minutes and a silent
    terminal is indistinguishable from a hang.

    On failure the tail of the output goes into the exception, because that
    string ends up in `errorMessage` and therefore in the red diamond's
    tooltip. "prepare exited 1" tells a reader nothing; the last lines usually
    name the actual cause — most often a build whose commit is not in the local
    checkout, which is the off-origin case for PR and Heavy-dev routines.
    """
    print(f"\n  $ {' '.join(cmd)}", flush=True)
    lines: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write("  | " + line)
        sys.stdout.flush()
        lines.append(line)
    rc = proc.wait()
    if rc == _RC_NO_VERDICTS:
        # Not a failure: every step ran clean, there was simply nothing to
        # explain. Raised as its own type so the caller can mark the run
        # INCONCLUSIVE rather than deleting the row as a success.
        raise NoVerdicts("".join(lines))
    if rc != 0:
        tail = " / ".join(x.strip() for x in lines[-5:] if x.strip())
        raise RuntimeError(f"{label} exited {rc}: {tail}")
    return "".join(lines)


def _row_pair(run: dict) -> tuple[int, int]:
    """The build pair a queued row names, or a readable failure."""
    baseline = run.get("r_baselineBuildToTriageRuns_c_buildId")
    target = run.get("r_buildToTriageRuns_c_buildId")
    if not (baseline and target):
        raise RuntimeError(
            f"queued row {run.get('externalReferenceCode')} is missing a build "
            f"FK (baseline={baseline!r}, target={target!r})")
    return int(baseline), int(target)


def _pipeline(baseline: int, target: int, args) -> str:
    """Run the pipeline script for one build pair. Returns the bundle path.

    Takes ids rather than a queue entry so both backends share it: a marker
    file and a TriageRun row disagree about everything except the pair, which
    is the only thing the pipeline needs.
    """
    if not _PIPELINE.is_file():
        raise RuntimeError(f"pipeline script not found at {_PIPELINE}")

    cmd = [str(_PIPELINE),
           "--baseline-build-id", str(baseline),
           "--target-build-id", str(target),
           "--mode", args.mode,
           "--out", args.out,
           "--engine", args.engine]
    if not args.classify:
        cmd.append("--no-classify")

    out = _run(cmd, "pipeline")

    match = _BUNDLE_RE.search(out)
    if not match:
        # The script exited 0, so prepare worked; not knowing what it built is
        # still fatal, because nothing downstream can proceed without it.
        raise RuntimeError("pipeline reported no BUNDLE= line")

    return match.group(1)


def _drain_files(q: FileQueue, args) -> int:
    """Run every pending marker, oldest first. Returns how many were claimed.

    How a job ends decides what is recorded, and the distinction is the whole
    reason `complete` exists. An answer — verdicts, or a clean run that
    explained nothing — is recorded as done, because this backend is only
    chosen on instances with no TriageResult Object, so nothing else remembers
    the pair was analysed and the next scan would queue it again. A failure is
    merely released: the pair stays eligible, so a token, a 500 or an unfetched
    commit gets another attempt rather than being written off.
    """
    pending = q.pending()

    for job in pending:
        print(f"\n=== claiming {job.name} "
              f"(build {job.baseline_build} -> {job.target_build}) ===")
        if job.signatures:
            print(f"  queued for {len(job.signatures)} unexplained "
                  f"signature(s): {', '.join(job.signatures[:3])}"
                  + (" …" if len(job.signatures) > 3 else ""))

        try:
            bundle = _pipeline(job.baseline_build, job.target_build, args)
        except NoVerdicts:
            # An answer, not a failure: re-running would reach it again.
            print(f"  ! {job.name}: ran clean but produced no verdicts — every "
                  f"failure was auto-classified or excluded. Needs a human.",
                  file=sys.stderr)
            q.complete(job)
            continue
        except Exception as e:                                   # noqa: BLE001
            print(f"  ! {job.name} failed: {e}", file=sys.stderr)
            q.release(job)
            continue

        q.complete(job)

        if args.classify:
            print(f"\n  Done: {bundle}")
        else:
            print(f"\n  Prepared: {bundle}")
            print("  Not classified (add --classify). Finish it with:")
            print(f"    {cli_command()} classify {bundle}")
            print(f"    {cli_command()} submit   {bundle}")

    return len(pending)


def _drain_rows(session: _Session, args) -> int:
    """Claim and run every QUEUED TriageRun row. Returns how many were seen."""
    try:
        queued = _queued(session)
    except Exception as e:                                       # noqa: BLE001
        # A poll failure is transient (token, restart, network). Report and
        # keep waiting rather than dying and leaving the queue unattended.
        print(f"  ! poll failed: {e}", file=sys.stderr)
        return 0

    for run in queued:
        erc = run.get("externalReferenceCode") or ""
        print(f"\n=== claiming {erc} ===")
        try:
            baseline, target = _row_pair(run)
        except RuntimeError as e:
            print(f"  ! {e}", file=sys.stderr)
            try:
                _set_status(session, erc, "FAILED", str(e))
            except Exception as inner:                           # noqa: BLE001
                print(f"  ! also could not mark it FAILED: {inner}",
                      file=sys.stderr)
            continue

        print(f"  build {baseline} -> {target}")
        try:
            _set_status(session, erc, "RUNNING")
        except Exception as e:                                   # noqa: BLE001
            print(f"  ! could not claim {erc}: {e}", file=sys.stderr)
            continue

        try:
            bundle = _pipeline(baseline, target, args)
        except NoVerdicts:
            # The row stays, carrying a state a human can see. Deleting it
            # would hand the build a green diamond for an analysis that
            # concluded nothing.
            msg = ("Ran clean but produced no verdicts — every failure was "
                   "auto-classified or excluded. Needs a human.")
            print(f"  ! {erc}: {msg}", file=sys.stderr)
            try:
                _set_status(session, erc, STATUS_INCONCLUSIVE, msg)
            except Exception:                                    # noqa: BLE001
                # Picklist may not carry the key yet (it is a schema change
                # on the Testray Object). Visible-and-wrong beats silent.
                try:
                    _set_status(session, erc, "FAILED", msg)
                except Exception as inner:                       # noqa: BLE001
                    print(f"  ! also could not mark it: {inner}",
                          file=sys.stderr)
            continue
        except Exception as e:                                   # noqa: BLE001
            print(f"  ! {erc} failed: {e}", file=sys.stderr)
            try:
                _set_status(session, erc, "FAILED", str(e))
            except Exception as inner:                           # noqa: BLE001
                print(f"  ! also could not mark it FAILED: {inner}",
                      file=sys.stderr)
            continue

        if args.classify:
            # submit wrote the real run row, keyed by the bundle id; this
            # one was only the request, and leaving both would give the
            # build two runs with the column free to show either.
            _delete(session, erc)
            print(f"\n  Done: {bundle}")
        else:
            print(f"\n  Prepared: {bundle}")
            print("  Not classified (add --classify). The row stays "
                  "RUNNING, so the diamond reads 'in progress', and this "
                  "runner will not pick it up again — finish it with:")
            print(f"    {cli_command()} classify {bundle}")
            print(f"    {cli_command()} submit   {bundle}")

    return len(queued)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Drain the triage queue — scanner jobs and Run Triage "
                    "requests — through the pipeline.")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS,
                    help=f"seconds between polls (default {POLL_SECONDS})")
    ap.add_argument("--once", action="store_true",
                    help="drain what is queued now, then exit")
    ap.add_argument("--classify", action="store_true",
                    help="also classify and submit. Off by default: this is "
                         "the step that costs minutes and model usage, and a "
                         "click in a browser should not trigger it silently.")
    ap.add_argument("--engine", default="claude-code",
                    choices=("claude-code", "api"),
                    help="passed to classify (default claude-code)")
    ap.add_argument("--mode", default="by-cluster",
                    choices=("by-cluster", "per-test", "by-subtask"),
                    help="passed to prepare (default by-cluster)")
    ap.add_argument("--out", default="runs",
                    help="where prepare writes bundles (default ./runs)")
    ap.add_argument("--queue-dir", default=None, metavar="DIR",
                    help="marker directory to drain (default: queue.path in "
                         "config, or $TRIAGE_QUEUE). Must match the scanner's "
                         "--queue-dir, or the drainer watches an empty one.")
    ap.add_argument("--queue", default="auto",
                    choices=("auto", "testray", "file"),
                    help="which queue to drain. auto (default) uses TriageRun "
                         "rows where the Object is deployed and marker files "
                         "where it is not — prod has no Object, so scanner "
                         "jobs land in files there.")
    args = ap.parse_args()

    cfg = load_config()

    # Probed, not assumed: the same command has to work against a local
    # instance carrying the Objects and against prod, which does not.
    # `TestrayQueue.available` is the same probe `open_queue` uses, so the
    # scanner and the drainer cannot disagree about which queue is live.
    kind = args.queue
    probed = kind == "auto"
    if probed:
        kind = "testray" if TestrayQueue.available(cfg["testray"]) else "file"

    # Only the backend actually drained gets built: constructing a session
    # mints an OAuth token, and the marker path needs no session at all.
    marker_dir = Path(args.queue_dir) if args.queue_dir else queue_path(cfg)
    session = _Session(cfg["testray"]) if kind == "testray" else None
    q = None if kind == "testray" else FileQueue(marker_dir)

    print(f"Watching {testray_target(cfg)} for triage work")
    if kind == "testray":
        print("  queue:    QUEUED TriageRun rows (the build-list diamond "
              "shows their state)")
    else:
        # Only say WHY when we actually asked. Forced with --queue file, the
        # Object may well be deployed and simply not in use.
        print(f"  queue:    {marker_dir} — marker files"
              + (", because the TriageRun Object is not deployed here"
                 if probed else " (requested with --queue file)")
              + ", so runs are invisible to Testray")
    print(f"  classify: {'yes' if args.classify else 'no (prepare only)'}"
          f"   poll: {args.interval}s")

    while True:
        seen = (_drain_rows(session, args) if kind == "testray"
                else _drain_files(q, args))

        if args.once:
            if not seen:
                print("Nothing queued.")
            return

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
