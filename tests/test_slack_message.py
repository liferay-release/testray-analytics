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
              "signature", "console_url"]
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


AXIS_CONSOLE = ("https://storage.cloud.google.com/testray-results/2026-09/"
                "test-1-42/test-portal-testsuite-upstream(master)/1493/"
                "modules-integration-postgresql163_stable/0/0/"
                "jenkins-console.txt.gz?authuser=0")


def test_the_console_row_names_the_axis_and_escapes_its_parens(tmp_path):
    """The console link is the one a reader opens when the verdict explains
    nothing, so it has to be both labelled and clickable: labelled with the
    axis, because that is how the same failure is named in Jenkins, and with
    its parens encoded, because Slack ends a link at the first `)` and the job
    name has two."""
    d = bundle(tmp_path, [verdict(1, "BUG")],
               clusters=[{"group_id": "1", "case_count": 1,
                          "member_test_cases": "SomeTest#case0",
                          "components": "Calendar", "signature": "boom",
                          "console_url": AXIS_CONSOLE}])
    text = S.render(d)
    assert "> *Console:* <" in text
    assert "|modules-integration-postgresql163_stable/0/0>" in text
    assert "test-portal-testsuite-upstream%28master%29" in text
    assert "upstream(master)" not in text


def test_no_console_row_when_the_cluster_has_no_axis_console(tmp_path):
    """`Top Level Build` carries only the TOP-LEVEL console, which prepare
    deliberately declines to record: its deepest message on a broken build is
    `Timeout waiting for update`, and offering that as *the* log confirms the
    "this is CI, not a commit" reading the classifier already reaches wrongly.
    An absent row is the honest rendering."""
    d = bundle(tmp_path, [verdict(1, "NEEDS_REVIEW")],
               clusters=[{"group_id": "1", "case_count": 1,
                          "member_test_cases": "Top Level Build",
                          "components": "Batch", "signature": "",
                          "console_url": ""}])
    assert "*Console:*" not in S.render(d)


def test_the_cause_is_never_truncated(tmp_path):
    """A cause cut mid-sentence loses the mechanism, which is the only part a
    reader can use to judge whether the attribution is credible — and a
    half-stated cause reads as a confident one."""
    why = ("BundleSiteInitializerTest.setUp invokes the listener without "
           "enabling the flag, so _validate re-checks it, finds it false and "
           "throws RoleSubtypeException, which the listener logs at ERROR and "
           "PortalLogAssertorTest then fails on. " * 3).strip()
    r = verdict(1, "BUG")
    r["candidates"] = [{"commit": "0c18989a6757c", "ticket": "LPD-103976",
                        "explains": True, "why": why}]
    text = S.render(bundle(tmp_path, [r]))
    assert why in text
    assert "…" not in text.split("*Reasoning:*")[0]


# --- "Still failing": the build that inherited every one of its failures -----

from testray_analytics.analysis.recurrence import Repeat


def _repeat(**kw):
    base = dict(cluster_key="v3:aaaa", occurrences=2, builds_ago=1,
                first_build_id=524997126,
                first_build_name="[master] ci:test:stable - 19704 - "
                                 "2026-09-15[08:06:43]",
                first_build_time="", test_name="semantic-versioning/0/0",
                error="boom")
    base.update(kw)
    return Repeat(**base)


META = {"routine_id": 79529, "project_id": 35392,
        "testray_url": "https://testray.liferay.com/web/testray"}


def test_a_repeat_names_the_build_it_started_in():
    out = "\n".join(S._still_failing(META, {"1": _repeat()}))

    assert "Still failing" in out
    assert "occurrence #2" in out
    assert "19704" in out, "the reader needs the build, not just a count"


def test_a_repeat_carries_the_verdict_from_when_it_was_analysed():
    """The point of the block: 'this is old AND here is what we decided'."""
    out = "\n".join(S._still_failing(META, {"1": _repeat(
        prior_verdict="POSSIBLEBUG",
        prior_culprit="modules/apps/mcp/mcp-server-rest-impl/ToolSetUtil.java",
        prior_reason="compileJava FAILED on a module rewritten in range.")}))

    assert "Likely cause" in out
    assert "ToolSetUtil.java" in out
    assert "compileJava FAILED" in out


def test_a_repeat_with_no_verdict_says_so_rather_than_going_quiet():
    """Silence reads as a missing answer; the exclusion was deliberate."""
    out = "\n".join(S._still_failing(META, {"1": _repeat()}))

    assert "No verdict on file" in out
    assert "Likely cause" not in out


def test_no_repeats_renders_nothing():
    assert S._still_failing(META, {}) == []


def test_the_build_aggregate_row_is_never_a_repeat():
    """`Top Level Build` is Testray's whole-build row, not a test. Matched on
    the label because its case id differs per instance."""
    assert S._is_aggregate_row("Top Level Build")
    assert S._is_aggregate_row("top level build")
    assert not S._is_aggregate_row("semantic-versioning/0/0")


# --------------------------------------------------------------------------
# One post per TICK, not per pair
#
# `watch` drains N queued pairs through N separate submits while Jenkins posts
# the file once, after the whole tick. Overwriting therefore loses every pair
# but the last: on 2026-09-16 a tick analysed 19764 (a segments compile break,
# BUG) and then 19757 (infra, FALSE_POSITIVE), and only the false positive
# reached the channel.
# --------------------------------------------------------------------------

