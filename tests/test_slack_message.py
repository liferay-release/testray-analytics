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
    # The verdict picks the siren and the order. It is never printed — see
    # test_no_verdict_label_reaches_the_channel.
    assert "Bug" not in text


def test_nothing_actionable_does_not_raise_the_siren(tmp_path):
    """A run whose verdicts are all FALSE_POSITIVE is news, but not an alarm."""
    text = S.render(bundle(tmp_path, [verdict(1, "FALSE_POSITIVE")]))
    assert text.startswith("🔍 ")


def test_no_verdict_label_reaches_the_channel(tmp_path):
    """No census, no per-block label, for any verdict.

    "False Positive 1" was read in the channel as permission to stop reading,
    so a label people glossed over decided whether the report was opened at
    all. The verdict still orders the blocks and still picks the siren; it is
    simply never rendered as text. It belongs where it can be weighed against
    the evidence — the report, and Testray.
    """
    for classification in ("BUG", "POSSIBLE_BUG", "TEST_FIX",
                           "FALSE_POSITIVE", "NEEDS_REVIEW"):
        d = tmp_path / classification
        d.mkdir()
        text = S.render(bundle(d, [verdict(1, classification)]))
        for banned in ("Bug", "Possible Bug", "Test Fix", "False Positive",
                       "Needs Review", "Not Attributable", "*Verdict:*",
                       "*Analysis:*"):
            assert banned not in text, f"{banned!r} leaked for {classification}"


def test_a_low_confidence_verdict_shows_no_confidence_either(tmp_path):
    """Confidence only ever appeared inside the verdict row, and a bare 🔴
    reads as "high-severity bug" to someone who has lost the legend."""
    text = S.render(bundle(tmp_path, [verdict(1, "NEEDS_REVIEW", "low")]))
    for circle in ("🟢", "🟡", "🔴", "⚪"):
        assert circle not in text


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
    """Still true with the label gone — the verdict decides what a reader sees
    FIRST, it just no longer gets to be what they see INSTEAD. Checked through
    the cluster each block names, since the verdict itself is not printed."""
    text = S.render(bundle(tmp_path, [verdict(1, "FALSE_POSITIVE"),
                                      verdict(2, "BUG")],
                           clusters=[
                               {"group_id": 1, "case_count": 1,
                                "member_test_cases": "TheFalsePositive"},
                               {"group_id": 2, "case_count": 1,
                                "member_test_cases": "TheBug"}]))
    block1 = text[text.index("*Failure --- 1:*"):text.index("*Failure --- 2:*")]
    assert "TheBug" in block1
    assert "TheFalsePositive" not in block1


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


REASONING = ("The build-services check fails because the service-builder "
             "verify step produces an uncommitted diff. Auto SF re-sorted the "
             "Java terms in BlogsEntryServiceHttp.java, so the regenerated "
             "file differs from the committed one.")


def test_reasoning_is_cut_to_one_sentence_with_a_link_to_the_rest(tmp_path):
    """The channel gets the conclusion; the report keeps the argument.

    A reasoning paragraph in Slack was read as the answer, because none of the
    evidence that makes it checkable — the diff, the error text — is beside it
    there. One sentence and a link is the same information with the weight put
    back where it can be judged.
    """
    d = bundle(tmp_path, [verdict(1, "BUG", reason=REASONING)])
    text = S.render(d, report_url="https://x/report")

    assert ("> *Reasoning:* The build-services check fails because the "
            "service-builder verify step produces an uncommitted diff. "
            "<https://x/report|Read more…>") in text
    assert "Auto SF re-sorted" not in text


def test_the_read_more_link_prefers_the_testray_triage_view(tmp_path):
    """Same order as the two report rows in the header: Testray is where the
    team already works and where the verdicts live."""
    d = bundle(tmp_path, [verdict(1, "BUG", reason=REASONING)])
    text = S.render(d, report_url="https://x/report", link_testray=True)
    assert ("<https://testray.liferay.com/web/testray/triage?buildId=222"
            "|Read more…>") in text
    assert "|Read more…>" in text and "https://x/report|Read more…" not in text


