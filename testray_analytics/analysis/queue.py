"""
queue.py — pending attribution work, as markers.

Modelled on liferay-docker's `templates/job-runner`, which is how Liferay
already schedules recurring work:

    cron ──▶ register_job.sh <name>    touch /opt/liferay/job-queue/<name>
                                       ("Skipping, it is already registered")
    run_jobs loop  ◀──                 poll, pop newest, run it, one at a time

Three properties are borrowed verbatim, and they are the whole reason this
module exists rather than the scanner just calling the pipeline directly:

  1. **The trigger never runs the work.** It enqueues a marker; something else
     drains. A hook that fires on a failing build and a job that takes ten
     minutes stop being coupled.
  2. **Enqueue is idempotent.** Re-registering an already-pending job is a
     no-op, so a tick landing while the previous job is still running cannot
     stack duplicates up.
  3. **The drainer is serial.** One job at a time is the lock — no flock, no
     PID file. It is also what makes the ledger single-writer by construction.

Two backends, because the Testray Objects are not deployed everywhere:
`/o/c/triageruns` returns 404 on prod today, so a Testray-only queue would mean
no prod scanning at all. The file queue works anywhere there is a disk, and is
the same shape as the job-runner's marker directory.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import resolve_path

# Statuses a blocking row can carry that mean "the drainer is going to get to
# this". Anything else — FAILED above all — means nobody will, and a scan that
# treats the two the same reports a dead pair as work in progress. That is not
# hypothetical: pair 79529-527680167-527702480 failed in `prepare` on
# 2026-09-21, kept its FAILED row, and every tick after it said "already
# queued" while `watch` (which claims QUEUED only) said "Nothing queued".
# Neither would ever touch it again, and two red Stable builds sat unanalysed
# and unannounced behind it.
LIVE_STATUSES = frozenset({"QUEUED", "RUNNING"})

# ...and DONE, which needs no action for the opposite reason: it is the answer.
# On an instance with no verdict store the file queue's `done` directory is the
# ONLY memory that a pair was analysed, so counting it as a dead row would
# re-report every finished pair as needing a human.
SETTLED_STATUSES = LIVE_STATUSES | {"DONE"}

QUEUE_ENV          = "TRIAGE_QUEUE"
DEFAULT_QUEUE_PATH = "state/queue"


def queue_path(cfg: dict | None = None) -> Path:
    """Resolved queue directory. Same precedence as the ledger: env, then
    config, then default; relative anchors to the project root."""
    env = os.environ.get(QUEUE_ENV)
    if env:
        return resolve_path(env, DEFAULT_QUEUE_PATH)
    configured = ((cfg or {}).get("queue") or {}).get("path")
    return resolve_path(configured, DEFAULT_QUEUE_PATH)


@dataclass(frozen=True)
class Job:
    """One attribution to run: a build pair plus why it was queued.

    `signatures` is carried for traceability only — the pipeline rediscovers
    membership from the bundle. It is what lets a human read the queue and see
    *which* new failure caused this job to exist.
    """
    routine_id:     int
    baseline_build: int
    target_build:   int
    signatures:     list[str]
    reason:         str = "new-signature"

    @property
    def name(self) -> str:
        """Deterministic marker name — this is what makes enqueue idempotent.

        Keyed on the build PAIR, not on the signature: several new signatures in
        one build share a single attribution run, because they share a bundle
        and a prompt. Keying on the signature would queue five runs for one
        build and pay five times for the same diff.
        """
        return f"{self.routine_id}-{self.baseline_build}-{self.target_build}"

    def to_json(self) -> dict:
        return {
            "routine_id":     self.routine_id,
            "baseline_build": self.baseline_build,
            "target_build":   self.target_build,
            "signatures":     sorted(self.signatures),
            "reason":         self.reason,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Job":
        return cls(
            routine_id=int(d["routine_id"]),
            baseline_build=int(d["baseline_build"]),
            target_build=int(d["target_build"]),
            signatures=list(d.get("signatures") or []),
            reason=str(d.get("reason") or "new-signature"),
        )


class FileQueue:
    """Marker-file queue. One JSON file per pending job, plus a `done/` record.

    `done/` exists because of what the file backend implies about the instance:
    it is chosen when the TriageRun Object is absent, and that same absence
    means `/o/c/triageresults` is absent too, so no verdict is ever written
    where the ledger could read it back. "Has this been dealt with?" therefore
    cannot be derived from Testray on this instance, and without a local answer
    the scanner re-queues the same build pair on every tick forever — with
    `--classify`, that is unbounded spend on one analysis.

    So a completed pair is recorded, and `register` declines it. This is the
    one piece of state the design would rather not keep (ARCHITECTURE: no
    watermark, nothing to get stuck), and it is scoped as tightly as possible:
    it answers only "this exact pair has already been analysed here", it is
    per-machine, and deleting the directory costs one re-analysis per pair.
    Where the Objects exist, Testray remains the authority and this is
    belt-and-braces.
    """

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.done_dir = self.dir / "done"

    def register(self, job: Job) -> bool:
        """Enqueue unless already pending or already done. True when added.

        Written to a temp file and renamed, so a drainer polling the directory
        can never observe a half-written job — `os.replace` is atomic within a
        filesystem.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.dir / f"{job.name}.json"
        if target.exists() or (self.done_dir / f"{job.name}.json").exists():
            return False
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(job.to_json(), fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True

    def blocking_status(self, job: Job) -> str:
        """Why `register` would refuse this job, or "" if it would not.

        This backend cannot deadlock the way the Testray one can: `release`
        deletes the marker, so a failed job leaves nothing behind and the pair
        is eligible on the next tick. The method exists so the scanner can ask
        both backends the same question.
        """
        if (self.dir / f"{job.name}.json").exists():
            return "QUEUED"
        if (self.done_dir / f"{job.name}.json").exists():
            return "DONE"
        return ""

    def requeue(self, job: Job) -> bool:
        """Make a settled job eligible again. True when something changed.

        Only reachable for a DONE job here: this backend drops a failed job's
        marker outright, so it has no dead state to recover from.
        """
        done = self.done_dir / f"{job.name}.json"
        if not done.exists():
            return False
        done.unlink()
        return self.register(job)

    def pending(self) -> list[Job]:
        """Queued jobs, oldest first — a backlog drains in the order it arrived."""
        if not self.dir.exists():
            return []
        out = []
        for p in sorted(self.dir.glob("*.json"), key=lambda x: x.stat().st_mtime):
            try:
                out.append(Job.from_json(json.loads(p.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue          # a corrupt marker must not stall the queue
        return out

    def release(self, job: Job) -> None:
        """Drop a job's marker without recording it as done.

        For a job that ended badly. It leaves the pair eligible again, so a
        transient failure — a token, a 500 on one build's case results, a
        commit not yet fetched — gets another attempt on a later tick instead
        of being written off. It does not spin: the marker is gone, so the
        retry happens only if a scan decides the failure is still unexplained.
        """
        (self.dir / f"{job.name}.json").unlink(missing_ok=True)

    def complete(self, job: Job) -> None:
        """Record a pair as analysed, and drop its pending marker.

        Called when the pipeline produced an answer — including "ran clean and
        explained nothing", which is an answer and would be the same answer
        next time. Not called when it failed; see `release`.
        """
        self.done_dir.mkdir(parents=True, exist_ok=True)
        pending = self.dir / f"{job.name}.json"
        record = self.done_dir / f"{job.name}.json"
        if pending.exists():
            os.replace(pending, record)
        else:
            # Nothing pending to move — a hand-run pair, or a marker someone
            # cleaned up mid-run. The record is the point, so write it anyway.
            record.write_text(json.dumps(job.to_json(), indent=2,
                                         sort_keys=True), encoding="utf-8")

    def completed(self) -> list[Job]:
        """Pairs already analysed on this machine, oldest first."""
        if not self.done_dir.exists():
            return []
        out = []
        for f in sorted(self.done_dir.glob("*.json"),
                        key=lambda x: x.stat().st_mtime):
            try:
                out.append(Job.from_json(json.loads(f.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return out

    def __len__(self) -> int:
        return len(list(self.dir.glob("*.json"))) if self.dir.exists() else 0


# ---------------------------------------------------------------------------
# Testray-backed queue — the one the CX diamond can see
# ---------------------------------------------------------------------------

class TestrayQueue:
    """Enqueue as a QUEUED `TriageRun`, the row `runner.py` already drains.

    Preferred over the file queue wherever the Object exists, because the row is
    what the build-list diamond renders: QUEUED, then RUNNING while the drainer
    works, then deleted on success (submit writes its own row keyed by the
    bundle) or FAILED with an errorMessage — the red diamond. A file marker is
    invisible to Testray, so a failure would be silent.

    Idempotency comes from the externalReferenceCode rather than a pre-flight
    query: the ERC is the job name, so registering the same build pair twice is
    an upsert onto the same row. That is the same guarantee `register_job.sh`
    gets from a marker filename, without the race between checking and creating.
    """

    def __init__(self, cfg: dict):
        from .testray_writer import _Session
        self.session = _Session(cfg)

    @staticmethod
    def available(cfg: dict) -> bool:
        """Is the TriageRun Object deployed on this instance?

        Prod answers 404 today, which is why the file queue exists. Probed
        rather than configured: a deployment can land without anyone updating
        a config file, and the scanner should start using it when it does.
        """
        from .testray_writer import RUN_ENDPOINT, _Session
        try:
            _Session(cfg).request("GET", f"{RUN_ENDPOINT}?pageSize=1")
            return True
        except Exception:                                        # noqa: BLE001
            return False

    def blocking_status(self, job: Job) -> str:
        """The status of the row that would make `register` refuse, or "".

        Read separately rather than returned from `register` so the bool
        contract the file queue and its tests share stays intact. It costs one
        GET, and only for a pair that was actually refused.
        """
        from .testray_writer import _run_erc_path
        import urllib.error
        try:
            body = self.session.request("GET", _run_erc_path(job.name)) or {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ""
            raise
        status = (body.get("triageRunStatus") or {})
        return str(status.get("key") or "").strip().upper() or "UNKNOWN"

    def register(self, job: Job) -> bool:
        """Upsert a QUEUED row. Returns False when one is already pending.

        A row that exists in any state means this pair has been dealt with or is
        being dealt with; re-queueing it would either duplicate work or reset a
        RUNNING job's status out from under the drainer.
        """
        from .testray_writer import RUN_ENDPOINT, _run_erc_path
        import urllib.error
        try:
            self.session.request("GET", _run_erc_path(job.name))
            return False
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        self.session.request("POST", RUN_ENDPOINT, {
            "externalReferenceCode": job.name,
            "triageRunStatus": {"key": "QUEUED"},
            "r_baselineBuildToTriageRuns_c_buildId": job.baseline_build,
            "r_buildToTriageRuns_c_buildId": job.target_build,
        })
        return True

    def requeue(self, job: Job) -> bool:
        """Put an existing row back to QUEUED so the drainer claims it again.

        The only way out of a dead row. `register` refuses whenever a row
        exists — deliberately, since re-queueing a RUNNING pair would reset a
        live job's status under the drainer — and `watch` claims QUEUED only,
        so a FAILED row is a state nothing in the pipeline can leave. It has to
        be an explicit act, which is why this is `--force` and not a retry.

        `errorMessage` is cleared with it: leaving the last failure's text on a
        row that is about to run again makes the diamond's tooltip describe a
        run that is no longer happening.
        """
        from .testray_writer import _run_erc_path
        self.session.request("PATCH", _run_erc_path(job.name), {
            "triageRunStatus": {"key": "QUEUED"},
            "errorMessage": "",
        })
        return True

    def pending(self) -> list[Job]:
        """QUEUED rows, as Jobs. The drainer is `runner.py`, which reads the
        rows directly; this exists so the scanner can report queue depth."""
        from .testray_writer import RUN_ENDPOINT
        body = self.session.request(
            "GET", f"{RUN_ENDPOINT}?pageSize=100&filter="
                   "triageRunStatus%20eq%20%27QUEUED%27")
        out = []
        for it in body.get("items") or []:
            base = it.get("r_baselineBuildToTriageRuns_c_buildId")
            tgt = it.get("r_buildToTriageRuns_c_buildId")
            if base and tgt:
                out.append(Job(routine_id=0, baseline_build=int(base),
                               target_build=int(tgt), signatures=[]))
        return out

    def __len__(self) -> int:
        return len(self.pending())


def open_queue(cfg: dict, queue_dir):
    """The Testray queue when the Object is deployed, else the file queue.

    Prefer Testray: only that one is visible to the CX build-list diamond, so
    only that one can show a failure. The file queue keeps the scanner working
    on instances where the Object has not been deployed — prod, today — at the
    cost of failures being invisible until it is.
    """
    tr = cfg.get("testray") or cfg
    if TestrayQueue.available(tr):
        return TestrayQueue(tr), "testray"
    return FileQueue(queue_dir), "file"
