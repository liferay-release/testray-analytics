"""
Tests for the prefilled Jira draft links in report.html.

Three fields cannot be inferred at render time and were missing:
  - `parent`   — the release's triage parent ticket (changes per release)
  - `reporter` — an Atlassian accountId; the legacy CreateIssueDetails endpoint
                 does NOT auto-fill Reporter, so without it every draft opens
                 with an empty Reporter field
  - `labels`   — so triage drafts are filterable in Jira. NOTE (2026-08-18):
                 the label no longer defaults. Like parent and reporter it is
                 per-run, and an always-on default would tag every draft from
                 every release identically.

Precedence: --jira-parent > run.yml `jira_parent` > config.yml `jira.parent`.
"""

import urllib.parse

import pandas as pd

from testray_analytics.analysis.jira_settings import (
    DEFAULT_LABEL, resolve_jira_settings,
)
from testray_analytics.analysis.report import render_run

META = {
    "run_id": "r", "build_id_a": 1, "build_id_b": 2, "routine_id": 82964,
    "classifier": "api:test", "mode": "by-subtask", "testflow_id": 999,
}
ACCOUNT = "557058:fa80bc56-9933-4ce6-a738-f92c755deff4"


def _row(cid=1, sid=555):
    return dict(
        testray_case_id=cid, subtask_id=sid, test_case=f"Test{cid}",
        component_name="Objects", team_name="BPM", status_a="PASSED",
        status_b="FAILED", transition="NEW_FAILURE", classification="BUG",
        confidence="high", culprit_file="Foo.java", specific_change="changed x",
        reason="because", error_message="boom", baseline_error_message="",
    )


def _jira_params(tmp_path, meta):
    render_run(tmp_path, pd.DataFrame([_row()]), meta)
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    href = html.split('class="jira-create" href="')[1].split('"')[0]
    import html as H
    return urllib.parse.parse_qs(urllib.parse.urlparse(H.unescape(href)).query)


# --- settings resolution ---------------------------------------------------

def test_parent_and_reporter_come_from_config():
    cfg = {"jira": {"parent": "LPD-100696", "reporter_account_id": ACCOUNT}}
    s = resolve_jira_settings(cfg)
    assert s["parent"] == "LPD-100696"
    assert s["reporter_account_id"] == ACCOUNT
    # No default: an unset label is omitted from the link entirely.
    assert s["label"] == ""


def test_run_yml_overrides_config_parent():
    cfg = {"jira": {"parent": "LPD-1"}}
    assert resolve_jira_settings(cfg, {"jira_parent": "LPD-2"})["parent"] == "LPD-2"


def test_cli_override_beats_everything():
    cfg = {"jira": {"parent": "LPD-1"}}
    s = resolve_jira_settings(cfg, {"jira_parent": "LPD-2"},
                              parent_override="LPD-3")
    assert s["parent"] == "LPD-3"


def test_reporter_resolved_from_credentials_when_not_set(monkeypatch):
    import testray_analytics.analysis.jira_settings as js
    monkeypatch.setattr(js, "fetch_jira_account_id",
                        lambda b, e, t: "resolved-id")
    cfg = {"jira": {"base_url": "https://x", "email": "a@b.c",
                    "api_token": "tok"}}
    assert js.resolve_jira_settings(cfg)["reporter_account_id"] == "resolved-id"


def test_explicit_account_id_skips_the_api_call(monkeypatch):
    import testray_analytics.analysis.jira_settings as js

    def boom(*a):
        raise AssertionError("should not call the API when the id is set")

    monkeypatch.setattr(js, "fetch_jira_account_id", boom)
    cfg = {"jira": {"reporter_account_id": ACCOUNT, "email": "a@b.c",
                    "api_token": "tok"}}
    assert js.resolve_jira_settings(cfg)["reporter_account_id"] == ACCOUNT


def test_missing_config_yields_empty_fields():
    s = resolve_jira_settings({})
    assert s["parent"] == "" and s["reporter_account_id"] == ""
    assert s["label"] == ""


def test_explicit_label_is_carried():
    """DEFAULT_LABEL is the suggested value, not an automatic one."""
    s = resolve_jira_settings({"jira": {"label": DEFAULT_LABEL}})
    assert s["label"] == DEFAULT_LABEL


def test_unreachable_jira_leaves_reporter_blank(monkeypatch):
    """A failed lookup must not send a blank Reporter — the param is omitted."""
    import testray_analytics.analysis.jira_settings as js
    monkeypatch.setattr(js.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no net")))
    assert js.fetch_jira_account_id("https://x", "a@b.c", "tok") == ""


# --- rendered link ---------------------------------------------------------

def test_link_carries_parent_reporter_and_label(tmp_path):
    meta = dict(META, jira={"parent": "LPD-100696",
                            "reporter_account_id": ACCOUNT,
                            "label": DEFAULT_LABEL})
    p = _jira_params(tmp_path, meta)
    assert p["parent"] == ["LPD-100696"]
    assert p["reporter"] == [ACCOUNT]
    assert p["labels"] == [DEFAULT_LABEL]
    assert p["pid"] == ["11106"]
    assert p["issuetype"] == ["10002"]


def test_blank_fields_are_omitted_not_sent_empty(tmp_path):
    """Sending parent= or reporter= empty would read as a deliberate clear."""
    meta = dict(META, jira={"parent": "", "reporter_account_id": "", "label": ""})
    p = _jira_params(tmp_path, meta)
    assert "parent" not in p
    assert "reporter" not in p
    assert "labels" not in p      # per-run, like the other two


def test_label_is_sent_when_supplied(tmp_path):
    meta = dict(META, jira={"label": DEFAULT_LABEL})
    assert _jira_params(tmp_path, meta)["labels"] == [DEFAULT_LABEL]


def test_no_jira_block_still_renders_a_usable_link(tmp_path):
    p = _jira_params(tmp_path, META)
    assert p["pid"] == ["11106"]
    assert "summary" in p and "description" in p


def test_description_includes_testray_link_and_error(tmp_path):
    meta = dict(META, jira={"parent": "LPD-100696"})
    p = _jira_params(tmp_path, meta)
    desc = p["description"][0]
    assert "testflow/999/subtasks/555" in desc
    assert "{code}" in desc
    assert "Claude reasoning" in desc


def test_long_fields_are_capped(tmp_path):
    from testray_analytics.analysis.report import (
        _JIRA_DESC_MAX, _JIRA_SUMMARY_MAX,
    )
    row = _row()
    row["test_case"] = "T" * 500
    row["reason"] = "R" * 9000
    render_run(tmp_path, pd.DataFrame([row]), META)
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    import html as H
    href = H.unescape(html.split('class="jira-create" href="')[1].split('"')[0])
    p = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    assert len(p["summary"][0]) <= _JIRA_SUMMARY_MAX
    assert len(p["description"][0]) <= _JIRA_DESC_MAX