def test_a_one_sentence_reasoning_gets_no_read_more(tmp_path):
    """A link that adds nothing teaches the channel not to follow links."""
    d = bundle(tmp_path, [verdict(1, "BUG", reason="The module was renamed.")])
    text = S.render(d, report_url="https://x/report")
    assert "> *Reasoning:* The module was renamed." in text
    assert "Read more" not in text


def test_reasoning_survives_in_full_when_there_is_nowhere_to_link(tmp_path):
    """No analytics CX and no published report. Cutting the paragraph here
    would delete it, not relocate it."""
    text = S.render(bundle(tmp_path, [verdict(1, "BUG", reason=REASONING)]))
    assert "Read more" not in text
    assert "Auto SF re-sorted" in text


def test_a_dotted_identifier_is_not_a_sentence_boundary(tmp_path):
    """`BlogsEntryServiceHttp.java` and `com.liferay.portal.kernel` used to end
    the first sentence, which put the whole finding behind the link."""
    reason = ("com.liferay.portal.kernel.NoSuchLayoutException is thrown by "
              "LayoutLocalServiceImpl.java at line 412. The commit in range "
              "removed the fallback.")
    d = bundle(tmp_path, [verdict(1, "BUG", reason=reason)])
    text = S.render(d, report_url="https://x/report")
    assert "LayoutLocalServiceImpl.java at line 412." in text
    assert "removed the fallback" not in text


def test_an_abbreviation_does_not_end_the_sentence(tmp_path):
    reason = ("The listener runs without the flag, e.g. When setUp calls it "
              "directly, and _validate then throws. A later commit hid it.")
    d = bundle(tmp_path, [verdict(1, "BUG", reason=reason)])
    text = S.render(d, report_url="https://x/report")
    assert "_validate then throws." in text
    assert "A later commit hid it" not in text


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
        prior_reason="compileJava FAILED on a module rewritten in range.",
        prior_tickets=("LPD-105486",))}))

    assert "Likely cause" in out
    assert "ToolSetUtil.java" in out
    assert "LPD-105486" in out
    assert "liferay.atlassian.net/browse/LPD-105486" in out, \
        "a bare key in Slack is a copy-paste job"


def test_a_repeat_does_not_quote_the_stored_reasoning():
    """This section is an index, not a second copy of the report.

    On 19793 two clusters of one compile break rendered ~300 characters of
    near-identical prose each, pushing the build, the file and the ticket —
    the parts a reader acts on — off the first screen.
    """
    out = "\n".join(S._still_failing(META, {"1": _repeat(
        prior_verdict="BUG",
        prior_culprit="SegmentsEntryLocalServiceTest.java",
        prior_reason="Identical error signature to group 1 — a compile failure "
                     "of ':apps:segments:segments-test' reported through the "
                     "exec wrapper.")}))

    assert "exec wrapper" not in out
    assert "Identical error signature" not in out
    # What replaces it: the run that HAS the reasoning, in one click.
    assert "Original report:" in out
    assert "triage?buildId=524997126" in out


def test_repeats_from_different_builds_each_link_their_own_report():
    """One "Original report" standing for all of them would send a reader to a
    run that never saw the failure they clicked from."""
    out = "\n".join(S._still_failing(META, {
        "1": _repeat(cluster_key="v3:aaa", test_name="a/0/0",
                     first_build_id=524997126),
        "2": _repeat(cluster_key="v3:bbb", test_name="b/0/0",
                     first_build_id=524000111)}))

    assert "Original report:" not in out
    assert "triage?buildId=524997126" in out
    assert "triage?buildId=524000111" in out


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
    # Both pairs are in the post; neither names its verdict.
    assert text.count("*Failure --- 1:*") == 2
    assert "False Positive" not in text
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
                      prior_reason="the 8-arg overload was removed",
                      prior_tickets=("LPD-105486",))})
    assert "occurrence #2" in text
    assert "SegmentsEntryLocalServiceTest.java" in text
    assert "LPD-105486" in text
    # The reasoning lives in the linked report, not in the message.
    assert "the 8-arg overload was removed" not in text
    assert "Original report:" in text
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


