"""
queue.py — pending attribution work, as markers.

Modelled on liferay-docker's `templates/job-runner`, which is how Liferay
already schedules recurring work:

    cron ──▶ register_job.sh <name>    touch /opt/liferay/job-queue/<name>
                                       ("Skipping, it is already registered")
    run_jobs loop  ◀──                 poll, pop newest, run it, one at a time

Three properties are borrowed verbatim, and they are the whole reason this
module exists rather than the scanner just calling the pipeline directly:

  1. **Cron never runs the work.** It enqueues a marker; something else drains.
     A 30-minute tick and a 10-minute job stop being coupled.
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
    """Marker-file queue. One JSON file per pending job."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def register(self, job: Job) -> bool:
        """Enqueue unless already pending. Returns True when it was added.

        Written to a temp file and renamed, so a drainer polling the directory
        can never observe a half-written job — `os.replace` is atomic within a
        filesystem.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.dir / f"{job.name}.json"
        if target.exists():
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
        """Drop a job's marker. Called after it has been run, successfully or
        not — a job that keeps failing should not spin the drainer forever;
        the next scan re-queues it if the signature is still unattributed."""
        (self.dir / f"{job.name}.json").unlink(missing_ok=True)

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
