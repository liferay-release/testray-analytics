"""compute_test_diff's coverage out-parameter.

The join is an inner one, so a case the baseline ran and the target did not
produces no row, no transition and no failure. These assert such a case is
still counted somewhere: it is a coverage regression, and nothing else in the
pipeline can notice it. See ARCHITECTURE §8 on build-pair comparability —
the existing overlap warning measures the join against the TARGET, which
stays near 100% precisely when the target is the side that shrank.
"""
import pandas as pd

from testray_analytics.analysis.prepare import compute_test_diff
from tests.test_transitions import _frame


def _coverage(baseline_rows, target_rows):
    cov: dict = {}
    compute_test_diff(pd.DataFrame(baseline_rows), pd.DataFrame(target_rows),
                      coverage_out=cov)
    return cov


def test_counts_cases_the_target_never_ran():
    cov = _coverage(
        [_frame(1, "PASSED", ""), _frame(2, "PASSED", ""),
         _frame(3, "FAILED", "boom"), _frame(4, "PASSED", "")],
        [_frame(1, "FAILED", "kaboom"), _frame(2, "PASSED", ""),
         _frame(5, "FAILED", "new")],
    )

    assert cov["baseline_cases"] == 4
    assert cov["target_cases"] == 3
    assert cov["ran_both"] == 2
    assert cov["baseline_only"] == 2       # cases 3 and 4 vanished
    assert cov["target_only"] == 1         # case 5 is new
    assert cov["baseline_only_by_status"] == {"PASSED": 1, "FAILED": 1}


def test_a_halved_suite_is_visible_even_though_every_row_is_green():
    """The failure this exists for: nothing fails, so nothing is triaged, and
    the build reads as clean while half its tests stopped running."""
    cov = _coverage([_frame(i, "PASSED", "") for i in range(10)],
                    [_frame(i, "PASSED", "") for i in range(5)])

    assert cov["ran_both"] == 5
    assert cov["baseline_only"] == 5
    assert cov["baseline_only_by_status"] == {"PASSED": 5}


def test_out_parameter_stays_optional():
    df, _ = compute_test_diff(
        pd.DataFrame([_frame(1, "PASSED", "")]),
        pd.DataFrame([_frame(1, "FAILED", "boom")]),
    )
    assert len(df) == 1
