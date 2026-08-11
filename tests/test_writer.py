"""Offline unit tests for testray_writer.build_batch — the payload the write
sink sends to /o/c/triageresults. No network; run with `pytest`.

Covers the LPD-95843 write contract: enum -> picklist-key flattening, the
{"key": ...} picklist shape, the CaseResult relationship FK, and the write
inclusion policy.
"""

import pandas as pd
import pytest

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