def test_a_second_run_overwrites_by_default(tmp_path):
    """A hand-run pipeline still replaces the file — one run, one message."""
    out = tmp_path / "msg.txt"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    S.write(bundle(tmp_path / "a", [verdict(1, "BUG")]), out=out)
    S.write(bundle(tmp_path / "b", [verdict(1, "FALSE_POSITIVE")]), out=out)

    text = out.read_text(encoding="utf-8")
    assert S.RUN_SEPARATOR not in text
    assert text.startswith("🔍 ")


def test_appending_keeps_the_earlier_pair_in_the_post(tmp_path):
    """The BUG analysed first must survive the FALSE_POSITIVE analysed after."""
    out = tmp_path / "msg.txt"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    S.write(bundle(tmp_path / "a", [verdict(1, "BUG")]), out=out, append=True)
    S.write(bundle(tmp_path / "b", [verdict(1, "FALSE_POSITIVE")]),
            out=out, append=True)

    text = out.read_text(encoding="utf-8")
    assert text.count(S.RUN_SEPARATOR) == 1
    assert "*Verdict:* Bug · 🟢 confidence high" in text
    assert "False Positive" in text
    # The siren belongs to the pair that earned it, so the reader still sees
    # it even though the tick ended on something unremarkable.
    assert text.startswith("🚨 ")


def test_appending_to_nothing_writes_a_plain_message(tmp_path):
    """First pair of a tick: no separator, no leading blank lines."""
    out = tmp_path / "msg.txt"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    S.write(bundle(tmp_path / "a", [verdict(1, "BUG")]), out=out, append=True)

    text = out.read_text(encoding="utf-8")
    assert S.RUN_SEPARATOR not in text
    assert text.startswith("🚨 ")


def test_appending_ignores_a_leftover_empty_file(tmp_path):
    """`ensure_slack_fallback` and a reset both leave an empty file behind."""
    out = tmp_path / "msg.txt"
    out.write_text("   \n", encoding="utf-8")
    (tmp_path / "a").mkdir()

    S.write(bundle(tmp_path / "a", [verdict(1, "BUG")]), out=out, append=True)

    text = out.read_text(encoding="utf-8")
    assert S.RUN_SEPARATOR not in text
    assert text.startswith("🚨 ")


# --------------------------------------------------------------------------
# The tick that analysed nothing
#
# Every pair red in the build was already analysed, so scan queues nothing,
# watch runs no pipeline and submit never executes. Stable posts on every
# failure, so this build still has to be answered — with the run that already
# explained it, not with the job's bare "nothing was submitted".
# --------------------------------------------------------------------------

def _recurrence_repeat(**kw):
    from testray_analytics.analysis.recurrence import Repeat
    base = dict(cluster_key="v3:abc", occurrences=2, builds_ago=1,
                first_build_id=525801997,
                first_build_name="[master] ci:test:stable - 19764",
                first_build_time="", test_name="modules-compile/0/1",
                error="Compilation failed")
    base.update(kw)
    return Repeat(**base)


def test_the_recurrence_message_never_raises_the_siren():
    """Nothing here is new, so it must not compete with the build that
    introduced the failure."""
    text = S.render_recurrence(
        {**STABLE_META, "build_id_b": 525834113,
         "build_b_name": "[master] ci:test:stable - 19766"},
        {"1": _recurrence_repeat()})
    assert text.startswith("🔁 ")
    assert "🚨" not in text


def test_the_recurrence_message_names_the_run_that_explained_it():
    text = S.render_recurrence(
        {**STABLE_META, "build_id_b": 525834113},
        {"1": _recurrence_repeat(prior_verdict="BUG",
                      prior_culprit="SegmentsEntryLocalServiceTest.java",
                      prior_reason="the 8-arg overload was removed")})
    assert "occurrence #2" in text
    assert "SegmentsEntryLocalServiceTest.java" in text
    assert "the 8-arg overload was removed" in text
    # The build that first carried it, so the reader can open that analysis.
    assert "19764" in text


def test_the_recurrence_message_says_so_when_nothing_traced():
    """A header with no body reads as a message that failed to render."""
    text = S.render_recurrence({**STABLE_META, "build_id_b": 525834113}, {})
    assert "No recurring signature" in text


def test_the_recurrence_message_lists_every_repeat_not_just_the_first():
    """`lookup` used to rebind `prior` — its list of predecessor builds — to a
    verdict dict on the first probe that emitted, so every later probe sliced a
    dict, threw, and was swallowed by the walk's `except Exception: continue`.
    The section could show exactly one repeat however many were recurring.
    """
    repeats = {
        "1": _recurrence_repeat(cluster_key="v3:aaa",
                                test_name="modules-compile/0/1"),
        "2": _recurrence_repeat(cluster_key="v3:bbb",
                                test_name="modules-compile[modules/apps/segments]"),
    }
    text = S.render_recurrence(
        {**STABLE_META, "build_id_b": 525834113}, repeats)

    assert "modules-compile/0/1" in text
    assert "modules-compile[modules/apps/segments]" in text
    assert text.count("occurrence #") == 2