# --------------------------------------------------------------------------
# The post has a ceiling
#
# Slack refuses a message over 40,000 characters outright. A tick draining a
# backlog appends one full run message per pair (~5,600 chars measured on a
# real Stable bundle), so around seven pairs the post would stop being sent —
# silence, which is the one outcome worse than a long post.
# --------------------------------------------------------------------------

def test_the_post_stops_growing_before_slack_refuses_it(tmp_path):
    out = tmp_path / "msg.txt"
    out.write_text("x" * (S.MAX_POST_CHARS - 100), encoding="utf-8")

    S.append_to_post(out, "y" * 500)

    text = out.read_text(encoding="utf-8")
    # The budget is a budget; what must never happen is nearing the 40,000
    # Slack actually refuses at.
    assert len(text) < 40_000
    assert "y" * 500 not in text
    assert "1 more pair(s)" in text


def test_the_dropped_count_accumulates_rather_than_resetting(tmp_path):
    """Three pairs dropped must read as 3, not as 1 three times over."""
    out = tmp_path / "msg.txt"
    out.write_text("x" * (S.MAX_POST_CHARS - 100), encoding="utf-8")

    for _ in range(3):
        S.append_to_post(out, "y" * 500)

    text = out.read_text(encoding="utf-8")
    assert "3 more pair(s)" in text
    assert "1 more pair(s)" not in text
    assert "2 more pair(s)" not in text
    assert len(text) < 40_000


def test_appending_under_the_ceiling_is_untouched(tmp_path):
    out = tmp_path / "msg.txt"
    out.write_text("first", encoding="utf-8")

    S.append_to_post(out, "second")

    text = out.read_text(encoding="utf-8")
    assert "first" in text and "second" in text
    assert S.RUN_SEPARATOR in text
    assert "more pair(s)" not in text


# --------------------------------------------------------------------------
# Which field the ticket on a repeat comes from
#
# `suspiciousCommits` is resolved from the commits that really touched the
# culprit file, so a key found there is provably in the pair's range. The
# prose fields can name a ticket the classifier only mentioned.
# --------------------------------------------------------------------------

def test_the_repeat_ticket_prefers_the_git_derived_field():
    from testray_analytics.analysis.recurrence import _tickets

    assert _tickets({"suspiciousCommits": "LPD-105486 (5cbd024, a040008)",
                     "reason": "looks like LPD-999999 to me"}) \
        == ("LPD-105486",)


def test_the_repeat_ticket_falls_back_to_the_prose():
    """A NEEDS_REVIEW verdict names no culprit file, so it has no commits
    either — and those are exactly the rows somebody has to act on."""
    from testray_analytics.analysis.recurrence import _tickets

    assert _tickets({"specificChange": "either LPD-105486 or LPS-200"}) \
        == ("LPD-105486", "LPS-200")


def test_a_verdict_naming_no_ticket_yields_none():
    from testray_analytics.analysis.recurrence import _tickets

    assert _tickets({"reason": "a flaky timeout, no change in range"}) == ()


# --------------------------------------------------------------------------
# The MIXED build: something new, on top of failures that were already there
#
# `_still_failing` only renders when a build inherited every one of its
# failures. A build with one new failure and 63 old ones took the other path
# and said nothing at all about the 63 — the only trace was that "4 classified
# over 69 failure(s)" carried two different numbers.
# --------------------------------------------------------------------------

def test_a_mixed_build_says_how_many_it_inherited(tmp_path):
    text = S.render(bundle(tmp_path, [verdict(1, "BUG")],
                           meta={"transition_counts": {"same_failure": 63,
                                                       "new": 1}}))
    assert "63 failures inherited" in text
    assert "Pre-existing (63)" in text
    # It must not become a second cluster list competing with the new failure.
    assert text.count("inherited") == 1
    assert "Still failing" not in text


def test_the_inherited_line_links_the_report_section(tmp_path):
    text = S.render(bundle(tmp_path, [verdict(1, "BUG")],
                           meta={"transition_counts": {"same_failure": 2}}),
                    report_url="https://example.com/report.html")
    assert "https://example.com/report.html#pre-existing" in text


def test_a_build_that_inherited_nothing_says_nothing(tmp_path):
    text = S.render(bundle(tmp_path, [verdict(1, "BUG")],
                           meta={"transition_counts": {"new": 1}}))
    assert "inherited" not in text


