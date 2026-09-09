"""
Tests for the scanner — the producer half of the auto-queue.

The rule it implements, stated in one sentence: a build is queued when it is
`importStatus` DONE, carries at least one FAILED case result, and holds a
failure signature Testray has no verdict for.

The third clause is the one that keeps the cost bounded, and the one a naive
"queue every red build" implementation gets wrong: Stable is red most of the
time, and re-queueing a failure that already carries a verdict pays for the
same diff and the same prompt again. So the cases pinned here are the two
directions of that — a new signature queues, an explained one does not — plus
the properties that make a tick safe to run on a short interval.

No network: the build list and the signature source are both injected.
"""

import pytest

from testray_analytics.analysis import ledger as L
from testray_analytics.analysis import queue as Q
from testray_analytics.analysis import scan as S
from testray_analytics.analysis.ledger import Attribution, BuildFailures

SIG_A = "v2:aaaaaaaaaaaaaaaa"
SIG_B = "v2:bbbbbbbbbbbbbbbb"

CFG = {"testray": {"base_url": "https://testray.example"}}


class FakeSource:
    """In-memory stand-in for Testray. `builds` is {build_id: signatures}."""

    def __init__(self, builds, attributions=None):
        self._builds = builds
        self._attr = attributions or {}

    def build_failures(self, build_id):
        sigs = self._builds.get(build_id, {})
        present = {c for v in sigs.values() for c in v} | {10, 11, 20}
        return BuildFailures(build_id=build_id,
                             signatures={k: list(v) for k, v in sigs.items()},
                             not_run=set(), present=present)

    def attributions(self):
        return dict(self._attr)


