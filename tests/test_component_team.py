"""
Tests for component/team name resolution.

§15 recorded component/team as blank on every api-sourced row. The cause was
not a missing API field: `fetch_build_caseresults_api` already *requested*
`r_componentToCaseResult_c_componentId` / `r_teamToCaseResult_c_teamId`, but
never read them out of the response — so the columns were hard-coded to None
and every report rendered "—".

Two regressions guarded here:
  - the FKs must survive the fetch and the diff merge (target side wins)
  - the local component→team map must not override the team Testray recorded
"""

import pandas as pd
import pytest

from testray_analytics.analysis import prepare


# --- the FKs must be read out of the response ------------------------------

def test_caseresult_fetch_reads_component_and_team_fks(monkeypatch):
    items = [{
        "id": 900, "dueStatus": {"key": "FAILED"}, "errors": "boom",
        "r_caseToCaseResult_c_caseId": 11,
        "r_componentToCaseResult_c_componentId": 35482,
        "r_teamToCaseResult_c_teamId": 275339599,
        "r_subtaskToCaseResults_c_subtaskId": 77,
    }]
    monkeypatch.setattr(prepare, "_testray_oauth_token", lambda cfg: "t")
    monkeypatch.setattr(prepare, "_testray_fetch_paginated",
                        lambda *a, **k: items)
    df = prepare.fetch_build_caseresults_api(1, {"base_url": "http://x"})
    assert df.loc[0, "component_id"] == 35482
    assert df.loc[0, "team_id"] == 275339599


def test_missing_fks_become_zero_not_nan(monkeypatch):
    """0 is the 'no link' sentinel the resolver filters on."""
    items = [{"id": 1, "dueStatus": {"key": "FAILED"}, "errors": "",
              "r_caseToCaseResult_c_caseId": 2}]
    monkeypatch.setattr(prepare, "_testray_oauth_token", lambda cfg: "t")
    monkeypatch.setattr(prepare, "_testray_fetch_paginated", lambda *a, **k: items)
    df = prepare.fetch_build_caseresults_api(1, {"base_url": "http://x"})
    assert df.loc[0, "component_id"] == 0
    assert df.loc[0, "team_id"] == 0


# --- the FKs must survive the diff merge ----------------------------------

def _side(case_id, status, errors, comp, team):
    return {
        "case_id": case_id, "caseresult_id": case_id * 10,
        "case_name": f"T{case_id}", "case_flaky": None,
        "component_name": None, "team_name": None,
        "status": status, "errors": errors, "jira_issue": None,
        "subtask_id": 0, "component_id": comp, "team_id": team,
    }


def test_diff_keeps_target_side_component_and_team():
    """Build B is what the report describes, so its FKs must win."""
    baseline = pd.DataFrame([_side(1, "PASSED", "", 111, 222)])
    target = pd.DataFrame([_side(1, "FAILED", "boom", 999, 888)])
    df, _ = prepare.compute_test_diff(baseline, target)
    assert df.loc[0, "component_id"] == 999
    assert df.loc[0, "team_id"] == 888


# --- resolution ------------------------------------------------------------

def test_resolve_fills_names_from_distinct_ids(monkeypatch):
    calls = {"components": [], "teams": []}

    def fake_components(ids, cfg):
        calls["components"].append(sorted(ids))
        return {35482: "Batch", 35483: "Objects"}

    def fake_teams(ids, cfg):
        calls["teams"].append(sorted(ids))
        return {275339599: "Shared"}

    monkeypatch.setattr(prepare, "fetch_component_metadata", fake_components)
    monkeypatch.setattr(prepare, "fetch_team_metadata", fake_teams)

    df = pd.DataFrame({
        "component_id": [35482, 35482, 35483],
        "team_id": [275339599, 275339599, 275339599],
    })
    out = prepare.resolve_component_team_names(df, {})
    assert list(out["testray_component_name"]) == ["Batch", "Batch", "Objects"]
    assert list(out["team_name"]) == ["Shared"] * 3
    # One lookup per DISTINCT id, not per row.
    assert calls["components"] == [[35482, 35483]]
    assert calls["teams"] == [[275339599]]