def test_the_inherited_line_stays_out_of_the_all_inherited_message():
    """There the whole `Still failing` section already speaks for them, in
    full. Two sections about the same failures is worse than one."""
    assert S._inherited_note({"transition_counts": {"same_failure": 63}},
                             [], "") == []


def test_a_singular_inherited_failure_reads_as_one(tmp_path):
    text = S.render(bundle(tmp_path, [verdict(1, "BUG")],
                           meta={"transition_counts": {"same_failure": 1}}))
    assert "1 failure inherited" in text


def test_each_repeat_is_separated_from_the_next():
    """Two-line entries run together into one grey wall otherwise, and on a
    six-repeat build the reader cannot see where one ends and the next
    begins."""
    out = S._still_failing(META, {
        "1": _repeat(cluster_key="v3:aaa", test_name="a/0/0"),
        "2": _repeat(cluster_key="v3:bbb", test_name="b/0/0")})

    bullets = [i for i, line in enumerate(out) if line.lstrip().startswith("•")]
    assert len(bullets) == 2
    for i in bullets:
        assert out[i - 1] == "", "each entry needs a blank line before it"


# --- Only the roll-up failed: the real failures never reached Testray -------
#
# `Top Level Build` is FAILED whenever anything under it failed. prepare drops
# it (it is not a test), which leaves nothing to classify — and a run that says
# nothing is indistinguishable from a healthy one. It runs unattended.
#
# Verified on build 20005: Testray held only the roll-up while the build's
# jenkins-report listed two axes at FAILURE with three failed specs. So the
# message must send a reader after the MISSING ROWS, not after a broken build.


def test_a_build_break_says_so_and_asks_for_a_human(tmp_path):
    d = bundle(tmp_path, [], meta={"aggregate_dropped": {"new": 1}})
    text = S.render(d)
    assert "Nothing was submitted this run — but Top Level Build failed." in text
    assert "*Human review required*" in text
    # It must point at the missing rows, not at a broken build: an earlier
    # wording said "this is what a broken build looks like" and sent readers
    # hunting for the wrong thing.
    assert "never made it into Testray" in text
    # and NOT the generic line, which is true of the mechanism and wrong
    # about the build.
    assert "auto-classified or excluded upstream" not in text


def test_a_build_break_raises_the_siren(tmp_path):
    """No verdicts means the rollup is PENDING, which would otherwise pick the
    magnifying glass — the quiet icon, on the one outcome nobody finds any
    other way."""
    d = bundle(tmp_path, [], meta={"aggregate_dropped": {"new": 1}})
    assert S.render(d).startswith("🚨 ")


def test_an_already_broken_build_does_not_re_raise_it(tmp_path, monkeypatch):
    """A same_failure aggregate row means the build was already broken, so it
    gets the 🔁 duration line, never the ⚠️ warning again."""
    from testray_analytics.analysis import recurrence
    monkeypatch.setattr(recurrence, "repeats_for_build", lambda *a, **k: {})
    monkeypatch.setattr("testray_analytics.analysis.prepare.load_config",
                        lambda *a, **k: {"testray": {}})
    d = bundle(tmp_path, [], meta={"aggregate_dropped": {"same_failure": 1}})
    text = S.render(d)
    assert "Top Level Build failed" not in text
    # Walk found nothing, so it falls back rather than inventing a duration.
    assert "auto-classified or excluded upstream" in text


def test_a_lookup_that_cannot_reach_testray_does_not_kill_the_message(tmp_path,
                                                                      monkeypatch):
    """`load_config` raises SystemExit, not Exception. An `except Exception`
    here let it escape and took the whole Slack message down with it."""
    def boom(*a, **k):
        raise SystemExit("OAuth2 token request failed")
    monkeypatch.setattr("testray_analytics.analysis.prepare.load_config", boom)
    d = bundle(tmp_path, [], meta={"aggregate_dropped": {"same_failure": 1}})
    assert "auto-classified or excluded upstream" in S.render(d)


def test_a_run_with_verdicts_is_never_a_build_break(tmp_path):
    """The note replaces the no-verdicts line. A pair that classified
    something has news of its own and must not be buried under a warning."""
    d = bundle(tmp_path, [verdict(1, "BUG")],
               meta={"aggregate_dropped": {"new": 1}})
    assert "Top Level Build failed" not in S.render(d)


