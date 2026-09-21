"""
slack_message.py — the triage run as one Slack post, for Jenkins to send.

Deliberately NOT a Slack client. The pipeline writes a plain text file and a
Jenkins post-build step posts it, the same way `slack-post` does in
jenkins-tools-private: rolling our own webhook call here would duplicate a
script that already mirrors `NotificationUtil.sendSlackNotification` byte for
byte, and would put a credential in this repo that it does not otherwise need.

The layout follows the `failure-cause-fix` skill's message format, because the
team already reads that shape in `#portal-failures` and a second vocabulary for
the same job would cost every reader a translation:

    header line       what broke, with the commit range
    bullet list       compact metadata — builds, coverage, report
    Failure --- N     one block per cluster, worst verdict first

Three deliberate departures from that skill:

  * **No "Proposed Fix" paragraph.** That chain attempts a fix, runs
    `pr-check` and can open a PR, so it always has an outcome to report. This
    pipeline classifies and never edits, so the slot would be filled with
    either silence or a fabrication.
  * **No verdict, anywhere.** No census line, no per-block label. "False
    Positive 1" was being read as permission to stop reading, so a label
    people glossed over decided whether the report got opened at all. The
    verdict still orders the blocks and still picks the siren; it just never
    appears as text. It belongs where it can be weighed against the evidence
    that produced it — the report, and Testray.
  * **One message per RUN, not per commit group.** A run's unit is the
    cluster, and a Stable pair regularly carries tens of them; posting one
    message each would bury the channel. Blocks past `MAX_BLOCKS` are dropped
    with the count stated — a silently truncated list reads as complete.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import verdicts as V
from .config import resolve_path

# Where Jenkins looks for the message. A fixed path, overwritten each run:
# the poster runs immediately after the pipeline, so history lives in the
# report and in Testray, not in a pile of message files.
OUT_REL = "slack/testray_analyzer_slack_message.txt"

# Verdicts that mean "a person needs to look at this build". Anything else is
# reported but does not raise the siren.
ACTIONABLE = {"BUG", "POSSIBLE_BUG", "TEST_FIX"}

# How many per-cluster blocks to include. Six fills a screen; past that the
# report is the right surface.
MAX_BLOCKS = 6

# Backstop for the reasoning row. The row is normally one sentence plus a
# `Read more…` link, so this only bites on reasoning with no sentence boundary
# in it at all — a pasted stack trace, which is exactly the case that used to
# fill the channel with a wall.
#
# There is deliberately no cap on a candidate's `why`. It used to be cut at 200
# characters, which reliably severed the sentence naming the mechanism — the
# one thing a reader needs to judge whether the attribution is credible — and a
# half-stated cause reads as a confident one. Length is the report's problem;
# being wrong in public is this file's.
_REASON_MAX = 700


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _flat(text: str) -> str:
    """Collapse whitespace without shortening. Slack renders a newline inside a
    `>` quote as an unquoted line, so the text still has to be one line."""
    return " ".join(_text(text).split())


# A sentence boundary: `.`, `?` or `!`, then whitespace, then something that
# starts a new sentence. Requiring the whitespace AND the capital is what keeps
# `BlogsEntryServiceHttp.java diff` and `com.liferay.portal.kernel` in one
# piece — a dotted identifier has no space after the dot, and a file extension
# is lowercase. Splitting naively on "." cut the reasoning at the first class
# name it mentioned, which is nearly always inside sentence one.
_SENTENCE_END_RE = re.compile(r"""(?<=[.!?])\s+(?=["'(\[]?[A-Z0-9])""")

# These end in a period without ending a sentence. Without the guard, a
# reasoning that opens "The check fails, e.g. When the flag is off…" renders a
# four-word first sentence and hides the rest behind the link.
_ABBREVIATIONS = frozenset(("e.g.", "i.e.", "cf.", "vs.", "etc.", "resp.",
                            "approx.", "no.", "fig.", "ver.", "al."))


def _first_sentence(text: str) -> tuple[str, bool]:
    """The first sentence, and whether anything followed it.

    The caller needs the second half of that pair: a `Read more…` link on a
    reasoning that was already complete sends a reader to the report to find
    nothing new, which teaches them the link is not worth following.
    """
    s = _flat(text)
    for m in _SENTENCE_END_RE.finditer(s):
        head = s[:m.start()]
        last = head.rsplit(" ", 1)[-1].casefold()
        # An abbreviation, or an initial like `J.` — neither ends a sentence.
        if last in _ABBREVIATIONS or len(last.rstrip(".")) <= 1:
            continue
        return head, True
    return s, False


def _trim(text: str, cap: int) -> str:
    """Cut on a word boundary, and say that it was cut."""
    s = " ".join(_text(text).split())
    if len(s) <= cap:
        return s
    return s[:cap].rsplit(" ", 1)[0] + "…"


def _link(url: str, label: str) -> str:
    """Slack mrkdwn link, or the bare label when there is no URL.

    Parens are URL-encoded because Slack ends a link at the first `)` — Jenkins
    job names are full of them (`test-portal-testsuite-upstream(master)`), and
    an un-encoded one truncates the URL silently.
    """
    u = _text(url)
    if not u:
        return _text(label)
    return f"<{u.replace('(', '%28').replace(')', '%29')}|{_text(label)}>"


def _jira_base(meta: dict) -> str:
    """Jira origin for ticket links.

    Deliberately NOT `resolve_jira_settings`, which report.py calls: that one
    fetches the reporter account id over the network when it is not configured,
    and a Slack message must never be able to hang on Jira being slow. Only the
    base URL is needed here, and run.yml already carries it whenever submit
    resolved it.
    """
    jira = meta.get("jira")
    base = _text(jira.get("base_url")) if isinstance(jira, dict) else ""
    return (base or "https://liferay.atlassian.net").rstrip("/")


def _short_build_name(name: str) -> str:
    """`[master] ci:test:stable - 19505 - …` -> `ci:test:stable - 19505 - …`.

    Only a LEADING bracket is stripped — the build name ends in a timestamp
    like `[05:05:51]`, and a greedy strip would eat the part that distinguishes
    two builds of the same routine on the same day.
    """
    s = _text(name)
    if s.startswith("[") and "]" in s:
        s = s.split("]", 1)[1].lstrip()
    return s


def _axis_label(url: str) -> str:
    """`…/1493/modules-integration-postgresql163_stable/0/0/jenkins-console.txt.gz`
    -> `modules-integration-postgresql163_stable/0/0`.

    The axis is what a reader recognises — it is how the same failure is named
    in Jenkins and in the batch that ran it. Falls back to a plain label rather
    than guessing when the path is not this shape.
    """
    path = _text(url).split("?", 1)[0].rstrip("/")
    parts = [seg for seg in path.split("/") if seg][:-1]  # drop the filename
    return "/".join(parts[-3:]) if len(parts) >= 3 else "console"


def _build_url(meta: dict, build_id) -> str:
    """Testray deep-link for a build. Same shape report.py uses."""
    base, project = _text(meta.get("testray_url")), _text(meta.get("project_id"))
    routine, bid = _text(meta.get("routine_id")), _text(build_id)
    if not (base and project and routine and bid):
        return ""
    return (f"{base.rstrip('/')}#/project/{project}/routines/{routine}"
            f"/build/{bid}")


def _case_result_url(meta: dict, caseresult_id) -> str:
    """Testray deep-link for one case result inside the target build.

    Testray's own `attachments` field uses exactly this shape to point back at
    a build's top-level result, so it is the instance's own spelling rather
    than one invented here.
    """
    build = _build_url(meta, meta.get("build_id_b"))
    crid = _text(caseresult_id)
    if not (build and crid):
        return ""
    return f"{build}/case-result/{crid}"


def triage_url(meta: dict) -> str:
    """The Testray triage view for the target build, or "".

    `/web/<site>/triage?buildId=<id>` — the site page the analytics custom
    element renders on, matching `TRIAGE_PATH` + `triageURL()` in
    liferay-testray-custom-element. `testray_url` already carries the site
    path, which differs per instance (`/web/testray` on prod,
    `/web/liferay-testray` locally), so it cannot be derived any other way.

    Building the URL is not the same as it being worth showing: on an instance
    without the analytics CX the page does not exist. The caller decides, and
    only passes it once the verdicts are in Testray.
    """
    base, build = _text(meta.get("testray_url")), _text(meta.get("build_id_b"))
    if not (base and build):
        return ""
    return f"{base.rstrip('/')}/triage?buildId={build}"


def _compare_url(meta: dict) -> str:
    """GitHub compare link for the pair's range.

    Built from `repo_slug`, which prepare resolves from the ROUTINE's own
    remote — Stable's commits live in the control repo and a link to
    liferay/liferay-portal would 404 for exactly the failures that matter most.
    """
    slug = _text(meta.get("repo_slug")) or "liferay/liferay-portal"
    a, b = _text(meta.get("git_hash_a")), _text(meta.get("git_hash_b"))
    if not (a and b):
        return ""
    return f"https://github.com/{slug}/compare/{a}...{b}"


def _load(run_dir: Path) -> tuple[dict, list[dict], dict, dict]:
    """run.yml, the verdicts, the cluster rows by group id, and a
    case_id -> caseresult_id map.

    All four come from bundle files rather than submit's in-memory frame, so
    this can be re-rendered for a finished run without re-running anything.

    The last one exists because the cluster rows carry Testray CASE ids while
    a deep-link needs the CASE RESULT id — different numbers, and only
    `diff_list.csv` holds both.
    """
    meta = yaml.safe_load((run_dir / "run.yml").read_text(encoding="utf-8")) or {}

    results_path = run_dir / "results.json"
    results = []
    if results_path.exists():
        payload = json.loads(results_path.read_text(encoding="utf-8")) or {}
        results = payload.get("results") or []

    clusters: dict[str, dict] = {}
    subtasks = run_dir / "diff_list_subtasks.csv"
    if subtasks.exists():
        with subtasks.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                clusters[_text(row.get("group_id"))] = row

    caseresult_ids: dict[str, str] = {}
    diff_list = run_dir / "diff_list.csv"
    if diff_list.exists():
        with diff_list.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                case_id = _text(row.get("testray_case_id"))
                crid = _text(row.get("caseresult_id"))
                if case_id and crid:
                    caseresult_ids.setdefault(case_id, crid)

    return meta, results, clusters, caseresult_ids


def _verdict_of(result: dict) -> str:
    """Display verdict, via the one module that owns the relabel rule.

    Not the raw `classification`: a low-confidence NEEDS_REVIEW displays as
    NOT_ATTRIBUTABLE, and the report and the Testray index both already say so.
    A third spelling here is how the counts drift apart.
    """
    return V.display_verdict(result.get("classification"),
                             result.get("confidence"),
                             _text(result.get("specific_change")))


# Slack resolves `<@handle>` against the workspace. Liferay's handles are the
# local part of the corporate address, so no API call, no token and no user
# cache is needed — the same transform the jenkins-results-parser
# failure-cause-fix skill uses. An address outside the domain has no handle to
# guess, so it renders raw: a visible email still carries the attribution,
# where a fabricated `<@...>` would either silently fail to link or, worse,
# ping whoever does own that handle.
_LIFERAY_EMAIL = "@liferay.com"


def _slack_mention(email: str) -> str:
    e = _text(email)
    if not e:
        return ""
    return f"<@{e[:-len(_LIFERAY_EMAIL)]}>" if e.lower().endswith(_LIFERAY_EMAIL) else e


def _commit_authors(run_dir: Path, meta: dict, resolve: bool = False) -> dict:
    """`{sha: (name, email)}` for the commits the verdicts named.

    `submit` resolves this once and passes it on `meta`. `resolve` asks for the
    lookup instead, for a standalone re-render (`testray-analysis slack
    <bundle>`) where nobody has done it.

    It is opt-in rather than automatic because the lookup shells into the
    configured checkout, and hidden filesystem access inside a pure render is
    the wrong shape: it made a unit test's output depend on whether the
    developer happened to have liferay-portal cloned, passing here and failing
    on a clean machine.

    Never fatal — no checkout means no mention, and the row falls back to the
    classifier's own author text.
    """
    cached = meta.get("_commit_authors")
    if cached is not None:
        return cached
    if not resolve:
        return {}
    try:
        from .prepare import fetch_commit_authors, load_config
        shas = set()
        payload = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
        for r in payload.get("results") or []:
            for c in (r.get("candidates") or []):
                if isinstance(c, dict) and c.get("commit"):
                    shas.add(str(c["commit"]).strip())
        repo = (load_config().get("git") or {}).get("repo_path")
        return fetch_commit_authors(Path(str(repo)).expanduser(), shas) if repo else {}
    except Exception:                                            # noqa: BLE001
        return {}


def _cause_row(meta: dict, result: dict, authors: dict | None = None) -> str:
    """`*Likely cause:*` — only when the classifier actually named one.

    A candidate flagged `explains: false` is the closest change in range, not
    the cause, so it is rendered as a lead ("closest in range") rather than an
    accusation. Naming a person's commit as the cause of a build everyone is
    watching is not a rendering detail.
    """
    slug = _text(meta.get("repo_slug")) or "liferay/liferay-portal"
    for cand in (result.get("candidates") or []):
        if not isinstance(cand, dict) or not _text(cand.get("commit")):
            continue
        sha = _text(cand["commit"])
        bits = [_link(f"https://github.com/{slug}/commit/{sha}", sha[:9])]
        if _text(cand.get("ticket")):
            bits.append(_text(cand["ticket"]))

        # Ping only when the classifier says this commit EXPLAINS the failure.
        # A candidate flagged `explains: false` is the closest change in range,
        # and notifying someone that their commit is the nearest thing to a
        # build they did not break is how a useful alert becomes one people
        # mute. Same reason the skill drops its Authors row on unattributed
        # clusters.
        name, email = (authors or {}).get(_text(cand.get("commit")), ("", ""))
        who = _slack_mention(email) if cand.get("explains") else ""
        if not who:
            # git's name beats the classifier's: it named the commit, not the
            # person, and on a multi-author ticket it picks the wrong one.
            who = name or _text(cand.get("author"))
        if who:
            bits.append(f"({who})")

        label = "Likely cause" if cand.get("explains") else "Closest in range"
        why = _flat(cand.get("why"))
        return f"> *{label}:* {' '.join(bits)}{' — ' + why if why else ''}"

    culprit = _text(result.get("culprit_file"))
    return f"> *Culprit file:* `{culprit}`" if culprit else ""


def _block(meta: dict, n: int, result: dict, cluster: dict,
           caseresult_ids: dict | None = None,
           authors: dict | None = None, read_more_url: str = "") -> list[str]:
    """One `Failure --- N` block.

    Carries no verdict label. See `render` for why.
    """
    tests = [t for t in _text(cluster.get("member_test_cases")).split("|") if t]
    count = int(_text(cluster.get("case_count")) or len(tests) or 1)
    title = _trim(tests[0], 90) if tests else _trim(cluster.get("signature"), 90)
    if not title:
        title = f"cluster {_text(result.get('group_id'))}"
    if count > 1:
        title += f"  (+{count - 1} more)"

    # Link the title at the failing case result in Testray. The first member,
    # not the cluster: a cluster has no page of its own until the analytics
    # client extension is deployed, and a reader following this link wants the
    # error text and the attachments, both of which live on the result.
    members = [c for c in _text(cluster.get("member_case_ids")).split("|") if c]
    if not members:
        members = [_text(c) for c in (result.get("case_ids") or [])]
    crid = (caseresult_ids or {}).get(members[0]) if members else ""
    href = _case_result_url(meta, crid) if crid else ""

    lines = [f"*Failure --- {n}:* *{_link(href, title) if href else title}*"]

    cause = _cause_row(meta, result, authors)
    if cause:
        lines.append(cause)

    # One sentence, then the link. The reasoning is a paragraph written for
    # the report, where it sits beside the diff and the error text that make it
    # checkable; in the channel it arrived as a wall that stated a conclusion
    # with none of that beside it, so it was read as the answer instead of as
    # the claim it is. The opening sentence says WHAT the model concluded,
    # which is the part a reader can act on, and the link is where the rest of
    # it can be weighed.
    first, more = _first_sentence(result.get("reason"))
    if more and read_more_url:
        reason = (f"{_trim(first, _REASON_MAX)} "
                  f"{_link(read_more_url, 'Read more…')}")
    else:
        # Nowhere to send them, so the text has to carry itself — hiding the
        # rest behind a `Read more…` that goes nowhere loses it outright. This
        # is the instance with no analytics CX and no published report.
        reason = _trim(result.get("reason"), _REASON_MAX)
    if reason:
        lines.append(f"> *Reasoning:* {reason}")

    # The axis console. Our error text stops at the assertion or at "a Gradle
    # task failed"; this log is where the failing task and its cause are
    # actually written, and it is the first thing a person opens when the
    # verdict is NEEDS_REVIEW. Reading it still needs a GCS grant — the link is
    # useful anyway, because a human following it has one.
    console = _text(cluster.get("console_url"))
    if console:
        lines.append(f"> *Console:* {_link(console, _axis_label(console))}")

    components = [c for c in _text(cluster.get("components")).split("|") if c]
    if components:
        shown = ", ".join(components[:3])
        if len(components) > 3:
            shown += f", +{len(components) - 3} more"
        lines.append(f"> *Components:* {shown}")

    return lines


def _repeats_for(run_dir: Path, meta: dict) -> dict:
    """Repeats for this run. `submit` does the lookup once and passes it in on
    `meta`; this only falls back to its own call when the message is
    re-rendered standalone (`testray-analysis slack <bundle>`), where there is
    nobody to have done it."""
    cached = meta.get("_repeats")
    if cached is not None:
        return cached
    from . import recurrence
    return recurrence.repeats_for_run(run_dir, meta)


def _is_aggregate_row(test_name: str) -> bool:
    """Testray's whole-build row. `prepare.drop_aggregate_rows` removes it from
    new bundles; this stays because a bundle prepared before that change still
    carries the row, and re-rendering an old run must not resurrect it."""
    from .prepare import is_aggregate_row
    return is_aggregate_row(test_name)


def _ago(iso: str) -> str:
    """"1h", "3d" — how long before now. Empty when the time is unusable, and
    the caller drops the clause rather than printing a wrong age."""
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = (datetime.now(timezone.utc) - when).total_seconds()
    if secs < 0:
        return ""
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def _still_failing(meta: dict, repeats: dict) -> list[str]:
    """The ":repeat: Still failing" section.

    It replaces "no verdicts were produced — needs a human" on a build that
    inherited every one of its failures. That line sent people to
    re-investigate a failure explained days earlier, which is worse than
    silence: it spends someone's morning to reach a conclusion already on file.

    An INDEX, not an explanation. An earlier cut quoted the stored `reason`
    under each bullet, and on 19793 that was two 300-character paragraphs
    saying the same thing about the same compile break — pushing what a reader
    acts on (which build, which file, which ticket) off the first screen of a
    message whose entire point is "you already know this". The reasoning still
    exists, in the report of the run that produced it, so this links that run
    instead of re-telling it.
    """
    if not repeats:
        return []

    from .recurrence import triage_url_for

    shown = sorted(repeats.values(),
                   key=lambda r: (-r.occurrences, r.test_name))[:MAX_BLOCKS]

    lines = ["", "🔁 *Still failing* — every failure here was inherited, "
                 "nothing new in this build."]

    # One link at the top when every repeat started in the same build — the
    # usual Stable shape, where one break makes every cluster in the build
    # recur together. When they started in DIFFERENT builds there is no single
    # "the original report", and letting the first one stand for all of them
    # would send a reader to a run that never saw the failure they clicked
    # from. Those carry their own link per bullet instead.
    shared = (triage_url_for(meta, shown[0].first_build_id)
              if len({r.first_build_id for r in shown}) == 1 else "")
    if shared:
        lines.append(f"*Original report:* {_link(shared, 'open in Testray')}")

    base = _jira_base(meta)
    for rep in shown:
        where = _link(_build_url(meta, rep.first_build_id),
                      _short_build_name(rep.first_build_name)
                      or str(rep.first_build_id))
        ago = _ago(rep.first_build_time)
        bullet = (f"  • *{_trim(rep.test_name, 90)}* — occurrence "
                  f"#{rep.occurrences}, first seen in {where}"
                  + (f" ({ago} ago)" if ago else ""))
        own = "" if shared else triage_url_for(meta, rep.first_build_id)
        if own:
            bullet += f" · {_link(own, 'report')}"
        # A blank line before each entry. Slack renders these two-line entries
        # as one grey wall without it, and the eye cannot find where one repeat
        # ends and the next begins — which on a six-repeat build is the whole
        # difference between a list and a paragraph. It also separates the
        # first entry from the header block above.
        lines += ["", bullet]

        # The file and the ticket from when this episode WAS analysed: enough
        # to recognise the failure and to reach the change that caused it,
        # which is the difference between "this is old" and "this is old AND
        # here is what we already decided about it". The ticket is linked
        # because a bare key in Slack is a copy-paste job.
        if rep.prior_culprit or rep.prior_tickets:
            bits = []
            if rep.prior_culprit:
                bits.append(f"`{_trim(rep.prior_culprit, 80)}`")
            bits += [_link(f"{base}/browse/{t}", t)
                     for t in (rep.prior_tickets or ())]
            lines.append(f"    *Likely cause:* {' '.join(bits)}")
        else:
            # Said out loud rather than left blank. A repeat with no verdict on
            # file is the write policy working as intended — the cluster was
            # flaky, auto-classified or never ran — and a reader who is not
            # told that reads the empty space as a missing answer.
            lines.append("    _No verdict on file — the original run excluded "
                         "this cluster from triage._")

    return lines


def _inherited_note(meta: dict, results: list, report_url: str) -> list[str]:
    """One line for the failures this build carried in and nobody re-analysed.

    `_still_failing` above only renders when a build inherited EVERY one of its
    failures. On a MIXED build the new failure is the news and has to stay the
    headline — but saying nothing at all is what a real Stable message did with
    63 pre-existing failures beside 4 verdicts: the only trace was that
    "4 classified over 69 failure(s)" had two different numbers in it, which no
    reader decodes. That is the difference between "one thing broke" and "one
    thing broke on top of a sync that has been blocked since Tuesday".

    One line, and never a bullet list: the whole point of the section above is
    that inherited failures must not compete with the build that introduced
    one.

    The count comes from `transition_counts`, which `prepare` already wrote
    into run.yml. Deliberately NOT a recurrence walk — this needs a number, not
    a first-seen build, and a walk per classified run would be a network call
    per run for something the bundle is already carrying.
    """
    if not results:
        # No verdicts means `_still_failing` rendered, and it speaks for these
        # in full. Two sections about the same failures is worse than one.
        return []
    try:
        n = int((meta.get("transition_counts") or {}).get("same_failure") or 0)
    except (TypeError, ValueError):
        return []
    if n < 1:
        return []

    label = f"Pre-existing ({n})"
    where = _link(f"{report_url}#pre-existing", label) if report_url else label
    return ["", f"🔁 {n} failure{'s' if n != 1 else ''} inherited from earlier "
                f"builds, not re-analyzed — see {where} in the report."]


def render_recurrence(meta: dict, repeats: dict) -> str:
    """The message for a tick that analysed nothing because nothing was new.

    Stable posts to the channel on every failure, so a red build that produced
    no run must still be answered. Without this the tick falls through to the
    Jenkins job's own "nothing was submitted this run" notice, which tells a
    reader the analyser did nothing — when in fact it recognised every failure
    and can name the run that explained each one.

    Deliberately never a siren: nothing here is new, so it must not compete
    with the build that introduced the failure.
    """
    build_b = _text(meta.get("build_b_name")) or _text(meta.get("build_id_b"))
    head = _trim(_short_build_name(build_b), 58)
    where = _link(_build_url(meta, meta.get("build_id_b")), head) if head else ""

    lines = [f"🔁 *{where or 'This build'}* — no new failures; "
             f"each one was analyzed already."]

    body = _still_failing(meta, repeats)
    # The section keeps a leading blank for `render`, where it has to separate
    # itself from the *Analysis:* paragraph above. Here the two 🔁 lines are one
    # statement — "nothing new, and here is what is still red" — so the gap
    # only makes the reader look for something that is not there.
    if body and body[0] == "":
        body = body[1:]
    if not body:
        # Reached when the walk found no earlier appearance for anything —
        # rare, and saying so is better than posting a bare header.
        lines.append("")
        lines.append("_No recurring signature could be traced to an earlier "
                     "build._")
        return "\n".join(lines)

    return "\n".join(lines + body)


def render(run_dir: Path, *, report_url: str = "",
           link_testray: bool = False, resolve_authors: bool = False) -> str:
    """The message body. Plain text — the poster adds nothing."""
    run_dir = Path(run_dir)
    meta, results, clusters, caseresult_ids = _load(run_dir)
    repeats = _repeats_for(run_dir, meta) if not results else {}
    authors = (_commit_authors(run_dir, meta, resolve_authors)
               if results else {})

    build_b = _text(meta.get("build_b_name")) or _text(meta.get("build_id_b"))
    head_b = _trim(_short_build_name(build_b), 58)

    # Worst first, then biggest: the same order the report leads with, so the
    # two artifacts cannot disagree about what the headline failure is.
    ordered = sorted(
        results,
        key=lambda r: (V.rank(_verdict_of(r)),
                       -int(_text(clusters.get(_text(r.get("group_id")), {})
                                  .get("case_count")) or 0)))

    rollup = V.rollup([_verdict_of(r) for r in results]) if results else "PENDING"
    siren = "🚨" if rollup in ACTIONABLE else "🔍"

    # The build name IS the link, and carries no `routine N build` preamble:
    # the routine is implied by the channel the message lands in, and the two
    # ids in front of the name pushed the build number — the only part a reader
    # scans for — past where Slack truncates a notification preview.
    headline = _link(_build_url(meta, meta.get("build_id_b")), head_b)
    compare = _compare_url(meta)
    head = f"{siren} *{headline}*"
    if compare:
        a, b = _text(meta.get("git_hash_a")), _text(meta.get("git_hash_b"))
        head += f" ({_link(compare, f'{a[:9]}..{b[:9]}')})"

    lines = [head]

    # No `Baseline → Target` row: the target is the headline link now, and the
    # baseline's only real use is the commit range, which the compare link on
    # the headline already spells out. Two build links that differ by one digit
    # cost a reader more than they tell them.

    # The Jenkins job that produced the build. Named "Top Level Build" to
    # match Testray's own label for it, and worth a row of its own on Stable:
    # our error text stops at "a Gradle task failed", and the console under
    # this link is where the failing task and its cause are actually written.
    jenkins = _text(meta.get("jenkins_url_b"))
    if jenkins:
        lines.append(f"• *Top Level Build:* "
                     f"{_link(jenkins, jenkins.rstrip('/').rpartition('/job/')[2] or 'open in Jenkins')}")

    # Two report links, and they are not interchangeable. The Testray one is
    # where the team already works and where the verdicts now live; the
    # published one is the standalone HTML, which is what exists on an
    # instance whose Objects are not deployed. Both sit above the cluster count
    # so the two "where do I read this" rows stay together under the build.
    tr = triage_url(meta) if link_testray else ""
    if tr:
        lines.append(f"• *Triage Report:* {_link(tr, 'open in Testray')}")

    url = _text(report_url) or _text(meta.get("report_url"))
    if url:
        lines.append(f"• *Full Report:* {_link(url, 'report')}")

    # Where `Read more…` on a block's reasoning points. Testray first, for the
    # same reason the two rows above are in this order: it is where the team
    # already works and where the verdicts now live. Empty when neither surface
    # exists, and the blocks then print the reasoning in full.
    read_more = tr or url

    total = _text(meta.get("total_failures"))
    lines.append(f"• *Clusters:* {len(results)} classified"
                 + (f" over {total} failure(s)" if total else ""))

    # NO verdict census, and no verdict label on the blocks below.
    #
    # "Test Fix 2" or "False Positive 1" is a conclusion about the RUN, and in
    # the channel it was read as permission to stop reading: a label people
    # glossed over decided whether they opened the report at all, which is
    # backwards — the label is the least reliable thing in the message and the
    # failing test and the named commit are the most. So the message carries
    # what a reader acts on, and the verdict stays where it can be weighed
    # against the evidence: the report, and Testray.
    #
    # The siren on the headline still comes from the rollup, and the blocks are
    # still ordered worst-verdict-first. The verdict decides what a reader sees
    # FIRST; it just does not get to be what they see INSTEAD.
    if not results and not repeats:
        # A pair with no verdicts AND no history is genuinely unexplained, and
        # that is not a verdict label — it is the absence of one, which nobody
        # can gloss over into "handled".
        lines += ["", "*Analysis:* no verdicts were produced for this pair "
                      "— every failure was auto-classified or excluded "
                      "upstream. Needs a human."]

    lines += _still_failing(meta, repeats)
    lines += _inherited_note(meta, results, url)

    for i, result in enumerate(ordered[:MAX_BLOCKS], start=1):
        cluster = clusters.get(_text(result.get("group_id")), {})
        lines += [""] + _block(meta, i, result, cluster, caseresult_ids,
                               authors, read_more)

    if len(ordered) > MAX_BLOCKS:
        lines += ["", f"_{len(ordered) - MAX_BLOCKS} further cluster(s) not "
                      f"shown — see the full report._"]

    return "\n".join(lines) + "\n"


# Separates one run's message from the next when a queue drain appends several
# into the single file Jenkins posts. A tick that drained two pairs used to
# post only the last: both submits wrote OUT_REL, so on 2026-09-16 the 19757
# infra false positive overwrote the 19764 compile break and nobody heard
# about the only actionable finding of the tick.
RUN_SEPARATOR = "━" * 24


# Slack refuses a message over 40,000 characters outright, and a tick draining
# a backlog appends one full run message per pair — measured at ~5,600
# characters on a real Stable bundle, so seven pairs would silence the post
# entirely. That is a worse failure than a long post: nothing gets said at all.
# Stop well short and name what was dropped, the way MAX_BLOCKS already does
# inside a single message.
MAX_POST_CHARS = 30_000

# Room for the overflow line itself, so adding it can never be what pushes the
# post over the budget it exists to enforce.
_OVERFLOW_RESERVE = 160

_OVERFLOW_RE = re.compile(r"^_… and \d+ more pair\(s\) [^\n]*_$", re.MULTILINE)
_OVERFLOW_COUNT_RE = re.compile(r"^_… and (\d+) more pair\(s\)", re.MULTILINE)


def _overflow_line(n: int) -> str:
    return (f"_… and {n} more pair(s) analyzed this tick — not shown here; "
            f"their verdicts are in Testray and on the report._")


def _without_overflow(text: str) -> tuple[str, int]:
    """The post with its overflow line removed, and the count it carried."""
    m = _OVERFLOW_COUNT_RE.search(text)
    if not m:
        return text.rstrip(), 0
    return _OVERFLOW_RE.sub("", text).rstrip(), int(m.group(1))


def append_to_post(target: Path, text: str) -> None:
    """Add one run's message to the tick's post, or record it as dropped.

    Shared by `write(append=True)` and `scan`, so the two cannot disagree about
    when the post is full.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if target.exists():
        existing = target.read_text(encoding="utf-8").rstrip()

    if not existing:
        target.write_text(text, encoding="utf-8")
        return

    budget = MAX_POST_CHARS - _OVERFLOW_RESERVE
    if len(existing) + len(text) + len(RUN_SEPARATOR) + 4 > budget:
        kept, dropped = _without_overflow(existing)
        target.write_text(f"{kept}\n\n{_overflow_line(dropped + 1)}",
                          encoding="utf-8")
        return

    target.write_text(f"{existing}\n\n{RUN_SEPARATOR}\n\n{text}",
                      encoding="utf-8")


def write(run_dir: Path, *, out: Path | None = None, report_url: str = "",
          link_testray: bool = False, append: bool = False) -> Path:
    """Render and write the message. Returns the path written.

    `append` belongs to the queue runner: it drains N pairs per tick through N
    separate submits while Jenkins posts the file once, at the end. Each
    submit therefore has to add to the post rather than replace it.
    """
    target = Path(out) if out else resolve_path(None, OUT_REL)
    target.parent.mkdir(parents=True, exist_ok=True)
    # A standalone re-render has no submit to have resolved the authors, so
    # ask for the lookup here rather than inside render().
    text = render(run_dir, report_url=report_url,
                  link_testray=link_testray, resolve_authors=True)

    if append:
        append_to_post(target, text)
    else:
        target.write_text(text, encoding="utf-8")

    return target


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Render a run bundle as the Slack message Jenkins posts.")
    ap.add_argument("run_dir", help="the run bundle (runs/r_…)")
    ap.add_argument("--out", default=None, metavar="PATH",
                    help=f"where to write it (default: {OUT_REL} under the "
                         f"project root)")
    ap.add_argument("--report-url", default=None,
                    help="link for the *Full Report:* row; falls back to "
                         "run.yml `report_url`.")
    ap.add_argument("--link-testray", action="store_true",
                    help="include the Testray triage-view link. Only pass this "
                         "when the verdicts are actually in that instance — "
                         "the page does not exist without the analytics CX.")
    ap.add_argument("--print", action="store_true", dest="to_stdout",
                    help="also print the message")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    if not (run_dir / "run.yml").exists():
        print(f"Not a run bundle (no run.yml): {run_dir}", file=sys.stderr)
        raise SystemExit(2)

    path = write(run_dir, out=Path(args.out) if args.out else None,
                 report_url=args.report_url or "",
                 link_testray=args.link_testray)
    print(f"Slack message: {path}")
    if args.to_stdout:
        print()
        print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
