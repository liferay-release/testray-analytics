"""
scan.py — find failures nobody has explained yet, and queue them.

The producer half of the job-runner pattern (see queue.py). One tick:

    1. list recent DONE builds for the routine          (1 request)
    2. for the newest failing build(s), read what is red (1 request each)
    3. drop anything Testray already has a verdict for   (1 request, cached)
    4. for the rest, walk back to a build that lacked it (usually 1 request)
    5. register one job per build pair                   (idempotent)

It never classifies and never spends.

**No watermark, and no local state.** Which builds have been dealt with is not
remembered, it is derived: a signature is "done" when TriageResult holds a
verdict for its clusterKey. That makes a tick self-correcting — a crashed run,
a wiped disk or a re-run of the same build all converge on the same answer,
and there is no cursor to get stuck or skip ahead. The queue markers are the
only thing on disk, and losing them costs one re-queue.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ledger as L
from .config import resolve_path
from .prepare import (_testray_oauth_token, fetch_one_page, fetch_paginated,
                      load_config, testray_target)
from .queue import Job, open_queue, queue_path

# Cadence lives in the environment, like the job-runner's crontab expression.
SCAN_INTERVAL_ENV = "TRIAGE_SCAN_INTERVAL"

# The failure-triggered Jenkins hook (LPD-95845) fires the moment Stable's
# build fails, which is BEFORE Testray finishes importing that build's
# results. Queried right then, the just-failed build fails `importStatus eq
# 'DONE'` and simply does not appear to recent_done_builds() — indistinguishable
# from "no failures here", not "come back in a minute". These give
# await_import() (below) somewhere to wait before scan() ever runs.
IMPORT_POLL_INTERVAL_ENV = "TRIAGE_IMPORT_POLL_INTERVAL"
IMPORT_WAIT_TIMEOUT_ENV = "TRIAGE_IMPORT_WAIT_TIMEOUT"
DEFAULT_IMPORT_POLL_INTERVAL = 60
# Measured live 2026-09-15 on a real trigger: hook fired at 13:40, Testray did
# not even create the Build row until 13:43:59, and did not finish importing
# it until ~14:04 — about 24 minutes end to end. 900s (15 min) would have
# given up before that row ever appeared. 1 hour leaves generous headroom
# above the measured 24 minutes without blocking the Jenkins job
# indefinitely on a Testray outage.
DEFAULT_IMPORT_WAIT_TIMEOUT = 3600

# How many recent builds to consider as baseline candidates. Comfortably past
# the measured worst case (20 builds back) without pulling whole history.
DEFAULT_BUILD_WINDOW = 60

# How many recent FAILING builds to examine per tick. One is enough when the
# scanner keeps up; more lets it catch up after downtime without a cursor.
DEFAULT_CATCH_UP = 3


def recent_done_builds(cfg: dict, routine_id: int,
                       limit: int = DEFAULT_BUILD_WINDOW) -> list[dict]:
    """Newest-first DONE builds for a routine, in BUILD order.

    Ordered by `dueDate`, which is when the build actually ran — not
    `dateCreated`, which is when Testray imported it. The two agree whenever
    import lag is uniform (they match on all 181 prod builds in the September
    window), and disagree the moment a batch import stamps several builds with
    one timestamp: five locally-imported builds came back in reverse build
    order that way.

    Ordering matters here because the baseline exists to give `git log A..B` a
    sane range. A baseline picked in import order can sit *ahead* of its target
    in git history, which yields a reversed or empty diff and an attribution
    against commits that cannot have caused anything.

    `importStatus eq 'DONE'` filters on the picklist key directly — the
    `importStatus/key` spelling is rejected — and the routine id must be quoted
    or the server answers "Incompatible types".
    """
    items = fetch_paginated(
        "/o/c/builds",
        {"filter": (f"r_routineToBuilds_c_routineId eq '{routine_id}' and "
                    f"importStatus eq 'DONE'"),
         "sort": "dueDate:desc"},
        token=_testray_oauth_token(cfg), base_url=cfg["base_url"],
        page_size=min(limit, 200),
    )
    items.sort(key=lambda b: (b.get("dueDate") or ""), reverse=True)
    return items[:limit]


def _import_status(build: dict) -> str | None:
    """`importStatus` comes back as the bare key when it drove an OData
    filter and as `{"key": ..., "name": ...}` from a plain read — same
    picklist field, two shapes depending on which endpoint answered."""
    value = build.get("importStatus")
    return value.get("key") if isinstance(value, dict) else value


def newest_build(tr: dict, routine_id: int) -> dict | None:
    """The single most-recently-run build for a routine, DONE or not.

    Deliberately not recent_done_builds(): that filters on `importStatus eq
    'DONE'` server-side, so a build still PENDING or INPROGRESS is invisible
    to it. This is the one place that has to see it anyway, to know whether
    to keep waiting. One request, one page — not fetch_paginated, which would
    walk the routine's whole build history to answer "what's newest" on every
    poll.
    """
    items = fetch_one_page(
        "/o/c/builds",
        {"filter": f"r_routineToBuilds_c_routineId eq '{routine_id}'",
         "sort": "dueDate:desc", "pageSize": 1},
        token=_testray_oauth_token(tr), base_url=tr["base_url"],
    )
    return items[0] if items else None


def await_import(tr: dict, routine_id: int, *,
                 poll_interval: int = DEFAULT_IMPORT_POLL_INTERVAL,
                 timeout: int = DEFAULT_IMPORT_WAIT_TIMEOUT) -> None:
    """Block until a build newer than the one on file when this started is
    `importStatus` DONE, or give up after `timeout` seconds.

    The build the hook fired for often does not exist as a Build row AT ALL
    yet — observed live 2026-09-15: the Jenkins job fired the hook at 13:40,
    Testray did not create the row until 13:43:59, and did not finish
    importing it until ~14:04. Queried at 13:40, newest_build() returns the
    PREVIOUS build, which is long since DONE. An earlier version of this
    function read that as "nothing to wait for" and returned immediately —
    exactly backwards, since that IS the case with the most waiting left to
    do. So the id seen on the FIRST call is recorded as a baseline and never
    updated; "ready" requires either that same build finishing (the ordinary
    PENDING/INPROGRESS-when-we-arrived case) or a build with a different id
    showing up DONE (the row-not-created-yet case) — never "whatever is
    newest right now happens to say DONE", which is true before AND after the
    row nobody has seen yet appears.

    Never raises and never signals failure either way: giving up just leaves
    scan() unable to see the build this tick, exactly as if this function did
    not exist. The backstop is the next Stable failure's `--catch-up`, not an
    error here — a build that never finishes importing is a Testray problem,
    not a reason to fail an otherwise-healthy triage run.
    """
    baseline = newest_build(tr, routine_id)
    if baseline is None:
        print(f"Routine {routine_id}: no builds yet — nothing to wait for.")
        return

    baseline_id = baseline["id"]
    baseline_status = _import_status(baseline)
    name = baseline.get("name") or baseline_id

    if baseline_status == "DONE":
        print(f"Routine {routine_id}: newest build on file is {baseline_id} "
              f"({name}), already DONE — watching for a newer build to "
              f"appear and finish importing, checking every {poll_interval}s "
              f"(up to {timeout}s)", flush=True)
    else:
        print(f"Routine {routine_id}: build {baseline_id} ({name}) is "
              f"{baseline_status or 'unknown'} — waiting for it, or a newer "
              f"build, to finish importing, checking every {poll_interval}s "
              f"(up to {timeout}s)", flush=True)

    waited = 0
    while waited < timeout:
        time.sleep(poll_interval)
        waited += poll_interval
        current = newest_build(tr, routine_id)
        status = _import_status(current) if current else None
        is_new = current is not None and current["id"] != baseline_id
        if status == "DONE" and (is_new or baseline_status != "DONE"):
            which = f"new build {current['id']}" if is_new else \
                    f"build {current['id']}"
            print(f"  {which} imported after {waited}s", flush=True)
            return
        cur_id = current["id"] if current else baseline_id
        print(f"  still waiting: newest is {cur_id} ({status or 'unknown'}) "
              f"after {waited}s", flush=True)

    print(f"  gave up after {timeout}s — scan will not see a new build this "
          f"tick; the next Stable failure's --catch-up will pick it up",
          flush=True)


# Counter fields on a Build (caseResultFailed/Passed/…) are computed by Testray
# on import. On prod they are populated and are a free way to skip green builds
# without fetching their case results. On the local mirror they are all ZERO
# even for builds that plainly have FAILED rows — loadTestrayData does not
# recompute them. Trusting them blindly there makes the scanner report "nothing
# red" on a routine with real failures, which is the worst kind of wrong: quiet.
def _counters_populated(builds: list[dict]) -> bool:
    """True when Build counter fields look computed rather than absent.

    Keyed on PASSED rather than FAILED: a window can legitimately contain no
    failures, but a window where nothing passed either means the counters were
    never filled in.
    """
    return any(int(b.get("caseResultPassed") or 0) > 0 for b in builds)


def analysed_pairs(cfg: dict, routine_id: int) -> set[tuple[int, int]]:
    """(baseline, target) pairs this routine already has a finished run for.

    This is the only memory the scanner has. The Jenkins job checks out both
    repos fresh on every Stable failure, so the queue's `done/` markers and the
    whole `runs/` tree are gone before the next tick — the TriageRun row in
    Testray is the sole surviving record that a pair was ever analysed.

    Without it, a pair whose clusters are all dropped by the write policy
    (never-ran, pre-existing, flaky, auto, high-confidence FALSE_POSITIVE)
    never gains a TriageResult. `attributions()` therefore cannot see it, every
    scan calls its signatures NEW, and the pipeline re-prepares and RE-PAYS for
    an identical answer on each trigger. Pair 522890829 -> 522894597 was
    classified twice in 100 minutes that way, for one written row both times.

    Keyed on the PAIR, not the target build: the baseline walk gives different
    signatures in one build different baselines, so skipping by target alone
    would strand every group but the first.

    `triageRunStatus eq 'DONE'` filters the picklist key directly — the
    `triageRunStatus/key` spelling is rejected, the same quirk `importStatus`
    has in recent_done_builds. QUEUED request rows are excluded on purpose:
    q.register() already owns "is this pending", and treating a request row as
    an analysis would skip a pair nobody has answered yet.
    """
    from .testray_writer import RUN_ENDPOINT

    try:
        items = fetch_paginated(
            RUN_ENDPOINT,
            {"filter": (f"r_routineToTriageRuns_c_routineId eq '{routine_id}' "
                        f"and triageRunStatus eq 'DONE'")},
            token=_testray_oauth_token(cfg), base_url=cfg["base_url"],
            page_size=200,
        )
    except Exception as e:                                       # noqa: BLE001
        # 404 means the TriageRun Object is not deployed here; anything else is
        # a token or a server fault. Either way the honest answer is "no runs on
        # file". Degrading to that re-analyses — which costs money but is
        # correct — where raising would stop the scanner dead and explain
        # nothing about a red build.
        print(f"  ! could not read TriageRun history "
              f"({type(e).__name__}: {e}) — treating every pair as unanalysed",
              file=sys.stderr)
        return set()

    pairs: set[tuple[int, int]] = set()
    for row in items:
        a = row.get("r_baselineBuildToTriageRuns_c_buildId")
        b = row.get("r_buildToTriageRuns_c_buildId")
        if a and b:
            pairs.add((int(a), int(b)))
    return pairs


def scan(cfg: dict, routine_id: int, *, queue_dir: Path,
         catch_up: int = DEFAULT_CATCH_UP,
         window: int = DEFAULT_BUILD_WINDOW,
         dry_run: bool = False, force: bool = False) -> dict:
    """One tick. Prints what it found; returns a summary.

    `force` re-queues pairs that already have a finished TriageRun. It is the
    only way back to a pair once it has been analysed, since the row in Testray
    is deliberately permanent — see analysed_pairs().
    """
    tr = cfg["testray"]
    q, kind = open_queue(cfg, queue_dir)
    print(f"Testray:  {testray_target(cfg)}")
    print(f"Queue:    " + ("TriageRun rows (visible to the build-list diamond)"
                           if kind == "testray"
                           else f"{queue_dir}  (file markers — TriageRun Object "
                                f"not deployed here, so failures stay invisible "
                                f"to the CX)"))

    builds = recent_done_builds(tr, routine_id, window)
    if not builds:
        print(f"\nRoutine {routine_id}: no DONE builds found.")
        return {"targets": 0, "queued": 0, "skipped": 0, "new": 0, "active": 0}

    ids = [int(b["id"]) for b in builds]
    by_id = {int(b["id"]): b for b in builds}
    index = L.SignatureIndex(L.TestraySource(tr, routine_id))

    if _counters_populated(builds):
        failing = [i for i in ids if int(by_id[i].get("caseResultFailed") or 0) > 0]
        targets = failing[:catch_up]
        print(f"\nRoutine {routine_id}: {len(builds)} recent DONE build(s), "
              f"{len(failing)} with failures; examining {len(targets)}")
    else:
        # Counters unusable — ask the case results themselves. Bounded to the
        # newest few builds, and the index caches them for the baseline walk
        # that follows, so this costs nothing extra once a target is chosen.
        print(f"\nRoutine {routine_id}: {len(builds)} recent DONE build(s); "
              f"Build counters are not populated on this instance, "
              f"reading case results directly")
        targets = []
        for bid in ids[:max(catch_up * 4, 8)]:
            if index.failures(bid).signatures:
                targets.append(bid)
            if len(targets) >= catch_up:
                break
        print(f"  {len(targets)} of the newest builds have failures")

    if not targets:
        print("Nothing red. No work to queue.")
        return {"targets": 0, "queued": 0, "skipped": 0, "new": 0, "active": 0}

    print(f"Verdicts on file: {len(index.attributions())} distinct signature(s)")

    done_pairs: set[tuple[int, int]] = set()
    if kind != "testray":
        # File-queue instances keep their own memory in done/, and an instance
        # without the TriageRun Object would 404 this lookup anyway. Note the
        # done/ dir only survives if the checkout does — which on the Jenkins
        # agent it does not. That is survivable only because the agent runs
        # against prod, where the Object IS deployed and this branch is dead.
        pass
    elif force:
        print("--force: pairs with a finished run will be re-queued.")
    else:
        done_pairs = analysed_pairs(tr, routine_id)
        if done_pairs:
            print(f"Runs on file:     {len(done_pairs)} pair(s) already analysed")

    queued = skipped = n_new = n_active = n_norange = n_done = 0
    # Dry-run has to model the real queue's idempotency, or it reports work a
    # real run would skip: two catch-up builds carrying the same signature
    # converge on one job, and saying "2 queued" would be a lie.
    would_queue: set[str] = set()

    for target in targets:
        preds = ids[ids.index(target) + 1:]
        states = index.classify_build(target, preds)
        name = by_id[target].get("name") or target
        print(f"\n  build {target}  {name}")

        pairs: dict[int, list[str]] = {}
        for st in states:
            if not st.needs_attribution:
                n_active += 1
                print(f"    · {st.cluster_key}  {st.state}  (verdict on file)")
                continue
            n_new += 1
            if st.baseline_build is None:
                n_norange += 1
                print(f"    ? {st.cluster_key}  {st.state}  no baseline within "
                      f"{L.MAX_BASELINE_WALK} builds — not queued")
                continue
            print(f"    + {st.cluster_key}  {st.state}  "
                  f"range {st.baseline_build} -> {st.target_build}"
                  + ("" if st.target_build == target else "  (first appeared there)"))
            pairs.setdefault((st.baseline_build, st.target_build), []).append(
                st.cluster_key)

        # One job per build PAIR: several new signatures in one build share a
        # bundle, a diff and a prompt, so queueing per signature would pay for
        # the same diff several times.
        #
        # The pair is the signature's OWN range, not (baseline, build we happen
        # to be examining). Using the examined build queued the same signature
        # once per catch-up build, each against a wider range than the one where
        # it actually appeared — paying repeatedly for a worse answer.
        for (baseline, first_seen), sigs in sorted(pairs.items()):
            job = Job(routine_id=routine_id, baseline_build=baseline,
                      target_build=first_seen, signatures=sigs)
            # Checked before the queue, not after: register() only knows about
            # rows that are still QUEUED, and submit deletes the request row
            # when it finishes. Without this the pair looks brand new again the
            # moment its own analysis succeeds.
            if (baseline, first_seen) in done_pairs:
                n_done += 1
                print(f"      already analysed {job.name} "
                      f"— skipping ({len(sigs)} signature(s); --force to re-run)")
                continue
            if dry_run:
                if job.name in would_queue:
                    skipped += 1
                    print(f"      already queued {job.name}")
                else:
                    would_queue.add(job.name)
                    queued += 1
                    print(f"      would queue {job.name}  ({len(sigs)} signature(s))")
            elif q.register(job):
                queued += 1
                print(f"      queued {job.name}  ({len(sigs)} signature(s))")
            else:
                skipped += 1
                print(f"      already queued {job.name}")

    print(f"\nSignatures: {n_new} unexplained, {n_active} with a verdict on file")
    print(f"Jobs: {queued} queued"
          + (f", {skipped} already registered" if skipped else "")
          + (f", {n_done} already analysed" if n_done else "")
          + (f", {n_norange} skipped for want of a baseline" if n_norange else ""))
    if dry_run:
        print("\n--dry-run: nothing was written to the queue.")
    elif queued == 0 and skipped == 0 and n_done and targets:
        # Red, but nothing to run: every pair is already analysed. `watch` will
        # find an empty queue and `submit` will never execute, so nothing else
        # in the pipeline can speak for this build. Stable posts on every
        # failure, so answering here is the difference between "already
        # explained, here is the run" and the job's bare "nothing submitted".
        #
        # `skipped` must be zero too: a pair already sitting in the queue is
        # work `watch` is about to do, and announcing "nothing new" in front of
        # the analysis it is running would contradict the next message.
        _post_recurrence(tr, routine_id, targets[0], by_id)

    return {"targets": len(targets), "queued": queued, "skipped": skipped,
            "new": n_new, "active": n_active, "no_range": n_norange,
            "analysed": n_done}


def _post_recurrence(tr: dict, routine_id, build_id, by_id: dict) -> None:
    """Write the "nothing new" Slack message for a tick that queued nothing.

    Never fatal: a scan that queued correctly must not be reported as failed
    because the courtesy message could not be rendered.
    """
    try:
        from . import recurrence, slack_message

        repeats = recurrence.repeats_for_build(
            tr, routine_id=routine_id, build_id=build_id)
        if not repeats:
            print("\nNo recurring signature could be traced — no message "
                  "written.")
            return

        build = by_id.get(build_id) or {}
        meta = {
            "routine_id": routine_id,
            "project_id": (build.get("r_projectToBuilds_c_projectId")
                           or tr.get("project_id")),
            "testray_url": tr.get("ui_url") or tr.get("base_url"),
            "build_id_b": build_id,
            "build_b_name": build.get("name") or str(build_id),
        }

        text = slack_message.render_recurrence(meta, repeats)
        target = resolve_path(None, slack_message.OUT_REL)
        target.parent.mkdir(parents=True, exist_ok=True)
        # The tick, not this step, owns the file: `watch` runs after scan and
        # its own submits append. Appending here too keeps both in one post.
        if os.environ.get("TRIAGE_SLACK_APPEND") == "1" and target.exists():
            existing = target.read_text(encoding="utf-8").rstrip()
            if existing:
                text = (f"{existing}\n\n{slack_message.RUN_SEPARATOR}\n\n"
                        f"{text}")
        target.write_text(text, encoding="utf-8")
        print(f"\nStill failing: {len(repeats)} signature(s) — "
              f"message written to {target}")
    except (Exception, SystemExit) as e:                          # noqa: BLE001
        # SystemExit is listed on purpose: config loading raises it, and a
        # courtesy message must not be able to end a scan that queued
        # correctly.
        print(f"\n  ! could not write the recurrence message: {e}",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _routines(args, cfg: dict) -> list[int]:
    """Which routines to scan: the flag, else config, else refuse.

    Configured rather than discovered. Scanning every routine Testray has would
    queue work for teams who never asked for it, and the cost of a run is real
    — so a routine is opted in by name, in config, by someone who meant it.

    This is where `TriageRoutineSetting.autoTriage` belongs once that Object
    exists on the instance being scanned. It does not on prod (both /o/c/
    endpoints 404 there), which is exactly where Stable lives, so the switch
    lives in config for now.
    """
    if args.routines:
        return args.routines
    configured = ((cfg.get("triage") or {}).get("scan") or {}).get("routines")
    if configured:
        return [int(r) for r in configured]
    print("No routine to scan. Pass --routine ID, or set triage.scan.routines "
          "in config.", file=sys.stderr)
    raise SystemExit(2)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Register unexplained failures as triage jobs. Never "
                    "classifies and never spends.")
    ap.add_argument("--routine", type=int, action="append", dest="routines",
                    metavar="ID",
                    help="routine to scan; repeatable. Defaults to "
                         "triage.scan.routines in config.")
    ap.add_argument("--interval", type=int, default=None, metavar="SECONDS",
                    help="keep scanning every SECONDS instead of exiting after "
                         f"one tick (env {SCAN_INTERVAL_ENV}). A tick is cheap: "
                         "one build list plus a case-result read per failing "
                         "build, all of it cached.")
    ap.add_argument("--once", action="store_true",
                    help="one tick, even if --interval or "
                         f"{SCAN_INTERVAL_ENV} says otherwise — the form to "
                         "call from cron or a Jenkins job.")
    ap.add_argument("--catch-up", type=int, default=DEFAULT_CATCH_UP,
                    metavar="N",
                    help=f"failing builds to examine per tick (default "
                         f"{DEFAULT_CATCH_UP}). Raise it to work through a "
                         f"backlog after downtime.")
    ap.add_argument("--window", type=int, default=DEFAULT_BUILD_WINDOW,
                    metavar="N",
                    help=f"how many recent builds are baseline candidates "
                         f"(default {DEFAULT_BUILD_WINDOW}).")
    ap.add_argument("--queue-dir", default=None, metavar="DIR",
                    help="marker directory for the file queue (default: "
                         "queue.path in config). Ignored where the TriageRun "
                         "Object is deployed — that queue is rows, not files.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be queued and write nothing.")
    ap.add_argument("--wait-for-import", action="store_true",
                    help="before each routine's tick, poll its newest build "
                         "until Testray reports importStatus DONE (env "
                         "TRIAGE_WAIT_FOR_IMPORT=1). For the failure-triggered "
                         "Jenkins hook, which fires before Testray has "
                         "finished importing the build that failed.")
    ap.add_argument("--import-poll-interval", type=int, default=None,
                    metavar="SECONDS",
                    help="how often to re-check while waiting (default "
                         f"{DEFAULT_IMPORT_POLL_INTERVAL}, env "
                         f"{IMPORT_POLL_INTERVAL_ENV})")
    ap.add_argument("--import-wait-timeout", type=int, default=None,
                    metavar="SECONDS",
                    help="give up waiting after this long and scan anyway "
                         f"(default {DEFAULT_IMPORT_WAIT_TIMEOUT}, env "
                         f"{IMPORT_WAIT_TIMEOUT_ENV})")
    ap.add_argument("--force", action="store_true",
                    help="re-queue a pair even though a finished TriageRun "
                         "exists for it. Costs a full classify: the run is "
                         "prepared fresh, so nothing is reused. Use it after a "
                         "prompt or rubric change, not to retry a bad tick.")
    args = ap.parse_args()

    cfg = load_config()
    routines = _routines(args, cfg)
    queue_dir = Path(args.queue_dir) if args.queue_dir else queue_path(cfg)

    interval = args.interval
    if interval is None and os.environ.get(SCAN_INTERVAL_ENV):
        interval = int(os.environ[SCAN_INTERVAL_ENV])
    if args.once:
        interval = None

    wait_for_import = (args.wait_for_import
                       or os.environ.get("TRIAGE_WAIT_FOR_IMPORT") == "1")
    import_poll_interval = (args.import_poll_interval
                            or int(os.environ.get(IMPORT_POLL_INTERVAL_ENV,
                                                   DEFAULT_IMPORT_POLL_INTERVAL)))
    import_wait_timeout = (args.import_wait_timeout
                           or int(os.environ.get(IMPORT_WAIT_TIMEOUT_ENV,
                                                  DEFAULT_IMPORT_WAIT_TIMEOUT)))

    while True:
        if interval:
            print(f"\n=== {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC ===")

        for routine_id in routines:
            if wait_for_import:
                try:
                    await_import(cfg["testray"], routine_id,
                                 poll_interval=import_poll_interval,
                                 timeout=import_wait_timeout)
                except Exception as e:                           # noqa: BLE001
                    # A broken wait must degrade to "did not wait", not to
                    # "did not scan" — this routine's own try/except below is
                    # what actually decides whether scan() runs, and a bug
                    # here should never be the reason a real scan gets
                    # skipped for the tick.
                    print(f"! routine {routine_id} await_import failed: "
                          f"{type(e).__name__}: {e} — scanning anyway",
                          file=sys.stderr)

            try:
                scan(cfg, routine_id, queue_dir=queue_dir,
                     catch_up=args.catch_up, window=args.window,
                     dry_run=args.dry_run, force=args.force)
            except Exception as e:                               # noqa: BLE001
                # One routine's bad tick must not take the others down, nor end
                # a long-running scan: the usual cause is transient (a token, a
                # Testray restart, a 500 on one build's case results).
                print(f"! routine {routine_id} scan failed: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)

        if not interval:
            return

        time.sleep(interval)


if __name__ == "__main__":
    main()