def test_a_bundle_with_no_aggregate_key_is_unaffected(tmp_path):
    """Every run.yml written before this field existed."""
    text = S.render(bundle(tmp_path, []))
    assert "Top Level Build failed" not in text
    assert "auto-classified or excluded upstream" in text


# --- A pair nothing will retry ----------------------------------------------

class _Job:
    def __init__(self, name, signatures=()):
        self.name, self.signatures = name, list(signatures)


def test_a_blocked_pair_is_announced_as_a_siren(tmp_path):
    """Unlike the recurrence message, this one competes for attention on
    purpose: the failure has never been explained and, left alone, never
    will be."""
    text = S.render_blocked(
        {**STABLE_META, "build_id_b": 222},
        [(_Job("79529-527680167-527702480", ["v3:ca61ca98281c6797"]), "FAILED")])
    assert text.startswith("🚨 ")
    assert "79529-527680167-527702480" in text, "the pair is what the fix takes"
    assert "*FAILED*" in text
    assert "*Human review required.*" in text


def test_a_blocked_message_does_not_claim_nothing_is_wrong(tmp_path):
    """The line it replaces — "no new failures; each one was analyzed already"
    — is the exact opposite of true for a parked pair."""
    text = S.render_blocked(STABLE_META, [(_Job("79529-1-2"), "FAILED")])
    assert "analyzed already" not in text


def test_many_blocked_pairs_are_capped(tmp_path):
    stuck = [(_Job(f"79529-{i}-{i + 1}"), "FAILED") for i in range(9)]
    text = S.render_blocked(STABLE_META, stuck)
    assert "… and 3 more." in text


# --- Only the roll-up failed: scan speaks, because nothing else can --------
#
# Since the ledger stopped signing the aggregate row, such a build produces no
# signature: nothing queues, no pipeline runs, no bundle exists and submit
# never executes. `scan` is the last place that sees it, so the renderer lives
# here and scan calls it.


def test_the_rollup_only_message_names_where_it_goes_back_to_without_a_count():
    """No number: scan sees only --catch-up builds, so a count capped at
    catch_up - 1 and said "2 earlier builds" on every tick of a longer run.
    Nor "for 2 builds" — scan examines builds that HAVE failures, which need
    not be consecutive."""
    text = S.render_rollup_only(
        {**STABLE_META, "build_id_b": 222},
        earlier=2, first_build_id=191,
        first_build_name="[master] ci:test:stable - 20002")
    assert "and the same on earlier builds, back to" in text
    assert "2 earlier" not in text
    assert "for 2 builds" not in text
    assert "ci:test:stable - 20002" in text
    assert "No failing test reached Testray for this build" in text
    # A standing condition, not an alarm. Repeating a siren every tick is how
    # a channel learns to skip it.
    assert text.startswith("🔁 ")
    assert "🚨" not in text


def test_the_first_build_says_it_plainly_without_inventing_a_duration():
    text = S.render_rollup_only({**STABLE_META, "build_id_b": 222})
    assert "is the only failure recorded" in text
    assert "has been the only failure for" not in text


def test_one_earlier_build_reads_the_same_as_many():
    text = S.render_rollup_only({**STABLE_META, "build_id_b": 222},
                                earlier=1, first_build_id=191,
                                first_build_name="b")
    assert "and the same on earlier builds, back to" in text
    assert "1 earlier" not in text


def test_a_walk_that_stopped_short_does_not_claim_the_start():
    text = S.render_rollup_only({**STABLE_META, "build_id_b": 222},
                                earlier=20, first_build_id=191,
                                first_build_name="b", at_least=True)
    assert "back to at least" in text


def test_a_fresh_rollup_in_a_bundle_still_gets_the_warning(tmp_path):
    """The bundle path still exists for the narrower case: a pair queued for
    OTHER signatures that all end up auto-classified or excluded, leaving no
    verdicts with the roll-up dropped."""
    text = S.render(bundle(tmp_path, [], meta={"aggregate_dropped": {"new": 1}}))
    assert "⚠️" in text
    assert "never made it into Testray" in text
