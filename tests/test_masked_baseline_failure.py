"""
A `new` failure that the baseline was already producing on another axis.

`_aggregate_baseline` resolves a case to PASSED if ANY of its rows passed;
`_aggregate_target` resolves the same case to its WORST row. On a routine that
runs each case on several axes those two rules read one build in opposite
directions, and a case failing on one axis of three is PASSED in A and FAILED
in B — a brand-new regression, on every run, forever.

The case pinned here is ci:test:db-partition (routine 990902), where
`LocalFile.DatabasePartitioning#ExportAndAddDBPartition` failed on exactly one
of its three axes with "Impersonated actions must be audited on the real
user's ID" from build 375 (2026-09-03) onward. Every run from 375 to 393
reported it as new, and the one that was classified attributed it to a commit
three weeks newer than the change that actually caused it. ci:test:upgrade,
at 3.0 rows per case, carried the same shape on half its `new` rows.

The last two tests are the other half of the contract: routines that run one
row per case — Stable, CMS, headless, all measured at exactly 1.00 rows/case —
must come through this code untouched.
"""

import pandas as pd

from testray_analytics.analysis.prepare import (
    TRANSITION_CHANGED, TRANSITION_NEW, TRANSITION_SAME_FAILURE,
    TRIAGE_TRANSITIONS, compute_test_diff,
)

AUDIT = ("java.lang.Exception: ## 1 Liferay Exception was thrown ## "
         "LIFERAY_ERROR: Impersonated actions must be audited on the real "
         "user's ID")


def _row(case_id, status, errors, axis=0):
    """One case result. `axis` only has to make the rows distinct — the
    aggregation keys on case_id, which is the point."""
    return {
        "case_id": case_id, "caseresult_id": case_id * 10 + axis,
        "case_name": f"Test{case_id}", "case_flaky": None,
        "component_name": "Database Partitioning", "team_name": None,
        "status": status, "errors": errors, "jira_issue": None,
        "subtask_id": 0,
    }


def _db_partition_pair(target_error=AUDIT):
    """The real shape: three axes per case, one of them failing on BOTH
    builds with the same error."""
    baseline = pd.DataFrame([
        _row(1, "FAILED", AUDIT, axis=0),
        _row(1, "PASSED", "", axis=1),
        _row(1, "PASSED", "", axis=2),
    ])
    target = pd.DataFrame([
        _row(1, "FAILED", target_error, axis=0),
        _row(1, "PASSED", "", axis=1),
        _row(1, "PASSED", "", axis=2),
    ])
    return baseline, target


def test_a_failure_the_baseline_also_had_is_not_new():
    baseline, target = _db_partition_pair()

    df, counts = compute_test_diff(baseline, target)

    assert list(df["transition"]) == [TRANSITION_SAME_FAILURE]
    assert counts[TRANSITION_NEW] == 0, (
        "the any-axis-passed rule manufactured a regression out of a failure "
        "present on both builds")


def test_it_does_not_reach_the_classifier():
    """The money assertion. This row used to be billed as a regression of the
    range on every run of a daily routine."""
    df, _ = compute_test_diff(*_db_partition_pair())

    assert set(df["transition"]).isdisjoint(TRIAGE_TRANSITIONS)


def test_the_row_still_appears_on_the_report():
    """Not new is not the same as not failing. The test IS broken; §12's rule
    is that the transition label may not remove rows from the report."""
    df, _ = compute_test_diff(*_db_partition_pair())

    assert len(df) == 1
    assert df.iloc[0]["error_message"] == AUDIT


def test_a_different_error_on_the_masked_axis_is_a_changed_failure():
    """Re-reading goes through the same §12 matrix, so the baseline having
    failed differently is `changed` — the camouflage case §12 exists for, and
    the reason this is not simply "drop it if the baseline failed too"."""
    baseline, target = _db_partition_pair(
        target_error="java.lang.IllegalStateException: partition already exists")

    df, _ = compute_test_diff(baseline, target)

    assert list(df["transition"]) == [TRANSITION_CHANGED]
    assert df.iloc[0]["baseline_error_message"] == AUDIT


def test_a_genuinely_new_failure_is_still_new():
    """The guard must key on the masked row, not on "the baseline had any
    failure anywhere" — otherwise it eats real regressions."""
    baseline = pd.DataFrame([
        _row(1, "PASSED", "", axis=0),
        _row(1, "PASSED", "", axis=1),
        _row(2, "FAILED", AUDIT, axis=0),
    ])
    target = pd.DataFrame([
        _row(1, "FAILED", "NoSuchMethodError: Foo.bar()", axis=0),
        _row(1, "PASSED", "", axis=1),
        _row(2, "FAILED", AUDIT, axis=0),
    ])

    df, _ = compute_test_diff(baseline, target)

    got = dict(zip(df["testray_case_id"], df["transition"]))
    assert got[1] == TRANSITION_NEW
    assert got[2] == TRANSITION_SAME_FAILURE


def test_a_masked_pass_does_not_rescue_a_failing_baseline():
    """When the worst row wins in A anyway, nothing was hidden and the
    existing FAILED→FAILED reading must be what applies."""
    baseline = pd.DataFrame([_row(1, "FAILED", AUDIT, axis=0)])
    target = pd.DataFrame([
        _row(1, "FAILED", AUDIT, axis=0),
        _row(1, "PASSED", "", axis=1),
    ])

    df, _ = compute_test_diff(baseline, target)

    assert list(df["transition"]) == [TRANSITION_SAME_FAILURE]


# --- one row per case: Stable, CMS, headless -------------------------------

def test_one_row_per_case_is_untouched():
    """Stable is 1.00 rows/case on every bundle measured, so there is never a
    second row to mask and this code must be inert there. Asserted rather than
    reasoned about, because Stable gates everyone's upstream master."""
    baseline = pd.DataFrame([
        _row(1, "PASSED", ""),
        _row(2, "FAILED", "VERSION INCREASE REQUIRED"),
        _row(3, "PASSED", ""),
    ])
    target = pd.DataFrame([
        _row(1, "FAILED", "VERSION INCREASE REQUIRED"),
        _row(2, "FAILED", "VERSION INCREASE REQUIRED"),
        _row(3, "PASSED", ""),
    ])

    df, counts = compute_test_diff(baseline, target)

    got = dict(zip(df["testray_case_id"], df["transition"]))
    assert got == {1: TRANSITION_NEW, 2: TRANSITION_SAME_FAILURE}, (
        "a one-row-per-case routine changed behaviour — case 1 is a real new "
        "failure that happens to share a signature with case 2's chronic one")
    assert counts[TRANSITION_NEW] == 1


def test_it_is_not_a_build_wide_prevalence_test():
    """The tempting cheap fix — demote `new` when `baseline_signature_count`
    is non-zero — is wrong on exactly the shape above: that count is computed
    over the whole baseline build, so on a 1300-case Stable build one chronic
    signature would suppress every new failure that shares it. This pins the
    distinction so the cheap version cannot be reintroduced."""
    baseline = pd.DataFrame(
        [_row(1, "PASSED", "")]
        + [_row(n, "FAILED", "modules-compile failed") for n in range(2, 8)])
    target = pd.DataFrame(
        [_row(1, "FAILED", "modules-compile failed")]
        + [_row(n, "FAILED", "modules-compile failed") for n in range(2, 8)])

    df, _ = compute_test_diff(baseline, target)

    got = dict(zip(df["testray_case_id"], df["transition"]))
    assert got[1] == TRANSITION_NEW, (
        "six prior occurrences of this signature elsewhere in the baseline "
        "must not make case 1's first failure look pre-existing")
