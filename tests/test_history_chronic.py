"""Pre-existing failures must not reach NEEDS_REVIEW.

The baseline is a single build. When it did not run a test (or ran it on a bad
day), a test that has been broken for months is triaged as a fresh regression.
Case 3096441 on prod routine 107946124 had failed in ~20 consecutive builds and
still landed in NEEDS_REVIEW, because the one baseline build recorded
"was not executed" and the signatures therefore differed.
"""
import pandas as pd
import pytest

from testray_analytics.analysis.prepare import (
    PRE_EXISTING, _nonpass_streak, attach_history,
)
from testray_analytics.analysis.submit import _auto_label


def _h(*statuses):
    return [{"status": s, "errors": "2 Failed tests foo"} for s in statuses]


# --- the streak itself -----------------------------------------------------

def test_streak_counts_consecutive_non_passes():
    assert _nonpass_streak(_h("FAILED", "FAILED", "FAILED")) == 3


def test_a_pass_ends_the_streak():
    assert _nonpass_streak(_h("FAILED", "FAILED", "PASSED", "FAILED")) == 2


def test_most_recent_pass_means_no_streak():
    assert _nonpass_streak(_h("PASSED", "FAILED", "FAILED")) == 0


def test_untested_does_not_end_the_streak():
    """A build that could not run the test is not evidence it recovered."""
    assert _nonpass_streak(_h("FAILED", "UNTESTED", "FAILED")) == 3


def test_empty_history_is_not_a_streak():
    assert _nonpass_streak([]) == 0


# --- attach_history --------------------------------------------------------

def _df(case_id=1, pre=None):
    return pd.DataFrame([{"testray_case_id": case_id, "error_message": "boom",
                          "pre_classification": pre}])


def test_chronic_row_is_pre_classified_out_of_the_classify_set():
    out = attach_history(_df(), {1: _h(*["FAILED"] * 6)}, streak_threshold=5)
    assert out.loc[0, "pre_classification"] == PRE_EXISTING
    assert out.loc[0, "history_fail_streak"] == 6


def test_a_streak_below_the_threshold_is_left_alone():
    out = attach_history(_df(), {1: _h("FAILED", "FAILED")}, streak_threshold=5)
    assert pd.isna(out.loc[0, "pre_classification"])


def test_recent_regression_is_left_alone():
    """Failed the last 2, passed before that — exactly what triage is for."""
    out = attach_history(_df(), {1: _h("FAILED", "FAILED", "PASSED", "PASSED")},
                         streak_threshold=5)
    assert pd.isna(out.loc[0, "pre_classification"])


def test_an_existing_env_label_is_not_overwritten():
    out = attach_history(_df(pre="ENV_SETUP"), {1: _h(*["FAILED"] * 9)},
                         streak_threshold=5)
    assert out.loc[0, "pre_classification"] == "ENV_SETUP"


def test_missing_history_leaves_the_row_classifiable():
    out = attach_history(_df(), {1: []}, streak_threshold=5)
    assert pd.isna(out.loc[0, "pre_classification"])
    assert out.loc[0, "history_depth"] == 0


def test_no_history_at_all_is_a_no_op():
    df = _df()
    assert attach_history(df, {}) is df


# --- the label a reader sees ----------------------------------------------

def test_pre_existing_reads_as_false_positive_not_env_failure():
    """It is a real failure, just not one this diff caused. Reusing
    FALSE_POSITIVE keeps verdicts.py and the Testray picklist untouched."""
    assert _auto_label(PRE_EXISTING) == "FALSE_POSITIVE"
    assert _auto_label("ENV_SETUP") == "ENV_FAILURE"
    assert _auto_label("BUILD_FAILURE") == "DID_NOT_RUN"
