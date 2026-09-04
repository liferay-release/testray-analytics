"""
Tests for the Testray-backed signature ledger.

The source is injected, so the whole decision layer is testable without a
server. The cases that matter are the ones a naive implementation gets wrong:

  - a signature debuting mid-burst must baseline against the previous RED build
    that lacked it, not against the last green build (D1);
  - a signature vanishing because its cases were SKIPPED must not read as fixed
    (D4) — every Stable build carries ~199 UNTESTED rows;
  - a signature that already has a verdict must not be re-attributed (D2)
    unless it demonstrably went away and came back.
"""

import pytest

from testray_analytics.analysis.ledger import (
    Attribution, BuildFailures, STATE_ACTIVE, STATE_NEW, STATE_REGRESSED,
    SignatureIndex,
)

SIG_A = "v2:aaaaaaaaaaaaaaaa"
SIG_B = "v2:bbbbbbbbbbbbbbbb"


class FakeSource:
    """In-memory stand-in for Testray. `builds` is {build_id: (sigs, not_run)}."""

    def __init__(self, builds, attributions=None):
        self._builds = builds
        self._attr = attributions or {}
        self.fetches = []

    def build_failures(self, build_id):
        self.fetches.append(build_id)
        spec = self._builds[build_id]
        sigs, not_run = spec[0], spec[1]
        # A build contains every case it reports on. `present` may be given
        # explicitly to model a case that did not exist yet in that build.
        present = spec[2] if len(spec) > 2 else (
            {c for v in sigs.values() for c in v} | set(not_run) | {10, 11, 20})
        return BuildFailures(build_id=build_id,
                             signatures={k: list(v) for k, v in sigs.items()},
                             not_run=set(not_run), present=set(present))

    def attributions(self):
        return dict(self._attr)


def attr(sig, a="aaa", b="bbb", date="2026-09-01T00:00:00Z"):
    return Attribution(cluster_key=sig, git_hash_a=a, git_hash_b=b, date=date)


# --- D1: the baseline is per-signature, not "last green" -------------------

def test_baseline_is_the_previous_build_when_it_lacked_the_signature():
    src = FakeSource({3: ({SIG_A: [10]}, []), 2: ({}, []), 1: ({}, [])})
    idx = SignatureIndex(src)
    st = idx.classify_build(3, [2, 1])[0]
    assert st.state == STATE_NEW
    assert st.baseline_build == 2 and st.target_build == 3


def test_debut_mid_burst_baselines_on_a_red_build_not_the_last_green():
    """A build can be red with OTHER failures and still be the right baseline."""
    src = FakeSource({
        4: ({SIG_B: [20], SIG_A: [10]}, []),   # SIG_A debuts
        3: ({SIG_B: [20]}, []),                # red, unrelated
        2: ({SIG_B: [20]}, []),                # red, unrelated
        1: ({}, []),                           # the last GREEN build
    })
    st = [s for s in SignatureIndex(src).classify_build(4, [3, 2, 1])
          if s.cluster_key == SIG_A][0]
    assert st.baseline_build == 3, "must not reach past a red build that lacked it"


def test_observation_gap_widens_the_range_by_exactly_the_gap():
    src = FakeSource({9: ({SIG_A: [10]}, []), 5: ({}, []), 1: ({}, [])})
    st = SignatureIndex(src).classify_build(9, [5, 1])[0]
    assert st.baseline_build == 5


def test_no_baseline_when_the_signature_is_present_throughout():
    src = FakeSource({3: ({SIG_A: [10]}, []), 2: ({SIG_A: [10]}, []),
                      1: ({SIG_A: [10]}, [])})
    st = SignatureIndex(src).classify_build(3, [2, 1])[0]
    assert st.baseline_build is None, "inventing a range is worse than skipping"


def test_walk_passes_through_builds_that_still_carry_the_signature():
    """A failure noticed several builds late still gets its true first range.

    Regression test: the walk used to stop at the first predecessor that still
    had the signature and report "no baseline", so a failure red for four builds
    was never attributed at all.
    """
    src = FakeSource({
        5: ({SIG_A: [10]}, []),   # looking from here
        4: ({SIG_A: [10]}, []),
        3: ({SIG_A: [10]}, []),   # first appearance
        2: ({}, []),              # last genuine absence  <- baseline
        1: ({}, []),
    })
    st = SignatureIndex(src).classify_build(5, [4, 3, 2, 1])[0]
    assert st.baseline_build == 2, "must walk through the episode to its start"
    assert st.target_build == 3, "range ends where it APPEARED, not where we looked"


