"""Offline unit tests for testray_writer.build_batch — the payload the write
sink sends to /o/c/triageresults. No network; run with `pytest`.

Covers the LPD-95843 write contract: enum -> picklist-key flattening, the
{"key": ...} picklist shape, the CaseResult relationship FK, and the write
inclusion policy.
"""

import pandas as pd
import pytest

from testray_analytics.analysis import error_signature
from testray_analytics.analysis.testray_writer import build_batch

META = {
    "build_id_b": 82964123,
    "mode": "per-test",
    "git_hash_a": "aaaa111",
    "git_hash_b": "bbbb222",
}
CLASSIFIER = "api:claude-opus-4-8"


def _row(**kw):
    """A classifiable diff row with sane defaults; override per test."""
    base = {
        "testray_case_id": 111,
        "caseresult_id": 900111,
        "classification": "BUG",
        "confidence": "high",
        "culprit_file": None,
        "specific_change": None,
        "reason": "because",
    }
    base.update(kw)
    return base


def _batch(rows):
    return build_batch(pd.DataFrame(rows), META, CLASSIFIER)


def _by_erc(items):
    return {it["externalReferenceCode"]: it for it in items}


# --- classification key flattening -----------------------------------------

@pytest.mark.parametrize("enum,key", [
    ("BUG", "BUG"),
    ("POSSIBLE_BUG", "POSSIBLEBUG"),
    ("NEEDS_REVIEW", "NEEDSREVIEW"),
    ("TEST_FIX", "TESTFIX"),
    ("FALSE_POSITIVE", "FALSEPOSITIVE"),
])
def test_classification_enum_flattened_to_picklist_key(enum, key):
    # FALSE_POSITIVE must not be high or it's excluded by policy.
    conf = "low" if enum == "FALSE_POSITIVE" else "high"
    items = _batch([_row(classification=enum, confidence=conf)])
    assert len(items) == 1
    assert items[0]["classification"] == {"key": key}


def test_unknown_classification_raises():
    with pytest.raises(ValueError):
        _batch([_row(classification="WAT")])


# --- picklist shape ---------------------------------------------------------

def test_confidence_is_key_object_and_passthrough():
    items = _batch([_row(confidence="medium")])
    assert items[0]["confidence"] == {"key": "medium"}


def test_missing_confidence_field_is_omitted_not_null():
    items = _batch([_row(classification="NEEDS_REVIEW", confidence=None)])
    assert "confidence" not in items[0]


# --- relationship FK --------------------------------------------------------

def test_fk_set_from_caseresult_id():
    items = _batch([_row(caseresult_id=555)])
    assert items[0]["r_caseResultToTriageResults_c_caseResultId"] == 555


def test_missing_caseresult_id_writes_unlinked():
    items = _batch([_row(caseresult_id=None)])
    assert "r_caseResultToTriageResults_c_caseResultId" not in items[0]


def test_nan_caseresult_id_writes_unlinked():
    items = _batch([_row(caseresult_id=float("nan"))])
    assert "r_caseResultToTriageResults_c_caseResultId" not in items[0]


# --- externalReferenceCode --------------------------------------------------

def test_erc_is_build_case_classifier():
    items = _batch([_row(testray_case_id=222)])
    assert items[0]["externalReferenceCode"] == f"82964123_222_{CLASSIFIER}"


# --- inclusion policy -------------------------------------------------------

def test_false_positive_high_excluded():
    items = _batch([_row(classification="FALSE_POSITIVE", confidence="high")])
    assert items == []


def test_false_positive_low_included():
    items = _batch([_row(classification="FALSE_POSITIVE", confidence="low")])
    assert len(items) == 1


@pytest.mark.parametrize("auto", ["DID_NOT_RUN", "ENV_FAILURE"])
def test_auto_buckets_excluded(auto):
    items = _batch([_row(classification=auto, confidence=None)])
    assert items == []


def test_row_without_case_id_skipped():
    items = _batch([_row(testray_case_id=None)])
    assert items == []


# --- field hygiene ----------------------------------------------------------

def test_null_scalar_fields_dropped():
    items = _batch([_row(culprit_file=None, specific_change=None)])
    it = items[0]
    assert "culpritFile" not in it
    assert "specificChange" not in it


def test_populated_fields_present():
    items = _batch([_row(culprit_file="mod/Foo.java",
                         specific_change="removed method")])
    it = items[0]
    assert it["culpritFile"] == "mod/Foo.java"
    assert it["specificChange"] == "removed method"
    assert it["classifier"] == CLASSIFIER
    assert it["analysisMode"] == "per-test"
    assert it["gitHashA"] == "aaaa111"
    assert it["gitHashB"] == "bbbb222"


def test_mixed_batch_counts():
    items = _batch([
        _row(testray_case_id=1, classification="BUG", confidence="high"),
        _row(testray_case_id=2, classification="POSSIBLE_BUG", confidence="medium"),
        _row(testray_case_id=3, classification="FALSE_POSITIVE", confidence="high"),   # excluded
        _row(testray_case_id=4, classification="DID_NOT_RUN", confidence=None),        # excluded
        _row(testray_case_id=5, classification="TEST_FIX", confidence="low",
             caseresult_id=None),                                                      # unlinked
    ])
    erc = _by_erc(items)
    assert len(items) == 3
    fk = "r_caseResultToTriageResults_c_caseResultId"
    assert fk in erc[f"82964123_1_{CLASSIFIER}"]
    assert fk not in erc[f"82964123_5_{CLASSIFIER}"]


