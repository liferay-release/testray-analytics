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
    Analysis          one paragraph, the verdict rollup in prose
    Failure --- N     one block per cluster, worst verdict first

Two deliberate departures from that skill:

  * **No "Proposed Fix" paragraph.** That chain attempts a fix, runs
    `pr-check` and can open a PR, so it always has an outcome to report. This
    pipeline classifies and never edits, so the slot would be filled with
    either silence or a fabrication. `*Analysis:*` says what was concluded
    instead.
  * **One message per RUN, not per commit group.** A run's unit is the
    cluster, and a Stable pair regularly carries tens of them; posting one
    message each would bury the channel. Blocks past `MAX_BLOCKS` are dropped
    with the count stated — a silently truncated list reads as complete.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import yaml

from . import verdicts as V
from .config import resolve_path

# Where Jenkins looks for the message. A fixed path, overwritten each run:
# the poster runs immediately after the pipeline, so history lives in the
# report and in Testray, not in a pile of message files.
OUT_REL = "slack/testray_analyzer_slack_message.txt"

# Same circles the failure-cause chain uses, so a reader does not learn a
# second colour scheme. White is theirs too: it means nobody attributed this.
CONFIDENCE_EMOJI = {"high": "🟢", "medium": "🟡", "low": "🔴"}
UNATTRIBUTED_EMOJI = "⚪"

# Verdicts that mean "a person needs to look at this build". Anything else is
# reported but does not raise the siren.
ACTIONABLE = {"BUG", "POSSIBLE_BUG", "TEST_FIX"}

# How many per-cluster blocks to include. Six fills a screen; past that the
# report is the right surface.
MAX_BLOCKS = 6

# Trim lengths. Slack soft-wraps long lines into unreadable walls, and the
# reasoning is already on the report in full.
_REASON_MAX = 700
_WHY_MAX = 200


def _text(value) -> str:
    return "" if value is None else str(value).strip()


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


def _cause_row(meta: dict, result: dict) -> str:
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
        if _text(cand.get("author")):
            bits.append(f"({_text(cand['author'])})")
        label = "Likely cause" if cand.get("explains") else "Closest in range"
        why = _trim(cand.get("why"), _WHY_MAX)
        return f"> *{label}:* {' '.join(bits)}{' — ' + why if why else ''}"

    culprit = _text(result.get("culprit_file"))
    return f"> *Culprit file:* `{culprit}`" if culprit else ""


def _block(meta: dict, n: int, result: dict, cluster: dict,
           caseresult_ids: dict | None = None) -> list[str]:
    """One `Failure --- N` block."""
    verdict = _verdict_of(result)
    confidence = _text(result.get("confidence")).lower()

    # White circle for anything nobody attributed, matching the skill's
    # unattributed vocabulary; otherwise the confidence colour.
    emoji = (UNATTRIBUTED_EMOJI if verdict in ("NEEDS_REVIEW", "NOT_ATTRIBUTABLE")
             else CONFIDENCE_EMOJI.get(confidence, UNATTRIBUTED_EMOJI))

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

    cause = _cause_row(meta, result)
    if cause:
        lines.append(cause)

    reason = _trim(result.get("reason"), _REASON_MAX)
    if reason:
        lines.append(f"> *Reasoning:* {reason}")

    components = [c for c in _text(cluster.get("components")).split("|") if c]
    if components:
        shown = ", ".join(components[:3])
        if len(components) > 3:
            shown += f", +{len(components) - 3} more"
        lines.append(f"> *Components:* {shown}")

    label = verdict.replace("_", " ").title()
    lines.append(f"> *Verdict:* {label}"
                 + (f" · {emoji} confidence {confidence}" if confidence
                    else f" · {emoji} unattributed"))
    return lines