def test_skipped_builds_inside_the_walk_do_not_end_it():
    src = FakeSource({
        4: ({SIG_A: [10]}, []),
        3: ({}, [10]),            # absent, but nothing ran — proves nothing
        2: ({SIG_A: [10]}, []),   # still the same episode
        1: ({}, []),              # genuine absence
    })
    st = SignatureIndex(src).classify_build(4, [3, 2, 1])[0]
    assert st.baseline_build == 1 and st.target_build == 2


# --- D4: a skipped case is not a fix ---------------------------------------

def test_a_build_that_skipped_the_cases_cannot_be_the_baseline():
    src = FakeSource({
        3: ({SIG_A: [10]}, []),
        2: ({}, [10]),        # SIG_A absent, but case 10 never ran
        1: ({}, []),          # case 10 ran clean here
    })
    st = SignatureIndex(src).classify_build(3, [2, 1])[0]
    assert st.baseline_build == 1, "absence is not evidence when nothing ran"


def test_partially_skipped_build_still_counts_when_any_member_ran():
    src = FakeSource({3: ({SIG_A: [10, 11]}, []), 2: ({}, [10]), 1: ({}, [])})
    st = SignatureIndex(src).classify_build(3, [2, 1])[0]
    assert st.baseline_build == 2, "one member running is enough to prove absence"


# --- D2: attribute once ----------------------------------------------------

def test_a_signature_with_a_verdict_is_active_and_needs_nothing():
    src = FakeSource({3: ({SIG_A: [10]}, []), 2: ({SIG_A: [10]}, [])},
                     attributions={SIG_A: attr(SIG_A)})
    st = SignatureIndex(src).classify_build(3, [2])[0]
    assert st.state == STATE_ACTIVE and not st.needs_attribution
    assert st.prior.git_hash_a == "aaa", "the existing verdict is carried, not redone"


def test_a_signature_that_went_away_and_returned_is_a_regression():
    src = FakeSource({
        4: ({SIG_A: [10]}, []),   # back
        3: ({}, []),              # clean run — cases ran, signature gone
        2: ({SIG_A: [10]}, []),
    }, attributions={SIG_A: attr(SIG_A)})
    st = SignatureIndex(src).classify_build(4, [3, 2])[0]
    assert st.state == STATE_REGRESSED and st.needs_attribution
    assert st.baseline_build == 3, "the new episode gets its own range"


def test_a_gap_of_skipped_builds_is_not_a_regression():
    src = FakeSource({
        4: ({SIG_A: [10]}, []),
        3: ({}, [10]),            # absent only because it did not run
        2: ({SIG_A: [10]}, []),
    }, attributions={SIG_A: attr(SIG_A)})
    st = SignatureIndex(src).classify_build(4, [3, 2])[0]
    assert st.state == STATE_ACTIVE, "a skipped build must not fake a regression"


# --- efficiency ------------------------------------------------------------

def test_predecessor_builds_are_fetched_at_most_once_per_run():
    src = FakeSource({
        3: ({SIG_A: [10], SIG_B: [20]}, []),
        2: ({}, []), 1: ({}, []),
    })
    idx = SignatureIndex(src)
    idx.classify_build(3, [2, 1])
    assert src.fetches.count(2) == 1, "two new signatures must share one fetch"


def test_baseline_walk_is_capped():
    from testray_analytics.analysis.ledger import MAX_BASELINE_WALK
    builds = {n: ({SIG_A: [10]}, []) for n in range(200, 0, -1)}
    idx = SignatureIndex(FakeSource(builds))
    idx.classify_build(200, list(range(199, 0, -1)))
    assert len(idx._failures) <= MAX_BASELINE_WALK + 1


# --- a case that did not exist yet in the baseline --------------------------

def test_a_build_predating_the_test_cannot_be_its_baseline():
    """Regression: a newly-added test is ABSENT from older builds, not passing.

    `ran()` used to ask only "is this case outside the not-run set?", which is
    trivially true for a case the build never contained. The scanner queued a
    range whose baseline predated the test's existence; prepare classes that
    `no_baseline` and refused the work after the run had already been paid for.
    """
    src = FakeSource({
        3: ({SIG_A: [99]}, [], {99, 10}),   # case 99 exists and fails
        2: ({}, [], {10}),                  # case 99 does not exist yet
        1: ({}, [], {10, 99}),              # case 99 exists and passed
    })
    st = SignatureIndex(src).classify_build(3, [2, 1])[0]
    assert st.baseline_build == 1, "must skip builds that never contained the case"


def test_no_baseline_when_the_test_is_brand_new():
    """Nothing before it ever ran the case, so there is no range to attribute."""
    src = FakeSource({
        2: ({SIG_A: [99]}, [], {99, 10}),
        1: ({}, [], {10}),                  # case 99 did not exist
    })
    st = SignatureIndex(src).classify_build(2, [1])[0]
    assert st.baseline_build is None, "a brand-new failing test has no baseline"
