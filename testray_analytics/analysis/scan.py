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

import os
from pathlib import Path

from . import ledger as L
from .prepare import _testray_oauth_token, fetch_paginated, testray_target
from .queue import Job, open_queue

# Cadence lives in the environment, like the job-runner's crontab expression.
SCAN_INTERVAL_ENV = "TRIAGE_SCAN_INTERVAL"

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


def scan(cfg: dict, routine_id: int, *, queue_dir: Path,
         catch_up: int = DEFAULT_CATCH_UP,
         window: int = DEFAULT_BUILD_WINDOW,
         dry_run: bool = False) -> dict:
    """One tick. Prints what it found; returns a summary."""
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

    queued = skipped = n_new = n_active = n_norange = 0
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
          + (f", {n_norange} skipped for want of a baseline" if n_norange else ""))
    if dry_run:
        print("\n--dry-run: nothing was written to the queue.")
    return {"targets": len(targets), "queued": queued, "skipped": skipped,
            "new": n_new, "active": n_active, "no_range": n_norange}