def test_unresolvable_id_leaves_name_blank(monkeypatch):
    monkeypatch.setattr(prepare, "fetch_component_metadata", lambda i, c: {})
    monkeypatch.setattr(prepare, "fetch_team_metadata", lambda i, c: {})
    df = pd.DataFrame({"component_id": [1], "team_id": [2]})
    out = prepare.resolve_component_team_names(df, {})
    assert pd.isna(out["testray_component_name"].iloc[0])


def test_resolve_does_not_clobber_existing_names(monkeypatch):
    monkeypatch.setattr(prepare, "fetch_component_metadata",
                        lambda i, c: {5: "FromApi"})
    monkeypatch.setattr(prepare, "fetch_team_metadata", lambda i, c: {6: "ApiTeam"})
    df = pd.DataFrame({
        "component_id": [5], "team_id": [6],
        "testray_component_name": ["AlreadySet"], "team_name": ["AlreadyTeam"],
    })
    out = prepare.resolve_component_team_names(df, {})
    assert out["testray_component_name"].iloc[0] == "AlreadySet"
    assert out["team_name"].iloc[0] == "AlreadyTeam"


def test_zero_ids_are_skipped(monkeypatch):
    seen = {}

    def fake_components(ids, cfg):
        seen["ids"] = ids
        return {}

    monkeypatch.setattr(prepare, "fetch_component_metadata", fake_components)
    monkeypatch.setattr(prepare, "fetch_team_metadata", lambda i, c: {})
    prepare.resolve_component_team_names(
        pd.DataFrame({"component_id": [0, 7], "team_id": [0, 0]}), {})
    assert seen["ids"] == [7]


def test_empty_frame_is_a_noop():
    out = prepare.resolve_component_team_names(pd.DataFrame(), {})
    assert out.empty


# --- the map must not override real Testray data --------------------------

def test_component_team_map_does_not_override_testray_team(monkeypatch):
    """The local component→team map used to run last and silently replace the
    team Testray had recorded on the caseresult."""
    monkeypatch.setattr(prepare.prompt_helpers, "load_triage_config", lambda: {})
    monkeypatch.setattr(prepare.prompt_helpers, "team_for_component",
                        lambda c: "MappedTeam")
    monkeypatch.setattr(prepare.prompt_helpers, "pre_classify", lambda e, p: None)

    df = pd.DataFrame({
        "testray_component_name": ["Objects", "Batch"],
        "team_name": ["RealTestrayTeam", None],
        "error_message": ["boom", "boom"],
    })
    out = prepare.enrich_and_pre_classify(df)
    assert out.loc[0, "team_name"] == "RealTestrayTeam"
    # Still falls back to the map where Testray gave nothing.
    assert out.loc[1, "team_name"] == "MappedTeam"


def test_fills_an_all_nan_float_column(monkeypatch):
    """The shape every real api bundle has: prepare seeds component_name /
    team_name as all-None, so pandas types them float64. Assigning strings
    in-place there raises `TypeError: Invalid value '<StringArray>...' for
    dtype 'float64'` on pandas >= 2.2 — which is why this failed on live data
    while the string-column tests above passed."""
    monkeypatch.setattr(prepare, "fetch_component_metadata",
                        lambda i, c: {5: "Batch"})
    monkeypatch.setattr(prepare, "fetch_team_metadata",
                        lambda i, c: {6: "Shared"})
    df = pd.DataFrame({
        "component_id": [5, 5],
        "team_id": [6, 6],
        # exactly what `[None] * len(items)` produces after a round-trip
        "testray_component_name": pd.Series([None, None], dtype="float64"),
        "team_name": pd.Series([None, None], dtype="float64"),
    })
    out = prepare.resolve_component_team_names(df, {})
    assert list(out["testray_component_name"]) == ["Batch", "Batch"]
    assert list(out["team_name"]) == ["Shared", "Shared"]


def test_fills_a_column_of_literal_nan_strings(monkeypatch):
    """A CSV round-trip turns NaN into the string 'nan' — it must still count
    as blank, or a re-render keeps showing 'nan' in the Component column."""
    monkeypatch.setattr(prepare, "fetch_component_metadata",
                        lambda i, c: {5: "Batch"})
    monkeypatch.setattr(prepare, "fetch_team_metadata", lambda i, c: {})
    df = pd.DataFrame({"component_id": [5], "team_id": [0],
                       "testray_component_name": ["nan"]})
    out = prepare.resolve_component_team_names(df, {})
    assert out["testray_component_name"].iloc[0] == "Batch"
