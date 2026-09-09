"""
Tests for the Slack message Jenkins posts.

The message is the only part of an unattended run a person actually sees, so
the cases pinned here are the ones where a rendering slip would mislead the
channel rather than merely look wrong:

  - a verdict must carry the DISPLAY label (a low-confidence NEEDS_REVIEW is
    "not attributable", not "153 things for you to review");
  - a candidate the classifier said does NOT explain the failure must not be
    rendered as the cause — that is someone's name against a broken build;
  - a truncated cluster list must say so, or it reads as the whole story;
  - the commit range must link to the routine's own repo, since Stable's
    commits are not in liferay/liferay-portal yet.
"""

import csv
import json

import yaml

from testray_analytics.analysis import slack_message as S

STABLE_META = {
    "routine_id": 79529,
    "project_id": 35392,
    "testray_url": "https://testray.liferay.com/web/testray",
    "repo_slug": "brianchandotcom/liferay-portal",
    "git_hash_a": "aaaaaaaaaaaa1111",
    "git_hash_b": "bbbbbbbbbbbb2222",
    "build_id_a": 111,
    "build_id_b": 222,
    "build_a_name": "[master] ci:test:stable - 19225",
    "build_b_name": "[master] ci:test:stable - 19228",
    "total_failures": 12,
}


def bundle(tmp_path, results, clusters=None, meta=None):
    """A minimal run bundle: the three files the renderer reads."""
    d = tmp_path / "r_test"
    d.mkdir()
    (d / "run.yml").write_text(yaml.safe_dump({**STABLE_META, **(meta or {})}))
    (d / "results.json").write_text(json.dumps(
        {"run_id": "r_test", "classifier": "claude-code", "results": results}))

    rows = clusters or [{"group_id": r.get("group_id"), "case_count": 1,
                         "member_test_cases": f"SomeTest#case{i}",
                         "components": "Calendar", "signature": "boom"}
                        for i, r in enumerate(results)]
    fields = ["group_id", "case_count", "member_test_cases", "components",
              "signature"]
    with (d / "diff_list_subtasks.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})
    return d


def verdict(group_id, classification, confidence="high", **extra):
    return {"group_id": group_id, "classification": classification,
            "confidence": confidence, "reason": "because", **extra}


def test_a_bug_raises_the_siren(tmp_path):
    text = S.render(bundle(tmp_path, [verdict(1, "BUG")]))
    assert text.startswith("🚨 ")
    assert "*Verdict:* Bug · 🟢 confidence high" in text


def test_nothing_actionable_does_not_raise_the_siren(tmp_path):
    """A run whose verdicts are all FALSE_POSITIVE is news, but not an alarm."""
    text = S.render(bundle(tmp_path, [verdict(1, "FALSE_POSITIVE")]))
    assert text.startswith("🔍 ")


def test_low_confidence_needs_review_reads_as_not_attributable(tmp_path):
    """The stored verdict stays NEEDS_REVIEW; the channel must see the label
    the report and the Testray index show."""
    text = S.render(bundle(tmp_path, [verdict(1, "NEEDS_REVIEW", "low")]))
    assert "Not Attributable" in text
    assert "*Analysis:* Not Attributable 1." in text
    # Nobody attributed it, so the circle is white rather than a confidence
    # colour — 🔴 would read as "high-severity bug".
    assert "⚪" in text and "🔴" not in text


def test_a_candidate_that_does_not_explain_is_a_lead_not_a_cause(tmp_path):
    text = S.render(bundle(tmp_path, [verdict(
        1, "NEEDS_REVIEW", "medium",
        candidates=[{"commit": "d31b08e1d96d6", "ticket": "LPD-99715",
                     "author": "Someone", "why": "touches the area",
                     "explains": False}])]))
    assert "*Closest in range:*" in text
    assert "*Likely cause:*" not in text


def test_a_candidate_that_explains_is_named_as_the_cause(tmp_path):
    text = S.render(bundle(tmp_path, [verdict(
        1, "BUG", "high",
        candidates=[{"commit": "d31b08e1d96d6", "ticket": "LPD-99715",
                     "author": "Someone", "why": "deleted the import",
                     "explains": True}])]))
    assert "*Likely cause:*" in text
    assert "LPD-99715" in text and "Someone" in text


def test_commit_links_use_the_routines_own_repo(tmp_path):
    """Stable's commits are in the control repo; a link to liferay/ 404s."""
    text = S.render(bundle(tmp_path, [verdict(
        1, "BUG", candidates=[{"commit": "abc123456789", "explains": True}])]))
    assert "github.com/brianchandotcom/liferay-portal/compare/" in text
    assert "github.com/brianchandotcom/liferay-portal/commit/" in text


def test_extra_clusters_are_counted_not_silently_dropped(tmp_path):
    results = [verdict(i, "POSSIBLE_BUG") for i in range(S.MAX_BLOCKS + 3)]
    text = S.render(bundle(tmp_path, results))
    assert text.count("*Failure --- ") == S.MAX_BLOCKS
    assert "3 further cluster(s) not shown" in text


def test_worst_verdict_is_reported_first(tmp_path):
    text = S.render(bundle(tmp_path, [verdict(1, "FALSE_POSITIVE"),
                                      verdict(2, "BUG")]))
    first = text.index("*Failure --- 1:*")
    assert "Bug" in text[first:text.index("*Failure --- 2:*")]


def test_a_run_with_no_verdicts_says_so(tmp_path):
    """Silence is indistinguishable from success under unattended operation."""
    text = S.render(bundle(tmp_path, []))
    assert "no verdicts were produced" in text
    assert "Needs a human." in text


def test_report_url_is_linked_when_given(tmp_path):
    d = bundle(tmp_path, [verdict(1, "BUG")])
    assert "*Full Report:*" not in S.render(d)
    assert "*Full Report:* <https://x/report|report>" in S.render(
        d, report_url="https://x/report")


def test_parens_in_urls_are_encoded(tmp_path):
    """Slack ends a link at the first ')' — Jenkins job names are full of them."""
    d = bundle(tmp_path, [verdict(1, "BUG")],
               meta={"testray_url": "https://tr/web/t(master)"})
    text = S.render(d)
    assert "%28master%29" in text


def test_write_creates_the_path_jenkins_reads(tmp_path):
    out = tmp_path / "slack" / "testray_analyzer_slack_message.txt"
    path = S.write(bundle(tmp_path, [verdict(1, "BUG")]), out=out)
    assert path == out and out.read_text().startswith("🚨 ")


def test_reasoning_is_labelled_like_the_report_column(tmp_path):
    text = S.render(bundle(tmp_path, [verdict(1, "BUG")]))
    assert "> *Reasoning:* because" in text


def test_the_testray_link_is_opt_in(tmp_path):
    """The triage view reads TriageResult rows. Linking it before the upsert
    lands — or on an instance with no analytics CX — sends the channel to an
    empty page, so the caller has to say the verdicts are there."""
    d = bundle(tmp_path, [verdict(1, "BUG")])
    assert "*Triage Report:*" not in S.render(d)

    text = S.render(d, link_testray=True)
    assert ("*Triage Report:* <https://testray.liferay.com/web/testray"
            "/triage?buildId=222|open in Testray>") in text


def test_the_testray_link_needs_a_build_and_a_base(tmp_path):
    d = bundle(tmp_path, [verdict(1, "BUG")], meta={"testray_url": ""})
    assert "*Triage Report:*" not in S.render(d, link_testray=True)