def build(build_id, *, failed=0, passed=100, name=None):
    """A Build row as the REST list returns it, counters included."""
    return {"id": build_id, "name": name or f"build-{build_id}",
            "caseResultFailed": failed, "caseResultPassed": passed,
            "dueDate": f"2026-09-{build_id:02d}T00:00:00Z"}


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Wire the scanner to fakes, and force the marker queue.

    `open_queue` probes for the TriageRun Object, which is a request — and on
    the instance that matters (prod) the answer is 404, so the file queue is
    the branch under test anyway.
    """
    monkeypatch.setattr(Q.TestrayQueue, "available", staticmethod(lambda cfg: False))

    def wire(builds, signatures, attributions=None):
        monkeypatch.setattr(S, "recent_done_builds",
                            lambda cfg, routine_id, limit=None: list(builds))
        monkeypatch.setattr(S.L, "TestraySource",
                            lambda cfg, routine_id: FakeSource(signatures,
                                                               attributions))
        return tmp_path / "queue"

    return wire


def test_red_build_with_an_unexplained_failure_is_queued(offline):
    """The whole point: a new signature in a DONE build becomes one job."""
    queue_dir = offline(
        builds=[build(3, failed=1), build(2), build(1)],
        signatures={3: {SIG_A: [10]}, 2: {}, 1: {}},
    )

    summary = S.scan(CFG, 79529, queue_dir=queue_dir)

    assert summary["queued"] == 1
    pending = Q.FileQueue(queue_dir).pending()
    assert len(pending) == 1
    # Baselined on the previous build that lacked the signature, not on the
    # newest build we happened to be examining.
    assert (pending[0].baseline_build, pending[0].target_build) == (2, 3)
    assert pending[0].signatures == [SIG_A]


def test_green_builds_queue_nothing(offline):
    """caseResultFailed == 0 is not examined at all — no case-result read."""
    queue_dir = offline(
        builds=[build(3), build(2), build(1)],
        signatures={3: {}, 2: {}, 1: {}},
    )

    summary = S.scan(CFG, 79529, queue_dir=queue_dir)

    assert summary == {"targets": 0, "queued": 0, "skipped": 0,
                       "new": 0, "active": 0}
    assert Q.FileQueue(queue_dir).pending() == []


def test_a_failure_that_already_has_a_verdict_queues_nothing(offline):
    """Stable is red most of the time; explained red must cost nothing.

    Without this the scanner re-pays for the same diff and the same prompt on
    every tick for as long as the failure stays red.
    """
    queue_dir = offline(
        # Red in every build in the window: chronic, not a regression. A gap
        # would be a different case — a signature that goes away and comes
        # back IS re-queued (ledger D2), verdict on file or not.
        builds=[build(3, failed=1), build(2, failed=1), build(1, failed=1)],
        signatures={3: {SIG_A: [10]}, 2: {SIG_A: [10]}, 1: {SIG_A: [10]}},
        attributions={SIG_A: Attribution(cluster_key=SIG_A, git_hash_a="aaa",
                                         git_hash_b="bbb",
                                         date="2026-09-01T00:00:00Z")},
    )

    summary = S.scan(CFG, 79529, queue_dir=queue_dir)

    assert summary["queued"] == 0
    assert summary["active"] >= 1, "it was seen, and deliberately not queued"
    assert Q.FileQueue(queue_dir).pending() == []


def test_a_second_tick_queues_nothing_new(offline):
    """A short interval next to a long job must not stack duplicates up."""
    queue_dir = offline(
        builds=[build(3, failed=1), build(2), build(1)],
        signatures={3: {SIG_A: [10]}, 2: {}, 1: {}},
    )

    first = S.scan(CFG, 79529, queue_dir=queue_dir)
    second = S.scan(CFG, 79529, queue_dir=queue_dir)

    assert first["queued"] == 1
    assert second["queued"] == 0 and second["skipped"] == 1
    assert len(Q.FileQueue(queue_dir).pending()) == 1


def test_two_new_signatures_in_one_build_share_one_job(offline):
    """One build pair is one bundle, one diff, one prompt — so one job."""
    queue_dir = offline(
        builds=[build(3, failed=2), build(2), build(1)],
        signatures={3: {SIG_A: [10], SIG_B: [20]}, 2: {}, 1: {}},
    )

    summary = S.scan(CFG, 79529, queue_dir=queue_dir)

    assert summary["new"] == 2
    assert summary["queued"] == 1
    assert sorted(Q.FileQueue(queue_dir).pending()[0].signatures) == [SIG_A, SIG_B]


def test_dry_run_writes_nothing_but_reports_the_same_count(offline):
    queue_dir = offline(
        builds=[build(3, failed=1), build(2), build(1)],
        signatures={3: {SIG_A: [10]}, 2: {}, 1: {}},
    )

    summary = S.scan(CFG, 79529, queue_dir=queue_dir, dry_run=True)

    assert summary["queued"] == 1
    assert Q.FileQueue(queue_dir).pending() == [], "dry run must not enqueue"


def test_counters_absent_falls_back_to_reading_case_results(offline):
    """The local mirror leaves every Build counter at zero.

    Trusting them there makes the scanner report "nothing red" on a routine
    with real failures — quiet and wrong. Keyed on PASSED: a window with no
    failures is normal, a window where nothing passed means the counters were
    never computed.
    """
    queue_dir = offline(
        builds=[build(3, failed=0, passed=0), build(2, failed=0, passed=0),
                build(1, failed=0, passed=0)],
        signatures={3: {SIG_A: [10]}, 2: {}, 1: {}},
    )

    summary = S.scan(CFG, 79529, queue_dir=queue_dir)

    assert summary["queued"] == 1, "counters unusable, so ask the case results"


def test_a_pair_already_analysed_here_is_not_queued_again(offline):
    """The prod case: no verdict store, so the queue's own record is the memory.

    On an instance without the analytics Objects the ledger can never see a
    verdict, so every failure reads as new on every tick. Without this the
    scanner re-queues the same pair forever.
    """
    queue_dir = offline(
        builds=[build(3, failed=1), build(2), build(1)],
        signatures={3: {SIG_A: [10]}, 2: {}, 1: {}},
    )

    first = S.scan(CFG, 79529, queue_dir=queue_dir)
    q = Q.FileQueue(queue_dir)
    q.complete(q.pending()[0])          # what the drainer does on success

    second = S.scan(CFG, 79529, queue_dir=queue_dir)

    assert first["queued"] == 1
    assert second["queued"] == 0 and second["skipped"] == 1
    assert q.pending() == []
