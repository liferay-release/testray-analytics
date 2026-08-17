"""The caseResult object id must survive fetch → diff → diff_list.csv.

It is the FK the TriageResult write hangs off
(`r_caseResultToTriageResults_c_caseResultId`) and it is only knowable at fetch
time — `/o/c/caseresults` returns it, and nothing downstream can recover it
from a case id alone. It used to be requested in `fields` and then dropped when
the DataFrame was built, so every verdict wrote unlinked.
"""

import pandas as pd

from testray_analytics.analysis import prepare
from testray_analytics.analysis.testray_writer import FK_FIELD, build_batch


def _fetch(monkeypatch, items):
    monkeypatch.setattr(prepare, "_testray_oauth_token", lambda cfg: "t")
    monkeypatch.setattr(prepare, "_testray_fetch_paginated", lambda *a, **k: items)
    return prepare.fetch_build_caseresults_api(1, {"base_url": "http://x"})


# --- fetch ------------------------------------------------------------------

def test_fetch_keeps_the_caseresult_object_id(monkeypatch):
    df = _fetch(monkeypatch, [{
        "id": 900111, "dueStatus": {"key": "FAILED"}, "errors": "boom",
        "r_caseToCaseResult_c_caseId": 11,
    }])
    assert df.loc[0, "caseresult_id"] == 900111
    assert df.loc[0, "case_id"] == 11          # distinct from the case id


def test_fetch_requests_the_id_field(monkeypatch):
    seen = {}

    def fake_paginated(endpoint, params, **kw):
        seen["fields"] = params["fields"]
        return []

    monkeypatch.setattr(prepare, "_testray_oauth_token", lambda cfg: "t")
    monkeypatch.setattr(prepare, "_testray_fetch_paginated", fake_paginated)
    prepare.fetch_build_caseresults_api(1, {"base_url": "http://x"})
    assert "id" in seen["fields"].split(",")


# --- diff -------------------------------------------------------------------

def _side(case_id, caseresult_id, status, errors=""):
    return {
        "case_id": case_id, "caseresult_id": caseresult_id,
        "case_name": f"T{case_id}", "case_flaky": None,
        "component_name": None, "team_name": None,
        "status": status, "errors": errors, "jira_issue": None,
        "subtask_id": 0,
    }


def test_diff_keeps_the_target_side_caseresult_id():
    """The verdict describes build B's failure, so B's caseResult is the FK."""
    baseline = pd.DataFrame([_side(1, 1000, "PASSED")])
    target   = pd.DataFrame([_side(1, 2000, "FAILED", "boom")])
    out, _ = prepare.compute_test_diff(baseline, target)
    assert out.loc[0, "caseresult_id"] == 2000      # not 1000


def test_caseresult_id_is_an_integer_dtype():
    """Floats would round-trip through diff_list.csv as 2.0e+03 and break the
    FK on read-back."""
    baseline = pd.DataFrame([_side(1, 1000, "PASSED")])
    target   = pd.DataFrame([_side(1, 505505733, "FAILED", "boom")])
    out, _ = prepare.compute_test_diff(baseline, target)
    assert str(out["caseresult_id"].dtype) == "Int64"
    assert "505505733" in out.to_csv(index=False)


def test_diff_without_caseresult_ids_omits_the_column():
    """csv/db sides carry no caseResult id — those verdicts write unlinked
    rather than blowing up."""
    b = _side(1, None, "PASSED"); t = _side(1, None, "FAILED", "boom")
    del b["caseresult_id"]; del t["caseresult_id"]
    out, _ = prepare.compute_test_diff(pd.DataFrame([b]), pd.DataFrame([t]))
    assert "caseresult_id" not in out.columns


# --- through to the write payload ------------------------------------------

def test_diff_output_feeds_the_triageresult_fk():
    baseline = pd.DataFrame([_side(1, 1000, "PASSED")])
    target   = pd.DataFrame([_side(1, 2000, "FAILED", "boom")])
    df, _ = prepare.compute_test_diff(baseline, target)
    df["classification"] = "BUG"
    df["confidence"] = "high"
    df["reason"] = "because"

    items = build_batch(df, {"build_id_b": 42, "mode": "per-test"}, "test")
    assert items[0][FK_FIELD] == 2000
