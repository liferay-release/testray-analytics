"""compute_test_diff's coverage out-parameter.

The join is an inner one, so a case the baseline ran and the target did not
produces no row, no transition and no failure. These assert such a case is
still counted somewhere: it is a coverage regression, and nothing else in the
pipeline can notice it. See ARCHITECTURE §8 on build-pair comparability —
the existing overlap warning measures the join against the TARGET, which
stays near 100% precisely when the target is the side that shrank.
"""
import pandas as pd

from testray_analytics.analysis import report
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


# ---------------------------------------------------------------------------
# The report side: the pill and the banner must quote the same number.
# ---------------------------------------------------------------------------

# The real pair (2026.Q3.0 -> Q3.1): the baseline ran 18,161 cases, the target
# 7,738, and 7,699 ran on both. Against the target that is 99% -- true, and
# useless, because the target is the side that shrank. Against the larger
# build it is 42%, which is what a reader means by "how much did this cover".
_SHRUNK = {
    "target_rows": 7738,
    "coverage": {"baseline_cases": 18161, "target_cases": 7738,
                 "ran_both": 7699, "baseline_only": 10462, "target_only": 0},
    "status_matrix": {"PASSED": {"PASSED": 7699}},
}


def test_denominator_is_the_larger_build_not_the_target():
    compared, denom = report._join_coverage(_SHRUNK)
    assert compared == 7699
    assert denom == 18161
    assert round(100.0 * compared / denom) == 42


def test_the_pill_and_the_banner_agree():
    """The defect this guards: each computed its own denominator, so the pill
    warned at 42% while the banner measured 99% against the target, cleared
    the 50% threshold and stayed silent on the very same run."""
    pill   = report._totals(pd.DataFrame(), 0, _SHRUNK)
    banner = report._banners(pd.DataFrame(), _SHRUNK)

    # Same number, different precision: the banner always quotes one decimal,
    # the pill drops it above 10%. That much is deliberate.
    assert "42%" in pill
    assert "pill warn" in pill, "the pill must flag low coverage"
    assert "covers only 42.4% of the build" in banner
    # The banner must cite the larger build, not the target it used to divide by.
    assert "18,161" in banner
    assert "7,738" not in banner


def test_no_banner_when_coverage_is_healthy():
    meta = {"target_rows": 100,
            "coverage": {"baseline_cases": 100, "target_cases": 100},
            "status_matrix": {"PASSED": {"PASSED": 98}}}
    assert "covers only" not in report._banners(pd.DataFrame(), meta)


def test_falls_back_to_target_rows_without_a_coverage_key():
    """A run.yml written before `coverage` existed still renders."""
    meta = {"target_rows": 200, "status_matrix": {"PASSED": {"PASSED": 50}}}
    compared, denom = report._join_coverage(meta)
    assert (compared, denom) == (50, 200)