def render(run_dir: Path, *, report_url: str = "",
           link_testray: bool = False) -> str:
    """The message body. Plain text — the poster adds nothing."""
    run_dir = Path(run_dir)
    meta, results, clusters, caseresult_ids = _load(run_dir)

    build_b = _text(meta.get("build_b_name")) or _text(meta.get("build_id_b"))
    build_a = _text(meta.get("build_a_name")) or _text(meta.get("build_id_a"))
    # Full names stay on the bullet row where there is space for them; the
    # headline gets the short form.
    head_b = _trim(build_b, 58)
    routine = _text(meta.get("routine_id"))

    # Worst first, then biggest: the same order the report leads with, so the
    # two artifacts cannot disagree about what the headline failure is.
    ordered = sorted(
        results,
        key=lambda r: (V.rank(_verdict_of(r)),
                       -int(_text(clusters.get(_text(r.get("group_id")), {})
                                  .get("case_count")) or 0)))

    counts: dict[str, int] = {}
    for r in results:
        counts[_verdict_of(r)] = counts.get(_verdict_of(r), 0) + 1

    rollup = V.rollup([_verdict_of(r) for r in results]) if results else "PENDING"
    siren = "🚨" if rollup in ACTIONABLE else "🔍"

    headline = (f"Testray triage — routine {routine} build {head_b}"
                if routine else f"Testray triage — build {head_b}")
    compare = _compare_url(meta)
    head = f"{siren} *{headline}*"
    if compare:
        a, b = _text(meta.get("git_hash_a")), _text(meta.get("git_hash_b"))
        head += f" ({_link(compare, f'{a[:9]}..{b[:9]}')})"

    lines = [head]

    lines.append(f"• *Baseline → Target:* "
                 f"{_link(_build_url(meta, meta.get('build_id_a')), build_a)}"
                 f" → {_link(_build_url(meta, meta.get('build_id_b')), build_b)}")

    # The Jenkins job that produced the build. Named "Top Level Build" to
    # match Testray's own label for it, and worth a row of its own on Stable:
    # our error text stops at "a Gradle task failed", and the console under
    # this link is where the failing task and its cause are actually written.
    jenkins = _text(meta.get("jenkins_url_b"))
    if jenkins:
        lines.append(f"• *Top Level Build:* "
                     f"{_link(jenkins, jenkins.rstrip('/').rpartition('/job/')[2] or 'open in Jenkins')}")

    total = _text(meta.get("total_failures"))
    lines.append(f"• *Clusters:* {len(results)} classified"
                 + (f" over {total} failure(s)" if total else ""))

    # Two report links, and they are not interchangeable. The Testray one is
    # where the team already works and where the verdicts now live; the
    # published one is the standalone HTML, which is what exists on an
    # instance whose Objects are not deployed.
    if link_testray:
        tr = triage_url(meta)
        if tr:
            lines.append(f"• *Triage Report:* {_link(tr, 'open in Testray')}")

    url = _text(report_url) or _text(meta.get("report_url"))
    if url:
        lines.append(f"• *Full Report:* {_link(url, 'report')}")

    # The verdict census. Stated even when it is all NEEDS_REVIEW — "the run
    # explained nothing" is the single most useful thing a reader can learn
    # from a glance, and it is what a missing line would hide.
    if counts:
        census = " · ".join(f"{v.replace('_', ' ').title()} {counts[v]}"
                            for v in V.VERDICT_ORDER if counts.get(v))
        lines += ["", f"*Analysis:* {census}."]
    else:
        lines += ["", "*Analysis:* no verdicts were produced for this pair — "
                      "every failure was auto-classified or excluded upstream. "
                      "Needs a human."]

    for i, result in enumerate(ordered[:MAX_BLOCKS], start=1):
        cluster = clusters.get(_text(result.get("group_id")), {})
        lines += [""] + _block(meta, i, result, cluster, caseresult_ids)

    if len(ordered) > MAX_BLOCKS:
        lines += ["", f"_{len(ordered) - MAX_BLOCKS} further cluster(s) not "
                      f"shown — see the full report._"]

    return "\n".join(lines) + "\n"


def write(run_dir: Path, *, out: Path | None = None, report_url: str = "",
          link_testray: bool = False) -> Path:
    """Render and write the message. Returns the path written."""
    target = Path(out) if out else resolve_path(None, OUT_REL)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(run_dir, report_url=report_url,
                             link_testray=link_testray), encoding="utf-8")
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
