"""A baseline row that never ran cannot establish that a failure CHANGED.

Real case (prod routine 107946124, case 3096441, 2026-08-24): the target
reported `2 Failed tests testGraphQLGetCartBillingAddres...` and the baseline
reported `testRun: @{test.task.name} was not executed`. The signatures differ,
so the pair was triaged as a changed failure and reached NEEDS_REVIEW — for a
test that had failed with the IDENTICAL error in the previous ~20 builds. The
baseline was not a healthy reference; it simply had not run the test.

Such a row is NOT dropped (changed 2026-08-26): no baseline usually means a
new test, and a failing new test must never disappear.

It is also no longer withheld from the classifier (changed 2026-09-04). "No
baseline to diff against" is an argument about the BASELINE, not about whether
the failure can be explained — a test added in this range and failing on its
first run is among the most attributable failures there is, because the commit
that added it is in the diff. Withholding them meant a red build could complete
with no verdicts at all, which is how a real defect
(PortalLogAssertorTest#testScanXMLLog, build 512102) shipped under a green
diamond.

What still skips the classifier is a row with nothing to reason from: one that
did not actually fail in the target (an UNTESTED row carrying the harness
notice "Unable to run test on CI" is text, not a defect), or one with no error
text at all. See `prepare._explainable`.
"""
import pandas as pd

from testray_analytics.analysis.prepare import (
    NO_BASELINE_PRE, TRANSITION_CHANGED, TRANSITION_NO_BASELINE,
    TRANSITION_SAME_FAILURE, compute_test_diff, enrich_and_pre_classify,
)
from testray_analytics.analysis.submit import _auto_label
from testray_analytics.analysis.prompt_helpers import pre_classify
from tests.test_transitions import _frame

_NOT_EXECUTED = ("testRun: @{test.task.name} was not executed. "
                 "Please check the logs for details.")
_REAL_FAILURE = ("2 Failed tests     testGraphQLGetCartBillingAddres      "
                 "testGraphQLGetCartShippingAddres")


def test_not_executed_is_recognised_as_a_did_not_run_notice():
    assert pre_classify(_NOT_EXECUTED) == "BUILD_FAILURE"
    assert pre_classify(_REAL_FAILURE) is None


def _transition(baseline_error, target_error):
    _, counts = compute_test_diff(
        pd.DataFrame([_frame(1, "FAILED", baseline_error)]),
        pd.DataFrame([_frame(1, "FAILED", target_error)]),
    )
    assert sum(counts.values()) == 1
    return next(iter(counts))


def test_baseline_that_never_ran_is_no_baseline_not_changed():
    assert _transition(_NOT_EXECUTED, _REAL_FAILURE) == TRANSITION_NO_BASELINE


def _no_baseline_row():
    df, _ = compute_test_diff(
        pd.DataFrame([_frame(1, "FAILED", _NOT_EXECUTED)]),
        pd.DataFrame([_frame(1, "FAILED", _REAL_FAILURE)]),
    )
    return df


def test_the_no_baseline_row_stays_in_triage():
    """It must NOT be dropped. A case with no baseline result is most often a
    NEW test, and a new test that fails is precisely what triage is for —
    dropping it hid the failure entirely."""
    df = _no_baseline_row()
    assert len(df) == 1
    assert df.iloc[0]["transition"] == TRANSITION_NO_BASELINE


def test_a_no_baseline_row_that_really_failed_goes_to_the_classifier():
    """It failed and it said why, so it is explainable — baseline or not."""
    df = enrich_and_pre_classify(_no_baseline_row())
    assert pd.isna(df.iloc[0]["pre_classification"]), \
        "a failing new test with error text must reach the classifier"


def test_a_no_baseline_row_with_no_error_text_is_still_withheld():
    """Nothing written down, nothing to reason from."""
    df, _ = compute_test_diff(
        pd.DataFrame([_frame(1, "FAILED", _NOT_EXECUTED)]),
        pd.DataFrame([_frame(1, "FAILED", "")]),
    )
    assert enrich_and_pre_classify(df).iloc[0]["pre_classification"] is not None


def test_explainable_requires_a_real_failure_not_just_error_text():
    """An UNTESTED row's harness notice reads as text but describes the
    harness, not a defect. Gating on text alone sent 199 never-ran rows from
    one build to the classifier — this is the guard that stops it."""
    from testray_analytics.analysis.prepare import _explainable
    df = pd.DataFrame({
        "status_b":      ["FAILED", "UNTESTED", "FAILED", "UNTESTED"],
        "error_message": ["PortalLogAssertorTest#testScanXMLLog: ...",
                          "Unable to run test on CI", "", ""],
    })
    assert list(_explainable(df)) == [True, False, False, False]


def test_the_no_baseline_row_surfaces_as_needs_review():
    assert _auto_label(NO_BASELINE_PRE) == "NEEDS_REVIEW"


def test_a_more_specific_pattern_still_wins_over_no_baseline():
    """An env failure on the target side keeps its own label — no_baseline
    only fills where nothing more specific fired."""
    df, _ = compute_test_diff(
        pd.DataFrame([_frame(1, "FAILED", _NOT_EXECUTED)]),
        pd.DataFrame([_frame(1, "FAILED", "session not created: chrome=100.0")]),
    )
    df = enrich_and_pre_classify(df)
    assert df.iloc[0]["pre_classification"] != NO_BASELINE_PRE


def test_the_reverse_direction_is_untouched():
    """Target stopped running while the baseline had a real failure: that is
    not 'no baseline signal', and must not be silently reclassified."""
    assert _transition(_REAL_FAILURE, _NOT_EXECUTED) != TRANSITION_NO_BASELINE


def test_two_real_but_different_failures_are_still_changed():
    assert _transition("NullPointerException in Foo",
                       "IllegalStateException in Bar") == TRANSITION_CHANGED


def test_env_churn_guard_still_wins_when_both_sides_are_env():
    assert _transition("The build failed prior to running the test on agent-7",
                       "The build failed prior to running the test on agent-42") \
        == TRANSITION_SAME_FAILURE