# --- clusterKey (§7) --------------------------------------------------------

def test_cluster_key_is_versioned_and_present():
    """Reference the constant, not a literal — the version bumps whenever
    normalize() changes behaviour, and a hardcoded prefix turns that into a
    spurious test failure."""
    items = _batch([_row(error_message="NPE at Foo.java:42")])
    assert items[0]["clusterKey"].startswith(f"{error_signature.SIGNATURE_VERSION}:")


def test_same_root_cause_shares_a_cluster_key():
    """Different tests, same error — one cluster. This is the whole point:
    34 tests failing on one ElementNotFound should be triaged once."""
    items = _batch([
        _row(testray_case_id=1, test_case="TestA",
             error_message="ElementNotFound: selector #foo at Bar.java:10"),
        _row(testray_case_id=2, test_case="TestB",
             error_message="ElementNotFound: selector #foo at Bar.java:88"),
    ])
    assert items[0]["clusterKey"] == items[1]["clusterKey"]


def test_different_errors_get_different_cluster_keys():
    items = _batch([
        _row(testray_case_id=1, error_message="NullPointerException in Foo"),
        _row(testray_case_id=2, error_message="TimeoutException waiting for x"),
    ])
    assert items[0]["clusterKey"] != items[1]["clusterKey"]


def test_culprit_file_separates_clusters():
    """Same error text attributed to different files is not one root cause."""
    items = _batch([
        _row(testray_case_id=1, error_message="assert failed", culprit_file="a/A.java"),
        _row(testray_case_id=2, error_message="assert failed", culprit_file="b/B.java"),
    ])
    assert items[0]["clusterKey"] != items[1]["clusterKey"]


def test_missing_culprit_file_from_pandas_does_not_crash():
    """A missing culprit_file arrives as NaN, not None. NaN is a float and is
    truthy, so `value or ""` keeps it and .strip() raises — this crashed a real
    submit. NaN must behave exactly like an absent value."""
    import math
    items = _batch([
        _row(testray_case_id=1, culprit_file=float("nan"), error_message="boom"),
        _row(testray_case_id=2, culprit_file=None, error_message="boom"),
    ])
    assert items[0]["clusterKey"] == items[1]["clusterKey"]


def test_nan_error_message_is_treated_as_blank_not_the_string_nan():
    """str(NaN) is 'nan'; letting that through would make every error-less
    failure cluster together under a bogus signature."""
    a = _batch([_row(testray_case_id=1, test_case="T1", error_message=float("nan"))])
    b = _batch([_row(testray_case_id=2, test_case="T2", error_message=float("nan"))])
    # Different tests, no error text -> the test-name fallback keeps them apart.
    assert a[0]["clusterKey"] != b[0]["clusterKey"]


# --- TriageRun verdict counts (index/report agreement) ---------------------

def _run(rows, **kw):
    from testray_analytics.analysis.testray_writer import build_triage_run
    return build_triage_run(
        {**META, "run_id": "r_test"}, pd.DataFrame(rows),
        classifier=CLASSIFIER, n_written=len(rows), n_excluded=0, **kw)


def test_verdict_counts_use_the_display_label_not_the_raw_classification():
    """The Testray index renders these counts straight onto a column.

    Regression: they were counted from `classification`, so a run whose report
    showed 1 NEEDS_REVIEW + 2 NOT_ATTRIBUTABLE was stored as 3 NEEDS_REVIEW,
    and the index disagreed with the report you clicked into. The relabel rule
    lives in `verdicts.display_verdict` and both sides must use it.
    """
    import json

    payload = _run([
        _row(testray_case_id=1, classification="NEEDS_REVIEW", confidence="high"),
        _row(testray_case_id=2, classification="NEEDS_REVIEW", confidence="low"),
        # No confidence means the row never reached the model — an auto label
        # from submit._auto_label. Nothing failed to attribute it, so it stays
        # NEEDS_REVIEW rather than claiming a failed attribution.
        _row(testray_case_id=3, classification="NEEDS_REVIEW", confidence=""),
        # Low confidence but it NAMED a candidate: it attributed something and
        # could not choose, which is not the same as attributing nothing.
        _row(testray_case_id=4, classification="NEEDS_REVIEW", confidence="low",
             specific_change="LPD-103652 reworked the dropdown items"),
        _row(testray_case_id=5, classification="BUG", confidence="high"),
    ])
    counts = json.loads(payload["verdictCounts"])

    assert counts.get("NEEDS_REVIEW") == 3, counts
    assert counts.get("NOT_ATTRIBUTABLE") == 1, counts
    assert counts.get("BUG") == 1, counts
    # The raw label must not leak an inflated figure alongside the split.
    assert sum(counts.values()) == 5, counts


def test_verdict_counts_reproduce_a_real_runs_rendered_totals():
    """Guards the confidence thresholds against a silent widening.

    These four rows stand in for the shape of run r_20260818T200045Z: every
    NEEDS_REVIEW carried `low` or `medium`, and only the `low` ones are the
    classifier declining to attribute. A change that swept `medium` into
    NOT_ATTRIBUTABLE would quietly reclassify most of a release's failures.
    """
    import json

    payload = _run([
        _row(testray_case_id=1, classification="NEEDS_REVIEW", confidence="medium"),
        _row(testray_case_id=2, classification="NEEDS_REVIEW", confidence="low"),
    ])
    counts = json.loads(payload["verdictCounts"])

    assert counts == {"NEEDS_REVIEW": 1, "NOT_ATTRIBUTABLE": 1}
