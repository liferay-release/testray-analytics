"""
report.py — HTML report for a triage run.

Layout is ported from the Release Analytics Platform per-test report
(`apps/triage/.../report.html`), which is the format Nikki settled on: one
table on a fixed column grid, cluster headers sitting on that same grid rather
than in colspan banners, member rows folding underneath, and every row
expanding in place to its own detail panel. Filters, search, group-by and
sort-within-cluster are all client-side so the file stays a single artifact you
can open from disk or hand to someone.

Two deliberate inheritances from that report:

* **A cluster header is a row, not a banner.** The count sits under *Test*, the
  component/team rollups under their own columns, the shared culprit and reason
  under theirs. It reads as a header through weight and tint, so the eye can
  still track a column down the page.
* **Member rows do not repeat the cluster's shared cells.** They collapse to an
  "↑ cluster N" pointer, because repeating an identical paragraph 30 times is
  what made the earlier per-test table unreadable.

Grouping is pre-computed server-side for every mode (cluster / component / team
/ verdict) and emitted as an ordering table, so switching group-by only
re-appends existing rows. Nothing about a rollup is recomputed in JavaScript —
one implementation, in Python, tested.

Colour is Testray-adjacent but not Testray's own palette: this file is read
outside the product (locally, in a terminal-adjacent workflow), where the RAP
report's higher-contrast verdict colours scan better than the in-product pills.
The in-app view (`liferay-testray-analytics-custom-element`) keeps Testray's
palette. That is a deliberate split, not drift.
"""

import collections
import html
import json
import re
import urllib.parse
from pathlib import Path

import pandas as pd

from . import error_signature, verdicts
from .jira_settings import DEFAULT_LABEL, resolve_jira_settings
from .prepare import TRANSITION_CHANGED

# Severity order — index doubles as the sort rank and drives _rollup().
# Severity order, as specified: BUG, POSSIBLE_BUG, NEEDS_REVIEW, TEST_FIX,
# FALSE_POSITIVE, DID_NOT_RUN. Index doubles as the sort rank and drives both
# _rollup() and the default ordering, so this list IS the report's opinion
# about what deserves attention first.
# The vocabulary lives in `verdicts.py` so the writer stores the same labels
# this renders. These names stay as aliases: they are used throughout the file
# and imported by the tests.
_VERDICT_ORDER = verdicts.VERDICT_ORDER
_UNATTRIBUTED_FROM = verdicts.UNATTRIBUTED_FROM
_UNATTRIBUTED_AT = verdicts.UNATTRIBUTED_AT
display_verdict = verdicts.display_verdict

# CSS class per verdict. Several non-actionable buckets share `auto` because
# they are all "the pipeline decided this without reasoning about it".
_VERDICT_CLASS = {
    "BUG": "bug",
    "POSSIBLE_BUG": "pbug",
    "TEST_FIX": "testfix",
    "NEEDS_REVIEW": "needs",
    "NOT_ATTRIBUTABLE": "unattr",
    "FALSE_POSITIVE": "fp",
    "ENV_FAILURE": "auto",
    "DID_NOT_RUN": "auto",
    "AUTO_CLASSIFIED": "auto",
    "PENDING": "auto",
}

# Long build logs are attached in full to the run bundle; the report shows
# enough to recognise the failure and says when it cut.
_ERROR_MAX = 2000

# Jira's CreateIssueDetails takes these as URL parameters, so an over-long
# field does not truncate gracefully — it makes a URL the browser or the
# gateway rejects, and the button silently does nothing. Cap here instead.
_JIRA_SUMMARY_MAX = 240
_JIRA_DESC_MAX = 8000

_GROUP_MODES = [
    ("cluster",   "error signature (cluster)"),
    ("component", "component"),
    ("team",      "team"),
    ("verdict",   "verdict only"),
]

_CSS = """
  :root {
    --c-bug: #c0392b;
    --c-pbug: #e74c3c;
    --c-needs: #d68910;
    --c-testfix: #2471a3;
    --c-fp: #5d6d7e;
    --c-auto: #7f8c8d;
    --c-bg: #fdfdfd;
    --c-fg: #1c1c1c;
    --c-muted: #5a6772;
    --c-border: #e1e4e8;
    --c-row: #f6f8fa;
    --c-code-bg: #eef1f4;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--c-bg); color: var(--c-fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }
  main { max-width: 1640px; margin: 0 auto; padding: 32px 28px 80px; }
  h1 { margin: 0 0 4px; font-size: 26px; }
  h1 a.build-link { color: #0747a6; text-decoration: none; }
  h1 a.build-link:hover { text-decoration: underline; }
  h1 .role {
    font-size: 13px; font-weight: 400; color: var(--c-muted);
    text-transform: uppercase; letter-spacing: .04em;
  }
  .summary {
    color: var(--c-muted); font-size: 14px; margin-bottom: 20px;
    padding-bottom: 14px; border-bottom: 1px solid var(--c-border);
  }
  h2 {
    margin: 36px 0 14px; font-size: 20px;
    border-bottom: 1px solid var(--c-border); padding-bottom: 6px;
  }
  code {
    background: var(--c-code-bg); padding: 1px 5px; border-radius: 3px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.92em;
  }
  /* ---- totals ---------------------------------------------------------- */
  /* Headline: counts on the left, the A x B matrix on the right. */
  /* Two columns: the controls you act with on the left, the numbers you read
     on the right, with the totals tucked under the matrix. Grid rather than
     flex on purpose — `minmax(0, 1fr)` guarantees the controls column can
     shrink below its content width, which three attempts at flex-basis plus
     min-width:0 could not do reliably (a flex item's automatic minimum size
     kept forcing the matrix onto its own row). */
  .headline {
    display: grid;
    /* The right track is capped: `auto` sized it to the totals pill row,
       which is very wide, and starved the controls column. Capping it lets
       the pills wrap under the matrix instead. */
    grid-template-columns: minmax(0, 1fr) minmax(0, 480px);
    gap: 18px; align-items: start; margin: 12px 0 14px;
  }
  .headline .controls {
    display: flex; flex-direction: column; gap: 10px; min-width: 0;
  }
  .headline .controls .viewbar,
  .headline .controls .filters { margin: 0; }
  .headline .side {
    display: flex; flex-direction: column; gap: 10px; min-width: 0;
  }
  @media (max-width: 1100px) {
    .headline { grid-template-columns: minmax(0, 1fr); }
  }
  /* Full width, above the control band: the counts are the first thing read,
     and they are the one element that benefits from the whole page width. */
  .totals {
    display: flex; flex-direction: row; flex-wrap: wrap;
    gap: 8px 16px; align-items: baseline;
    padding: 11px 14px; margin: 0; background: var(--c-row);
    border-radius: 4px; border: 1px solid var(--c-border);
  }
  .matrix {
    display: flex; flex-direction: column;
    padding: 12px 18px 14px; background: var(--c-row);
    border: 1px solid var(--c-border); border-radius: 4px;
  }
  .matrix table { margin: 0 auto; }
  /* Was "Runs", which named the thing without explaining how to read it.
     Wraps rather than forcing the panel wider. */
  .matrix-title {
    font-weight: 600; font-size: 12px; margin-bottom: 8px;
    color: var(--c-muted); line-height: 1.35; text-align: center;
  }
  .matrix table { width: auto; border-collapse: collapse; font-size: 12px; }
  .matrix th {
    position: static; background: none; border: none; padding: 5px 20px;
    color: var(--c-muted); font-weight: 600; text-align: center;
    font-size: 11px; line-height: 1.3; white-space: nowrap;
  }
  .matrix th span { font-weight: 700; color: var(--c-fg); }
  .matrix td {
    padding: 9px 20px; text-align: center; font-weight: 700;
    border: 1px solid #edf0f3; font-size: 13px; min-width: 74px;
  }
  /* The count stays the loud thing; the note is a caption under it, not a
     second number competing with it. */
  .matrix .cell-n { display: block; }
  .matrix .cell-note {
    display: block; font-size: 9.5px; font-weight: 500; line-height: 1.2;
    margin-top: 2px; opacity: .75; white-space: nowrap;
  }
  .matrix td.same   { background: #f4f6f8; color: var(--c-fg); }
  .matrix td.worse  { background: #fdecea; color: #a4302a; }
  .matrix td.better { background: #e9f7ef; color: #1e6b45; }
  /* Neither a fix nor a regression — counted, but making no claim. Kept
     visually distinct from the grey diagonal, which means "unchanged". */
  .matrix td.neutral { background: #fbfcfd; color: var(--c-fg); }
  .matrix td.zero   { background: #fbfcfd; }
  .totals .pill {
    display: inline-flex; align-items: baseline; gap: 6px; font-size: 13px;
  }
  .totals .pill .n { font-weight: 700; font-size: 16px; }
  /* The fan-out: how many case rows the clusters cover. Secondary on purpose
     — clusters are the unit of work, cases are the blast radius. */
  .totals .pill .fanout { color: var(--c-muted); font-size: 11.5px; }
  /* Coverage below the threshold: the reader must not skim past it. */
  .totals .pill.warn { background: #fff8e6; border-color: #e0b74a; }
  .totals a.pill { text-decoration: none; color: inherit; cursor: pointer; }
  .totals a.pill:hover { text-decoration: underline; }
  .verdict {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 12px; font-weight: 600; color: white; white-space: nowrap;
  }
  .verdict.bug { background: var(--c-bug); }
  .verdict.pbug { background: var(--c-pbug); }
  .verdict.needs { background: var(--c-needs); }
  /* Deliberately muted: it is the absence of a finding, not a finding. */
  .verdict.unattr { background: #8895a4; }
  .verdict.testfix { background: var(--c-testfix); }
  .verdict.fp { background: var(--c-fp); }
  .verdict.auto { background: var(--c-auto); }
  .conf { display: inline-block; font-size: 12px; color: var(--c-muted); text-transform: lowercase; }
  .conf.high { color: #1e8449; font-weight: 600; }
  .conf.medium { color: #b9770e; font-weight: 600; }
  .conf.low { color: #7d3c98; }
  .status {
    display: inline-block;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11px; color: var(--c-muted);
  }
  .status .arrow { color: var(--c-muted); margin: 0 3px; }
  .status .failed { color: var(--c-bug); font-weight: 600; }
  .status .passed { color: #1e8449; }
  .status .untested, .status .blocked { color: var(--c-needs); }
  /* ---- banners --------------------------------------------------------- */
  section.rationale {
    margin: 14px 0 8px; padding: 14px 18px;
    background: #fff8e7; border-left: 3px solid var(--c-needs);
    border-radius: 4px; font-size: 13.5px;
  }
  section.rationale h2 { margin: 0 0 8px; font-size: 15px; border: none; padding: 0; }
  /* A caveat about the data itself, not a finding in it — tinted so it is not
     skimmed past as commentary. */
  section.rationale.warn {
    background: #fff8e6; border-color: #e0b74a;
  }
  section.rationale.warn h2 { color: #7a5a00; }
  section.rationale ul, section.rationale ol { margin: 6px 0 0; padding-left: 22px; }
  section.rationale li { margin-bottom: 5px; }
  /* ---- controls -------------------------------------------------------- */
  .viewbar {
    display: flex; align-items: center; gap: 22px; flex-wrap: wrap; min-width: 0;
    margin: 18px 0 0; padding: 10px 14px;
    background: #f4f5f7; border: 1px solid var(--c-border);
    border-radius: 4px; font-size: 13px;
  }
  .viewbar-group { display: flex; align-items: center; gap: 8px; }
  .viewbar-label { font-weight: 600; }
  .viewbar-note { color: var(--c-muted); font-size: 12px; }
  .viewbar select, .filters select, .filters input[type="search"] {
    font: inherit; padding: 4px 6px; border: 1px solid var(--c-border);
    border-radius: 3px; background: #fff;
  }
  .filters {
    display: flex; flex-direction: column; gap: 8px; min-width: 0;
    margin: 18px 0 14px; padding: 10px 14px;
    background: var(--c-row); border: 1px solid var(--c-border);
    border-radius: 4px; font-size: 13px;
  }
  /* min-width:0 has to be repeated down the nesting: a flex item's default
     min-width is its min-content size, so without it the filter row keeps the
     controls column ~1580px wide and the matrix wraps to its own line. */
  .filters-row {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    min-width: 0;
  }
  .filters label { font-weight: 600; white-space: nowrap; }
  /* A label and its select are ONE flex item, so a wrap can never separate
     them — that is what stranded "Transition:" on a line above its dropdown. */
  .filter-field {
    display: flex; align-items: center; gap: 6px; min-width: 0; flex: 0 1 auto;
  }
  /* The selects must be able to shrink: five of them at a fixed 200px kept
     the controls column above 1000px, which forced the matrix to wrap onto
     its own row instead of sitting beside them. Widths are per-vocabulary so
     all five fit one row — Confidence holds four short words and does not
     need the same room as Component. */
  .filters select { min-width: 0; flex: 1 1 auto; }
  .ff-lg select { flex-basis: 130px; max-width: 190px; }
  .ff-md select { flex-basis: 104px; max-width: 132px; }
  .ff-sm select { flex-basis: 74px;  max-width: 92px; }
  .filters input[type="search"] { min-width: 0; flex: 1 1 220px; }
  .filters .visible-count { color: var(--c-muted); margin-left: auto; }
  button.cluster-btn {
    font: inherit; font-size: 12.5px; padding: 4px 10px;
    border: 1px solid var(--c-border); border-radius: 3px;
    background: #fff; color: #0747a6; cursor: pointer;
  }
  button.cluster-btn:hover { background: #f0f3f6; }
  p.hint { color: var(--c-muted); font-size: 12.5px; margin: 4px 0 8px; }
  p.hint kbd {
    background: #fff; border: 1px solid var(--c-border); border-bottom-width: 2px;
    border-radius: 3px; padding: 0 5px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px;
  }
"""

_CSS += """
  /* ---- table ----------------------------------------------------------- */
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; table-layout: auto; }
  thead th {
    position: sticky; top: 0; background: #f0f3f6; text-align: left;
    padding: 8px; border-bottom: 2px solid var(--c-border); font-weight: 600;
    cursor: pointer; user-select: none;
  }
  thead th .sort-ind { color: var(--c-muted); font-size: 11px; margin-left: 4px; }
  thead th.sorted .sort-ind { color: var(--c-fg); }
  tbody td {
    padding: 8px; border-bottom: 1px solid var(--c-border); vertical-align: top;
    overflow-wrap: anywhere; word-break: break-word;
  }
  th.col-idx { width: 62px; }
  td.col-idx { white-space: nowrap; color: var(--c-muted); }
  th.col-test, td.col-test { min-width: 220px; max-width: 340px; }
  td.col-test {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px;
  }
  td.col-test a.test-link { color: #0747a6; text-decoration: none; }
  td.col-test a.test-link:hover { text-decoration: underline; }
  th.col-comp { width: 130px; }
  th.col-team { width: 110px; }
  th.col-status { width: 130px; }
  /* Status wraps at whitespace only (between the two state spans), never
     mid-word — overrides the global overflow-wrap:anywhere. */
  td.col-status { word-break: normal; overflow-wrap: normal; }
  td.col-status .status > span { white-space: nowrap; }
  th.col-verdict { width: 130px; }
  th.col-confidence, td.col-confidence { width: 90px; text-align: center; }
  th.col-culprit, td.col-culprit { min-width: 220px; }
  th.col-reasoning, td.col-reasoning { min-width: 320px; }
  td.col-culprit, td.col-reasoning { white-space: normal; line-height: 1.45; }
  /* Issues already linked on the CaseResult. Its own column so a key is
     scannable down the table; the global overflow-wrap:anywhere would
     otherwise split one mid-token ("LPD-" / "99999"), so wrap BETWEEN keys. */
  /* 92px fitted "Ticket"; "Existing Ticket" needs the extra room to stay on
     one line beside its sort arrow. */
  th.col-ticket, td.col-ticket { width: 112px; text-align: center; }
  td.col-ticket { font-size: 11.5px; }
  .ticket-key {
    display: inline-block; overflow-wrap: normal; white-space: nowrap;
    word-break: normal;
  }
  .ticket-key + .ticket-key { margin-left: 4px; }
  .ticket-none { color: var(--c-muted); }

  /* A kebab needs a fraction of the 110px the "Create ticket" button did.
     Linked issues used to share this cell and forced it wider; they have
     their own Ticket column now. */
  th.col-jira, td.col-jira { width: 56px; text-align: center; }
  th[title], .filter-field[title], .viewbar-label[title] { cursor: help; }
  .actions-menu { display: inline-block; position: relative; }
  .actions-kebab {
    background: none; border: 1px solid transparent; border-radius: 3px;
    color: var(--c-muted); cursor: pointer; line-height: 0; padding: 4px 6px;
  }
  .actions-kebab:hover, .actions-kebab.is-open {
    background: #e4eaf0; border-color: #c9d2db; color: var(--c-fg);
  }
  .actions-kebab svg { fill: currentColor; }
  /* Right-aligned because this is the last column — a left-aligned menu
     would run off the page. z-index clears the rows below it. */
  .actions-list {
    background: #fff; border: 1px solid #cdd5dd; border-radius: 4px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.14);
    min-width: 190px; padding: 4px 0; position: absolute; right: 0;
    text-align: left; top: calc(100% + 2px); z-index: 20;
  }
  .actions-item {
    color: var(--c-fg); display: block; font-size: 12.5px;
    padding: 6px 12px; text-decoration: none; white-space: nowrap;
  }
  a.actions-item:hover { background: #eef1f4; }
  /* Disabled, not hidden: the menu is landing before two of its three
     actions work, and saying what is coming is the point. */
  .actions-item.is-pending {
    align-items: center; color: var(--c-muted); cursor: default;
    display: flex; gap: 8px; justify-content: space-between;
  }
  .actions-soon {
    background: #eef1f4; border-radius: 8px; color: var(--c-muted);
    font-size: 9.5px; letter-spacing: 0.04em; padding: 1px 6px;
    text-transform: uppercase;
  }
  /* ---- click-to-expand rows -------------------------------------------- */
  tr.case-row { cursor: pointer; }
  tr.case-row:hover td { background: #eef1f4; }
  tr.case-row td.col-idx::before { content: "\\25B8 "; color: var(--c-muted); font-size: 11px; }
  tr.case-row.expanded td.col-idx::before { content: "\\25BE "; color: var(--c-fg); }
  tr.case-row.expanded td { background: #f0f3f6; border-bottom-color: transparent; }
  tr.case-detail > td { padding: 0; background: #fafbfc; border-bottom: 2px solid var(--c-border); }
  tr.case-detail .case-detail-inner {
    padding: 14px 18px 16px; border-left: 3px solid var(--c-border); margin-left: 40px;
  }
  tr.case-detail.bug .case-detail-inner { border-left-color: var(--c-bug); }
  tr.case-detail.pbug .case-detail-inner { border-left-color: var(--c-pbug); }
  tr.case-detail.needs .case-detail-inner { border-left-color: var(--c-needs); }
  tr.case-detail.testfix .case-detail-inner { border-left-color: var(--c-testfix); }
  tr.case-detail.fp .case-detail-inner { border-left-color: var(--c-fp); }
  tr.case-detail.auto .case-detail-inner { border-left-color: var(--c-auto); }
  tr.case-detail dl {
    margin: 0; display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 6px 14px;
  }
  tr.case-detail dt { font-weight: 600; color: var(--c-muted); font-size: 13px; }
  tr.case-detail dd {
    margin: 0; font-size: 13px; min-width: 0;
    overflow-wrap: anywhere; word-break: break-word;
  }
  pre.error { white-space: pre-wrap; margin: 0; font-size: 12px; }
  /* ---- cluster header rows --------------------------------------------
     A header sits on the same column grid as its members: count under Test,
     rollups under Component / Team, shared culprit and reason under theirs.
     It reads as a header through weight and tint, not through a colspan. */
  tr.cluster-row { cursor: pointer; }
  tr.cluster-row > td {
    background: #eceff3; border-top: 2px solid #b3bac5;
    border-bottom: 1px solid var(--c-border); vertical-align: top;
    padding-top: 10px; padding-bottom: 11px;
  }
  tr.cluster-row:hover > td { background: #e4e8ee; }
  tr.cluster-row > td.col-idx {
    text-align: right; font-weight: 700; color: var(--c-muted);
    white-space: nowrap; border-left: 4px solid var(--c-border);
  }
  tr.cluster-row > td.col-idx::before { content: "\\25B8 "; color: var(--c-muted); font-size: 11px; }
  tr.cluster-row.expanded > td.col-idx::before { content: "\\25BE "; color: var(--c-fg); }
  tr.cluster-row.bug > td.col-idx { border-left-color: var(--c-bug); }
  tr.cluster-row.pbug > td.col-idx { border-left-color: var(--c-pbug); }
  tr.cluster-row.needs > td.col-idx { border-left-color: var(--c-needs); }
  tr.cluster-row.testfix > td.col-idx { border-left-color: var(--c-testfix); }
  tr.cluster-row.fp > td.col-idx { border-left-color: var(--c-fp); }
  tr.cluster-row.auto > td.col-idx { border-left-color: var(--c-auto); }
  tr.cluster-row > td.col-test { font-family: inherit; white-space: nowrap; }
  tr.cluster-row .cluster-n { font-weight: 700; font-size: 13px; }
  tr.cluster-row > td.cluster-rollup { font-size: 11.5px; color: #42526e; white-space: normal; }
  tr.cluster-row > td.cluster-rollup.multiple { font-style: italic; color: var(--c-muted); cursor: help; }
  tr.cluster-row > td.cluster-culprit { font-size: 12px; color: #42526e; white-space: normal; }
  tr.cluster-row .cluster-culprit-none { color: var(--c-muted); font-style: italic; }
  tr.cluster-row > td.cluster-reason { font-size: 12.5px; line-height: 1.5; white-space: normal; }
  tr.cluster-row .cluster-title { font-weight: 700; }
  .cluster-reason-mixed { font-style: italic; color: #42526e; }
  /* Member rows: indented, shared cells collapse to a pointer. */
  tr.case-row.in-cluster td.col-idx { padding-left: 18px; }
  .same-as-cluster { color: var(--c-muted); font-size: 12px; white-space: nowrap; }
  /* The arrow is only meaningful while the visible group IS the cluster. */
  table.per-test-table[data-mode="cluster"] .own-value { display: none; }
  table.per-test-table:not([data-mode="cluster"]) .same-as-cluster { display: none; }
  .same-as-cluster a { color: #0747a6; text-decoration: none; }
  .same-as-cluster a:hover { text-decoration: underline; }
  .cluster-key { font-size: 11px; color: var(--c-muted); }
  details.pre-existing { margin-top: 28px; }
  details.pre-existing > summary {
    cursor: pointer; padding: 8px 10px; border: 1px solid var(--c-border);
    border-radius: 4px; background: var(--c-row); font-size: 14px;
  }
  details.pre-existing > summary .hint {
    display: block; font-weight: 400; margin-top: 2px;
  }
  details.pre-existing > table { margin-top: 10px; }

  /* Reads as a link, not as a code chip. The grey `code` background made it
     look like the inert run-id and mode chips beside it in the summary line,
     so the one clickable thing there was the one nobody clicked. Underlined by
     default rather than on hover — hover discovery does not work for something
     a reader has no reason to hover over. */
  a.hash-link code {
    background: none; padding: 0; border-radius: 0;
    color: #0747a6; text-decoration: underline;
  }
  a.hash-link { text-decoration: none; }
  a.hash-link:hover code { color: #0747a6; text-decoration: underline;
    text-decoration-thickness: 2px; }
  /* Handoff prompt. The text is always visible and selectable, so the copy
     button is a convenience rather than the only route: an artifact renders in
     a sandboxed frame where the clipboard API can be blocked outright. */
  .handoff { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
  /* The prompt rides inside the menu as hidden text for the copy handler
     to read; it must never take part in the menu's layout. */
  .actions-list pre.handoff-text { display: none !important; }
  button.actions-item.handoff-copy {
    background: none; border: 0; color: var(--c-fg); cursor: pointer;
    font: inherit; font-size: 12.5px; padding: 6px 12px; text-align: left;
    width: 100%;
  }
  button.actions-item.handoff-copy:hover { background: #eef1f4; }
  button.handoff-copy {
    font: inherit; font-size: 12px; padding: 3px 10px; cursor: pointer;
    background: #0052cc; color: #fff; border: 1px solid #0747a6;
    border-radius: 3px;
  }
  button.handoff-copy:hover { background: #0747a6; }
  button.handoff-copy.copied { background: #1e6b45; border-color: #1e6b45; }
  .handoff-hint { color: var(--c-muted); font-size: 11.5px; }
  pre.handoff-text {
    margin: 0; padding: 10px 12px; width: 100%; max-height: 260px;
    overflow: auto; background: var(--c-code-bg);
    border: 1px solid var(--c-border); border-radius: 3px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11.5px; line-height: 1.45; white-space: pre-wrap;
  }
  .pre-count { color: var(--c-muted); font-weight: 400; margin-left: 8px;
    font-size: 12px; }
  details.verdict-legend { margin-top: 14px; }
  details.verdict-legend > summary {
    cursor: pointer; padding: 8px 10px; border: 1px solid var(--c-border);
    border-radius: 4px; background: var(--c-row); font-size: 14px;
  }
  details.verdict-legend > summary .hint {
    display: block; font-weight: 400; margin-top: 2px;
  }
  .legend-grid {
    display: grid; grid-template-columns: max-content 1fr;
    gap: 8px 14px; align-items: baseline; margin: 12px 4px 4px;
  }
  .legend-item { display: contents; }
  .legend-text { color: var(--c-fg); font-size: 12.5px; line-height: 1.5; }

  /* Candidate tickets, shown when the classifier named causes but no file. */
  .cause-tickets { display: flex; flex-wrap: wrap; gap: 4px; }
  a.cause-ticket {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    background: var(--c-code-bg); border: 1px solid var(--c-border);
    color: #0747a6; text-decoration: none;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px;
  }
  a.cause-ticket:hover { text-decoration: underline; }

  /* Flaky is a modifier on the verdict, not a verdict — hence a badge that
     sits beside the pill rather than another pill colour. */
  .flaky-badge {
    display: inline-block; margin-left: 4px; padding: 0 5px;
    border: 1px dashed #b7791f; border-radius: 3px;
    color: #8a6116; background: #fdf6e3;
    font-size: 10px; font-weight: 700; letter-spacing: .04em; vertical-align: middle;
  }

  /* The ticket/commit that touched the culprit file — the actionable half. */
  .culprit-commits {
    font-size: 11.5px; color: #42526e; margin-top: 3px; line-height: 1.4;
  }
"""

# Client-side behaviour. Group-by only re-appends rows using the ordering
# table emitted by Python — no rollup or clustering logic is duplicated here.
_JS = """
(function () {
  var ORDER = JSON.parse(document.getElementById('group-order').textContent);
  var tbody = document.querySelector('table.per-test-table tbody');
  var mode = 'cluster';

  function rowsOf(id) {
    var tr = document.getElementById(id);
    if (!tr) return [];
    var out = [tr];
    var nxt = tr.nextElementSibling;
    if (nxt && nxt.classList.contains('case-detail')) out.push(nxt);
    return out;
  }

  function renumber() {
    var n = 0;
    (ORDER[mode] || []).forEach(function (g) {
      g.rows.forEach(function (rid) {
        var tr = document.getElementById(rid);
        if (!tr) return;
        var cell = tr.querySelector('td.col-num');
        if (cell) cell.textContent = ++n;
      });
    });
  }

  function regroup(next) {
    mode = next;
    // Drives the collapse CSS: members may only show "↑" when the visible
    // group is the cluster itself.
    var tbl = document.querySelector('table.per-test-table');
    if (tbl) tbl.dataset.mode = mode;
    var groups = ORDER[mode] || [];
    document.querySelectorAll('tr.cluster-row').forEach(function (h) {
      h.hidden = true;
    });
    groups.forEach(function (g) {
      var head = document.getElementById(g.h);
      if (head) { head.hidden = false; tbody.appendChild(head); }
      g.rows.forEach(function (rid) {
        rowsOf(rid).forEach(function (tr) { tbody.appendChild(tr); });
      });
    });
    renumber();
    applyFilters();
  }

  // --- expand / collapse -------------------------------------------------
  // Membership comes from the ordering table, never from walking siblings:
  // headers for the three inactive group modes stay in the tbody (hidden), so
  // a nextElementSibling walk can run straight through them and claim rows
  // that belong to a different header.
  function groupOf(headId) {
    return (ORDER[mode] || []).filter(function (g) { return g.h === headId; })[0];
  }

  function setCluster(head, open, defer) {
    var g = groupOf(head.id);
    if (!g) return;                   // a header for some other group mode
    head.classList.toggle('expanded', open);
    g.rows.forEach(function (rid) {
      var tr = document.getElementById(rid);
      if (!tr) return;
      tr.dataset.collapsed = open ? '' : '1';
      if (!open) {
        var d = tr.nextElementSibling;
        if (d && d.classList.contains('case-detail')) d.hidden = true;
      }
    });
    if (!defer) applyFilters();
  }

  function setAll(open) {
    (ORDER[mode] || []).forEach(function (g) {
      var head = document.getElementById(g.h);
      if (head) setCluster(head, open, true);
    });
    applyFilters();
  }

  // ---- handoff prompt ---------------------------------------------------
  // Selects the text as the fallback rather than reporting failure: in a
  // sandboxed frame navigator.clipboard can be missing or rejected, and a
  // selected block still answers Ctrl-C.
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('button.handoff-copy');
    if (!btn) return;
    // Inside the kebab the menu's own handler would otherwise treat this as a
    // click on a menu item and close the menu before the copy resolves.
    e.stopPropagation();
    e.preventDefault();
    var scope = btn.closest('.actions-list') || btn.parentNode;
    var pre = scope.querySelector('pre.handoff-text');
    if (!pre) return;

    // execCommand('copy') via a throwaway textarea. Needed because
    // navigator.clipboard is rejected outright in a sandboxed artifact frame
    // (NotAllowedError), and because selecting the menu's hidden <pre> copies
    // NOTHING — a display:none element has no selectable content, so the
    // earlier "Selected — press Ctrl-C" fallback was telling the reader
    // something untrue. This actually puts the text on the clipboard.
    function legacyCopy(text) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '0';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
      document.body.removeChild(ta);
      return ok;
    }
    function done(label) {
      var was = btn.textContent;
      btn.textContent = label;
      btn.classList.add('copied');
      setTimeout(function () {
        btn.textContent = was;
        btn.classList.remove('copied');
      }, 1600);
    }

    var text = pre.textContent;
    function fallback() {
      // Only ever report what actually happened. If both routes fail the
      // prompt is still readable and selectable in the row's detail panel,
      // so say that rather than claiming a copy that did not occur.
      done(legacyCopy(text) ? 'Copied' : 'Copy failed — open row detail');
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done('Copied'); },
                                              fallback);
    } else {
      fallback();
    }
  }, true);

  // ---- actions kebab ----------------------------------------------------
  // One open menu at a time. The row underneath expands on click, so every
  // path here returns before the row handler below can see the event — the
  // CX gets this from stopPropagation on the wrapper; here the guard in the
  // row handler does the same job.
  function closeMenus(except) {
    document.querySelectorAll('.actions-menu').forEach(function (m) {
      if (m === except) return;
      m.querySelector('.actions-list').hidden = true;
      var b = m.querySelector('.actions-kebab');
      b.classList.remove('is-open');
      b.setAttribute('aria-expanded', 'false');
    });
  }

  document.addEventListener('click', function (e) {
    var kebab = e.target.closest('.actions-kebab');
    if (kebab) {
      var menu = kebab.closest('.actions-menu');
      var list = menu.querySelector('.actions-list');
      var open = list.hidden;
      closeMenus(menu);
      list.hidden = !open;
      kebab.classList.toggle('is-open', open);
      kebab.setAttribute('aria-expanded', open ? 'true' : 'false');
      return;
    }
    if (!e.target.closest('.actions-menu')) closeMenus(null);
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeMenus(null);
  });

  document.addEventListener('click', function (e) {
    // A click inside the menu must not also fold the cluster or expand the row.
    if (e.target.closest('.actions-menu') || e.target.closest('.handoff')) return;
    var head = e.target.closest('tr.cluster-row');
    if (head && !e.target.closest('a')) {
      setCluster(head, !head.classList.contains('expanded'));
      return;
    }
    var row = e.target.closest('tr.case-row');
    if (row && !e.target.closest('a')) {
      var detail = row.nextElementSibling;
      if (!detail || !detail.classList.contains('case-detail')) return;
      detail.hidden = !detail.hidden;
      row.classList.toggle('expanded', !detail.hidden);
    }
  });

  // --- filters -----------------------------------------------------------
  var sel = {
    team: document.getElementById('team-filter'),
    verdict: document.getElementById('verdict-filter'),
    confidence: document.getElementById('confidence-filter'),
    component: document.getElementById('component-filter'),
    transition: document.getElementById('transition-filter')
  };
  var search = document.getElementById('search-filter');
  var count = document.getElementById('filter-count');

  function matches(tr) {
    for (var k in sel) {
      if (!sel[k] || !sel[k].value) continue;
      if ((tr.dataset[k] || '') !== sel[k].value) return false;
    }
    var q = (search.value || '').trim().toLowerCase();
    if (!q) return true;
    var text = tr.textContent || '';
    var detail = tr.nextElementSibling;
    if (detail && detail.classList.contains('case-detail')) {
      text += ' ' + (detail.textContent || '');
    }
    return text.toLowerCase().indexOf(q) !== -1;
  }

  function applyFilters() {
    var filtering = !!(search.value.trim()) || Object.keys(sel).some(function (k) {
      return sel[k] && sel[k].value;
    });
    var shown = 0;
    var matched = 0;
    document.querySelectorAll('tr.case-row').forEach(function (tr) {
      var hit = matches(tr);
      if (hit) matched++;
      // `collapsed` is ALWAYS honoured. This used to read
      // `!hit || (!filtering && collapsed)`, which kept a matching row visible
      // no matter what — so while a filter was active a cluster could not be
      // collapsed at all: the header arrow flipped and the rows stayed put.
      // Revealing folded-away matches is still wanted, but it belongs at the
      // moment a filter CHANGES (onFilterChanged), not in a rule that
      // overrides every later click.
      var hide = !hit || tr.dataset.collapsed === '1';
      // Whether a row MATCHED is distinct from whether it is displayed: a
      // collapsed cluster still owns its rows, so its header must stay
      // clickable. Conflating the two made "Collapse all" hide the headers
      // as well, leaving nothing to click.
      tr.dataset.matched = hit ? '1' : '';
      tr.hidden = hide;
      if (!hide) shown++;
      if (hide) {
        var d = tr.nextElementSibling;
        if (d && d.classList.contains('case-detail')) d.hidden = true;
      }
    });
    (ORDER[mode] || []).forEach(function (g) {
      var head = document.getElementById(g.h);
      if (!head) return;
      var any = g.rows.some(function (rid) {
        var tr = document.getElementById(rid);
        return tr && tr.dataset.matched === '1';
      });
      head.hidden = !any;
    });
    // Report MATCHES, not visible rows. Counting what is displayed read
    // "0 of 93 shown" whenever the table was collapsed — which is its default
    // state — and that looks like nothing matched rather than nothing expanded.
    var total = document.querySelectorAll('tr.case-row').length;
    count.textContent = filtering
      ? matched + ' of ' + total + ' match'
      : total + (total === 1 ? ' row' : ' rows');

    // The pre-existing rows are on the page, so the filters have to reach them
    // too — otherwise selecting Transition=same_failure emptied the table and
    // silently left seven matching rows sitting below it. They carry the same
    // data-* keys, and no data-verdict/-confidence, so a verdict or confidence
    // filter correctly excludes them: they were never classified.
    var pre = document.querySelectorAll('tr.pre-row');
    if (pre.length) {
      var preShown = 0;
      pre.forEach(function (tr) {
        var hit = matches(tr);
        tr.hidden = !hit;
        if (hit) preShown++;
      });
      var box = document.querySelector('details.pre-existing');
      var label = document.getElementById('pre-count');
      if (label) {
        label.textContent = filtering
          ? preShown + ' of ' + pre.length + ' match'
          : '';
      }
      // A match must not stay hidden inside a folded section, the same reason a
      // hit inside a collapsed cluster is forced open.
      if (box && filtering && preShown) box.open = true;
    }
  }

  var wasFiltering = false;

  function isFiltering() {
    if (search.value.trim()) return true;
    return Object.keys(sel).some(function (k) {
      return sel[k] && sel[k].value;
    });
  }

  // Runs when a FILTER control changes — never when a cluster is toggled, which
  // is the distinction that lets an explicit collapse survive an active filter.
  function onFilterChanged() {
    // Filtering narrows the OVERVIEW, so it collapses rather than expands: the
    // matching cluster headers are the answer, and their members are one click
    // away. An earlier version expanded every matching group, which turned a
    // filter into a wall of member rows and buried the thing being looked for.
    //
    // Nothing is hidden by this. applyFilters marks a row matched whether or
    // not it is displayed, and a header shows whenever any of its members
    // matched — so a search still surfaces a hit inside a folded cluster, as
    // that header.
    setAll(false);
    wasFiltering = isFiltering();
    applyFilters();
  }

  Object.keys(sel).forEach(function (k) {
    if (sel[k]) sel[k].addEventListener('change', onFilterChanged);
  });
  search.addEventListener('input', onFilterChanged);
  search.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { search.value = ''; onFilterChanged(); }
  });
  document.getElementById('group-by').addEventListener('change', function (e) {
    regroup(e.target.value);
  });
  document.getElementById('expand-clusters').addEventListener('click', function () {
    setAll(true);
  });
  document.getElementById('collapse-clusters').addEventListener('click', function () {
    setAll(false);
  });
  document.querySelectorAll('[data-pill-for]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      sel.verdict.value = a.dataset.pillFor;
      applyFilters();
    });
  });

  // --- sort, within each cluster ----------------------------------------
  function cellKey(td) {
    // An explicit data-sort wins over the cell's text: verdict and confidence
    // are ordinal, and sorting them alphabetically puts DID_NOT_RUN above BUG.
    if (td && td.dataset && td.dataset.sort !== undefined) {
      return [0, Number(td.dataset.sort)];
    }
    var t = (td.textContent || '').trim();
    var n = Number(t.replace(/,/g, ''));
    return Number.isFinite(n) && t !== '' ? [0, n] : [1, t.toLowerCase()];
  }
  function cmp(a, b) {
    if (a[0] !== b[0]) return a[0] - b[0];
    return a[1] < b[1] ? -1 : (a[1] > b[1] ? 1 : 0);
  }
  var table = document.querySelector('table.per-test-table');
  var ths = table.querySelectorAll('thead th');
  ths.forEach(function (th, idx) {
    var ind = document.createElement('span');
    ind.className = 'sort-ind';
    ind.textContent = '\\u2195';
    th.appendChild(ind);
    th.addEventListener('click', function () {
      var asc = !th.classList.contains('asc');
      ths.forEach(function (h) {
        h.classList.remove('sorted', 'asc', 'desc');
        h.querySelector('.sort-ind').textContent = '\\u2195';
      });
      th.classList.add('sorted', asc ? 'asc' : 'desc');
      ind.textContent = asc ? '\\u25B2' : '\\u25BC';
      // Sort the CLUSTERS by the same column first, then the members inside
      // each. Sorting only within clusters left the cluster order fixed, so
      // clicking Verdict appeared to do nothing to the headers.
      var order = (ORDER[mode] || []).slice();
      order.sort(function (ga, gb) {
        var ha = document.getElementById(ga.h), hb = document.getElementById(gb.h);
        if (!ha || !hb) return 0;
        var ca = ha.children[idx], cb = hb.children[idx];
        if (!ca || !cb) return 0;
        // A cluster header's verdict/confidence cells carry the rollup, so
        // sorting on them sorts by "worst member", which is the useful sense.
        return asc ? cmp(cellKey(ca), cellKey(cb)) : cmp(cellKey(cb), cellKey(ca));
      });
      ORDER[mode] = order;
      order.forEach(function (g) {
        var groups = g.rows.map(rowsOf).filter(function (x) { return x.length; });
        groups.sort(function (a, b) {
          var c1 = a[0].children[idx], c2 = b[0].children[idx];
          if (!c1 || !c2) return 0;
          return asc ? cmp(cellKey(c1), cellKey(c2)) : cmp(cellKey(c2), cellKey(c1));
        });
        var head = document.getElementById(g.h);
        if (head) tbody.appendChild(head);
        groups.forEach(function (grp) {
          grp.forEach(function (tr) { tbody.appendChild(tr); });
        });
        // The ordering table drives numbering and membership, so keep it in
        // step with the DOM the sort just produced.
        g.rows = groups.map(function (grp) { return grp[0].id; });
      });
      renumber();
    });
  });

  // Rows are emitted headers-first, then all members, so the initial order
  // must be built the same way a group-by switch builds it. Start COLLAPSED:
  // the cluster list is the overview, and expanding every member on load
  // buries it under hundreds of rows.
  regroup('cluster');
  setAll(false);
})();
"""


def _esc(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return html.escape(str(v))


def _text(v) -> str:
    """NaN-safe scalar to str. pandas hands back float('nan') for missing
    cells and NaN is truthy, so `v or ''` is not enough."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


_rank = verdicts.rank
_rollup = verdicts.rollup


def _vclass(verdict) -> str:
    return _VERDICT_CLASS.get(_text(verdict), "auto")


# Explicit sort ranks. Without these the table sorts these columns as TEXT,
# so clicking "Verdict" orders DID_NOT_RUN before BUG — alphabetical, and the
# exact opposite of useful. The rank rides on the cell as data-sort so the
# generic sorter stays generic.
_CONF_ORDER = {"high": 0, "medium": 1, "low": 2}


def _verdict_rank_attr(verdict) -> str:
    return f' data-sort="{_rank(verdict):02d}"'


def _conf_rank_attr(confidence) -> str:
    c = _text(confidence).lower()
    return f' data-sort="{_CONF_ORDER.get(c, 9)}"'


def _verdict_span(verdict) -> str:
    v = _text(verdict)
    if not v:
        return '<span class="conf">—</span>'
    return f'<span class="verdict {_vclass(v)}">{_esc(v)}</span>'


def _conf_span(confidence) -> str:
    c = _text(confidence).lower()
    if not c:
        return '<span class="conf">auto</span>'
    return f'<span class="conf {_esc(c)}">{_esc(c)}</span>'


def _status_span(a, b) -> str:
    sa, sb = _text(a), _text(b)
    if not sa and not sb:
        return ""
    if not sa or not sb:
        one = sb or sa
        return f'<span class="status"><span class="{one.lower()}">{_esc(one)}</span></span>'
    return ('<span class="status">'
            f'<span class="{sa.lower()}">{_esc(sa)}</span> '
            '<span class="arrow">&rarr;</span> '
            f'<span class="{sb.lower()}">{_esc(sb)}</span></span>')


def _truncate(value, limit=_ERROR_MAX) -> str:
    t = _text(value)
    if len(t) <= limit:
        return t
    return t[:limit] + "\n… [truncated]"


def _cluster_of(row) -> str:
    return error_signature.cluster_key(row.get("culprit_file"),
                                       row.get("test_case"),
                                       row.get("error_message"))


def _rollup_cell(values, css) -> str:
    """One value, or `multiple` with the breakdown on hover.

    Showing the first of thirty components would be a quiet lie, and listing
    all thirty would blow the column, so the cell states that it is mixed and
    keeps the counts in the title.
    """
    counts = collections.Counter(v for v in (_text(x) for x in values) if v)
    if not counts:
        return f'<td class="{css} cluster-cell cluster-rollup">—</td>'
    if len(counts) == 1:
        only = next(iter(counts))
        return f'<td class="{css} cluster-cell cluster-rollup">{_esc(only)}</td>'
    breakdown = " · ".join(f"{k} {n}" for k, n in counts.most_common())
    return (f'<td class="{css} cluster-cell cluster-rollup multiple" '
            f'title="{_esc(breakdown)}">multiple</td>')


def _case_url(meta: dict, caseresult_id) -> str:
    """Testray deep-link for a case result, or "" when it cannot be built.

    Needs a base url and a project id, and `prepare` does not record a project
    id in run.yml yet, so most runs will render plain text. Emitting a
    half-built link would be worse than none — a broken deep-link reads as a
    Testray bug rather than a missing field here.
    """
    base = _text(meta.get("testray_url"))
    project = _text(meta.get("project_id") or meta.get("project_id_b"))
    routine = _text(meta.get("routine_id"))
    build = _text(meta.get("build_id_b"))
    cr = _text(caseresult_id)
    if not (base and project and routine and build and cr):
        return ""
    # `<base>#/…`, not `<base>/#/…` — the UI base already carries the site
    # path (/web/liferay-testray), and that is the form Testray's own links use.
    return (f"{base.rstrip('/')}#/project/{project}/routines/{routine}"
            f"/build/{build}/case-result/{cr}")


def _build_url(meta: dict, build_id) -> str:
    base = _text(meta.get("testray_url"))
    project = _text(meta.get("project_id") or meta.get("project_id_b"))
    routine = _text(meta.get("routine_id"))
    if not (base and project and routine and _text(build_id)):
        return ""
    return (f"{base.rstrip('/')}#/project/{project}/routines/{routine}"
            f"/build/{_text(build_id)}")


def _groups(df: pd.DataFrame, ckeys: list[str]) -> dict:
    """Ordered groups per mode: {mode: [(label, [positional index, …]), …]}.

    Cluster mode drives the default view and is ordered severity-first, then
    by size — a 30-member NEEDS_REVIEW cluster still sorts below a single BUG,
    because the BUG is the thing to act on. The other modes are ordered the
    same way so the eye learns one rule.
    """
    if df is None or df.empty:
        return {mode: [] for mode, _ in _GROUP_MODES}

    keys = {
        "cluster":   list(ckeys),
        "component": [_text(v) or "(no component)" for v in df.get("component_name", [])],
        "team":      [_text(v) or "(no team)" for v in df.get("team_name", [])],
        "verdict":   [_text(v) or "(unclassified)" for v in df.get("display_verdict", [])],
    }
    verdicts = list(df.get("display_verdict", []))

    out = {}
    for mode, _label in _GROUP_MODES:
        buckets: dict[str, list[int]] = collections.defaultdict(list)
        for i, key in enumerate(keys[mode]):
            buckets[key].append(i)
        ordered = sorted(
            buckets.items(),
            key=lambda kv: (_rank(_rollup([verdicts[i] for i in kv[1]])),
                            -len(kv[1]), kv[0]),
        )
        out[mode] = ordered
    return out


def _header_row(mode: str, idx: int, label: str, members: pd.DataFrame,
                cluster_no: int | None, meta: dict) -> str:
    """A cluster header, on the same column grid as its members."""
    verdict = _rollup(members.get("display_verdict", []))
    css = _vclass(verdict)

    confs = [c for c in (_text(x).lower() for x in members.get("confidence", [])) if c]
    order = {"high": 0, "medium": 1, "low": 2}
    top = min(confs, key=lambda c: order.get(c, 9)) if confs else ""
    breakdown = " · ".join(f"{k} {n}" for k, n in collections.Counter(confs).most_common())

    culprits = {c for c in (_text(x) for x in members.get("culprit_file", [])) if c}
    if len(culprits) == 1:
        only = next(iter(culprits))
        culprit_cell = f"<code>{_esc(only)}</code>"
        commits = {c for c in (_text(x) for x in members.get("culprit_commits", [])) if c}
        if len(commits) == 1:
            culprit_cell += (f'<div class="culprit-commits">'
                             f'{_esc(next(iter(commits)))}</div>')
    elif culprits:
        culprit_cell = (f'<span class="cluster-culprit-none" title="{_esc(" · ".join(sorted(culprits)))}">'
                        f"{len(culprits)} files</span>")
    else:
        # No file named anywhere in the cluster — fall back to the candidate
        # tickets its members named instead, so the header carries the same
        # actionable content the member rows now do.
        chips = _cause_tickets(meta, " ; ".join(
            _text(x) for x in members.get("specific_change", [])))
        culprit_cell = (f'<div class="cause-tickets">{chips}</div>' if chips
                        else '<span class="cluster-culprit-none">no cause named</span>')

    reasons = {r for r in (_text(x) for x in members.get("reason", [])) if r}
    if len(reasons) == 1:
        reason_cell = f'<strong class="cluster-title">{_esc(next(iter(reasons)))}</strong>'
    elif reasons:
        reason_cell = (f'<span class="cluster-reason-mixed">{len(reasons)} distinct reasons '
                       f"— see the member rows</span>")
    else:
        reason_cell = '<span class="cluster-reason-mixed">no reasoning recorded</span>'

    # In cluster mode the label is the clusterKey, which is the useful thing to
    # show; in the other modes the label IS the group (a team, a component).
    if mode == "cluster":
        title = f'<span class="cluster-key">{_esc(label)}</span>'
    else:
        title = f"<strong>{_esc(label)}</strong>"

    n = len(members)
    return (
        f'<tr class="cluster-row {css}" id="grp-{mode}-{idx}" '
        f'data-group-mode="{mode}"{"" if mode == "cluster" else " hidden"}>'
        f'<td class="col-idx col-cluster-caret">{cluster_no if cluster_no else idx + 1}</td>'
        f'<td class="col-test cluster-cell"><span class="cluster-n">{n} test'
        f'{"" if n == 1 else "s"}</span><br>{title}</td>'
        + _rollup_cell(members.get("team_name", []), "col-team")
        + _rollup_cell(members.get("component_name", []), "col-comp")
        + '<td class="col-status cluster-cell"></td>'
        + f'<td class="col-verdict cluster-cell"{_verdict_rank_attr(verdict)}>'
          f'{_verdict_span(verdict)}{_flaky_badge(members)}</td>'
        f'<td class="col-confidence cluster-cell"{_conf_rank_attr(top)}'
        + (f' title="Highest confidence in this group. Breakdown: {_esc(breakdown)}"' if breakdown else "")
        + f">{_conf_span(top)}</td>"
        f'<td class="col-culprit cluster-cell cluster-culprit">{culprit_cell}</td>'
        f'<td class="col-reasoning cluster-cell cluster-reason">{reason_cell}</td>'
        + _ticket_cell(members.get("linked_issues", []), cluster=True)
        + f'<td class="col-jira cluster-cell">'
        + _actions_menu(
            _jira_href(meta, verdict=verdict,
                       summary_text=(next(iter(reasons)) if len(reasons) == 1 else ''),
                       rows=[members.iloc[0]] if len(members) else [],
                       n=n),
            label="Actions for this cluster",
            prompt=(_reviewer_prompt(
                        members.iloc[0], meta,
                        [_text(t) for t in members.get("test_case", [])],
                        verdict=verdict)
                    if verdict in _NEEDS_HUMAN and len(members) else None))
        + '</td>'
        "</tr>"
    )


def _member_rows(df: pd.DataFrame, meta: dict, cluster_no: dict[str, int],
                 ckeys: list[str], shared: dict) -> str:
    """Every member row plus its detail row, rendered once.

    Rendered once and re-appended by the group-by handler rather than
    re-rendered per mode: a row's identity does not change when you regroup it,
    and duplicating rows would duplicate their ids.
    """
    # clusterKey -> its member test names, one pass. Not recomputed per row:
    # see the warning in _cluster_index about per-row _cluster_of calls.
    tests_by_cluster: dict[str, list[str]] = {}
    for pos, ck in enumerate(ckeys):
        name = _text(df.iloc[pos].get("test_case"))
        if name:
            tests_by_cluster.setdefault(ck, []).append(name)

    parts = []
    for i, (_, r) in enumerate(df.iterrows()):
        verdict = _text(r.get("display_verdict")) or _text(r.get("classification"))
        css = _vclass(verdict)
        rid = f"row-{i}"
        ckey = ckeys[i]
        cno = cluster_no.get(ckey)

        test = _text(r.get("test_case")) or "(unnamed)"
        url = _case_url(meta, r.get("caseresult_id"))
        test_cell = (f'<a class="test-link" href="{_esc(url)}" target="_blank" '
                     f'rel="noopener" title="Open in Testray">{_esc(test)}</a>'
                     if url else _esc(test))

        reason = _text(r.get("reason"))
        culprit = _text(r.get("culprit_file"))
        # Collapse the two shared cells to a pointer at the cluster that owns
        # them. Repeating an identical paragraph on thirty rows is what made
        # the flat table unreadable; the full text is still one click away in
        # the detail panel, and on hover.
        # Render the pointer AND the full value. Which one shows depends on the
        # ACTIVE group-by, which changes at runtime — so it cannot be decided
        # here. Collapsing to "↑" is only honest while the visible group IS the
        # cluster; under group-by team/component/verdict the header says
        # "N distinct reasons" and the arrow would point at a cluster that is
        # not on screen.
        def _collapsible(full_html: str, raw: str) -> str:
            return (f'<span class="same-as-cluster" title="{_esc(raw)}">'
                    f'<a href="#grp-cluster-{cno}">&uarr;</a></span>'
                    f'<span class="own-value">{full_html}</span>')
        if cno is not None and _shared_in_cluster(shared, ckey, "reason", reason):
            reason_cell = _collapsible(_esc(reason) or "—", reason)
        else:
            reason_cell = _esc(reason) or "—"
        if cno is not None and culprit and _shared_in_cluster(shared, ckey, "culprit_file", culprit):
            culprit_cell = _collapsible(f"<code>{_esc(culprit)}</code>", culprit)
        else:
            culprit_cell = _cause_cell(meta, culprit,
                                       _text(r.get("culprit_commits")),
                                       r.get("specific_change"))

        # A cluster with one verdict says it once, on its header. Repeating it
        # on every member is the noise that made the flat table unreadable.
        # When a cluster IS mixed, the per-row value is the whole point, so it
        # stays.
        if cno is not None and _shared_in_cluster(shared, ckey, "display_verdict", verdict):
            verdict_cell = _collapsible(_verdict_span(verdict), verdict)
        else:
            verdict_cell = _verdict_span(verdict)
        # Flaky is a MODIFIER, not a verdict. A test can be known-flaky and
        # still need review — the two answer different questions ("how much do
        # we trust this signal" vs "what is the failure"), so the badge sits
        # beside the pill instead of replacing it. Same shape as
        # NOT_ATTRIBUTABLE in verdicts.py: display-only, nothing stored.
        if _is_flaky(r):
            verdict_cell += (' <span class="flaky-badge" title="Marked flaky on '
                             'the Testray case — excluded from classification, '
                             'shown for awareness.">FLAKY</span>')
        conf = _text(r.get("confidence"))
        if cno is not None and _shared_in_cluster(shared, ckey, "confidence", conf):
            conf_cell = _collapsible(_conf_span(conf), conf)
        else:
            conf_cell = _conf_span(r.get("confidence"))

        # Ticket and Actions are separate columns now: "has this been filed
        # already" is a fact about the failure, "file it" is something you do.
        # They used to share one cell, which meant a row with a linked issue
        # offered no way to act on it at all.
        ticket_cell = _ticket_cell([r.get("linked_issues")])
        actions_cell = _actions_menu(
            _jira_href(meta, verdict=verdict, summary_text=reason,
                       rows=[r], n=1),
            prompt=(_reviewer_prompt(r, meta, tests_by_cluster.get(ckey, []))
                    if verdict in _NEEDS_HUMAN else None))

        parts.append(
            f'<tr class="case-row in-cluster" id="{rid}" data-cluster="{_esc(ckey)}" '
            f'data-team="{_esc(_text(r.get("team_name")))}" '
            f'data-component="{_esc(_text(r.get("component_name")))}" '
            f'data-verdict="{_esc(verdict)}" '
            f'data-transition="{_esc(_text(r.get("transition")))}" '
            f'data-confidence="{_esc(_text(r.get("confidence")).lower())}">'
            f'<td class="col-idx col-num">{i + 1}</td>'
            f'<td class="col-test">{test_cell}</td>'
            f'<td class="col-team">{_esc(_text(r.get("team_name"))) or "—"}</td>'
            f'<td class="col-comp">{_esc(_text(r.get("component_name"))) or "—"}</td>'
            f'<td class="col-status">{_status_span(r.get("status_a"), r.get("status_b"))}</td>'
            f'<td class="col-verdict"{_verdict_rank_attr(verdict)}>{verdict_cell}</td>'
            f'<td class="col-confidence"{_conf_rank_attr(conf)}>{conf_cell}</td>'
            f'<td class="col-culprit">{culprit_cell}</td>'
            f'<td class="col-reasoning">{reason_cell}</td>'
            f'{ticket_cell}'
            f'<td class="col-jira">{actions_cell}</td>'
            "</tr>"
        )
        # The whole cluster's tests, not just this row's: a cluster is one
        # shared error, so a reviewer needs every test it covers.
        parts.append(_detail_row(r, rid, css, ckey, meta,
                                 tests_by_cluster.get(ckey, [])))
    return "\n".join(parts)



def _jira_href(meta: dict, *, verdict: str, summary_text: str, rows: list,
               n: int = 1) -> str:
    """A prefilled Jira draft link. Opens a draft — nothing is filed.

    Blank `parent` / `reporter` are omitted rather than sent empty: Jira reads
    an empty value as a deliberate clear, which is worse than staying silent.
    """
    jira = meta.get("jira")
    if not isinstance(jira, dict):
        jira = resolve_jira_settings({})
    base = str(jira.get("base_url") or "https://liferay.atlassian.net").rstrip("/")

    first = rows[0] if rows else {}
    def _g(key):
        try:
            return _text(first.get(key))
        except AttributeError:
            return ""

    build_b = _text(meta.get("build_b_name")) or _text(meta.get("build_id_b"))
    summary = f"Investigate {n} test failure{'' if n == 1 else 's'}"
    if build_b:
        summary += f" in {build_b}"
    if summary_text:
        summary += f" — {summary_text}"

    parts = [f"h3. Root cause ({verdict or 'UNCLASSIFIED'}, {n} test"
             f"{'' if n == 1 else 's'})", "", summary_text or "(no reasoning recorded)", ""]

    culprit = _g("culprit_file")
    if culprit:
        parts += ["h3. Culprit file", "", f"{{{{{culprit}}}}}", ""]
        commits = _g("culprit_commits")
        if commits:
            parts += [f"Changed by: {commits}", ""]

    err = _g("error_message")
    if err:
        parts += ["h3. Error", "", "{code}", _truncate(err, 1200), "{code}", ""]

    # Link back to Testray so the draft is traceable to the run.
    testflow_id = _text(meta.get("testflow_id"))
    subtask_id = _g("subtask_id")
    base_ui = _text(meta.get("testray_url"))
    if testflow_id and subtask_id:
        link = f"testflow/{testflow_id}/subtasks/{subtask_id}"
        parts += ["h3. Testray", "",
                  (f"{base_ui}#/{link}" if base_ui else link), ""]
    else:
        url = _case_url(meta, first.get("caseresult_id") if hasattr(first, "get") else None)
        if url:
            parts += ["h3. Testray", "", url, ""]

    parts += ["h3. Claude reasoning", "",
              f"Classifier: {_text(meta.get('classifier'))}",
              f"Run: {_text(meta.get('run_id'))}"]

    description = "\n".join(parts)[:_JIRA_DESC_MAX]

    params = {
        "pid":         str(jira.get("project_id") or "11106"),
        "issuetype":   str(jira.get("issue_type") or "10002"),
        "summary":     summary[:_JIRA_SUMMARY_MAX],
        "description": description,
    }
    if str(jira.get("label") or "").strip():
        params["labels"] = str(jira["label"]).strip()
    # Omit rather than send blank — see the module docstring.
    if str(jira.get("parent") or "").strip():
        params["parent"] = str(jira["parent"]).strip()
    if str(jira.get("reporter_account_id") or "").strip():
        params["reporter"] = str(jira["reporter_account_id"]).strip()

    return (f"{base}/secure/CreateIssueDetails!init.jspa?"
            + urllib.parse.urlencode(params))


def _cluster_index(df: pd.DataFrame) -> tuple[list[str], dict]:
    """Precompute each row's clusterKey once, plus a per-cluster value index.

    Built in a single pass on purpose. The previous form recomputed the key
    inside a per-row predicate, so `normalize()` ran roughly
    clusters x columns x rows times — ~150,000 regex passes on a 449-row run,
    which pinned a core for minutes. It looked fine at 170 rows, which is why
    it shipped. Do not reintroduce a per-row `_cluster_of` call in a loop.
    """
    keys: list[str] = []
    shared: dict[tuple[str, str], set] = {}
    for _, r in df.iterrows():
        ck = _cluster_of(r)
        keys.append(ck)
        for col in ("reason", "culprit_file", "display_verdict", "confidence"):
            shared.setdefault((ck, col), set()).add(_text(r.get(col)))
    return keys, shared


# The two actions that are not wired yet, declared once so a cluster header and
# a member row cannot drift into advertising different things. Ported verbatim
# in intent from the CX's PENDING_ACTIONS (ActionsMenu.tsx).
_PENDING_ACTIONS = [
    ("Change verdict",
     "Correct the AI verdict, link an issue and leave a comment — the same "
     "shape as Edit on a Testray case result. Changing a cluster will apply to "
     "every test in it; changing one row stays on that row"),
    ("Send Test Fix PR",
     "Triggers the /test-fix skill and opens a pull request for the team to "
     "review — nothing is merged automatically"),
]


def _actions_menu(jira_href: str, label: str = "Actions",
                  prompt: str | None = None) -> str:
    """The row/cluster actions kebab — the last column of the triage table.

    Ported from the CX's ActionsMenu.tsx so both renderers offer the same three
    things (§12 view contract). Items are ordered ALPHABETICALLY, not by
    importance: whether you file, correct or fix first depends on the verdict,
    so any importance order would be a guess, and alphabetical is at least
    predictable.

    Only Create Jira Ticket is wired. The other two render disabled rather than
    hidden, deliberately — the point is to show the shape of the menu and
    advertise what is coming, and each carries a tooltip so a disabled item
    reads as unfinished rather than broken.

    Unlike the CX this is one static menu per row with no framework behind it,
    so the open/close behaviour lives in _JS and the markup here is inert.
    """
    items = [
        f'<span class="actions-item is-pending" role="menuitem" '
        f'aria-disabled="true" title="{_esc(_PENDING_ACTIONS[0][1])} '
        f'(not wired yet)">{_esc(_PENDING_ACTIONS[0][0])}'
        f'<span class="actions-soon">Soon</span></span>',
    ]
    # Alphabetically between "Change verdict" and "Create Jira Ticket". This
    # lives in the MENU, not only in the row detail panel: the report opens
    # fully collapsed, so a detail-panel-only affordance is two clicks deep
    # behind a cluster a reader has no reason to expand — which is exactly how
    # it went unnoticed. The kebab is on every visible cluster header.
    if prompt:
        items.append(
            '<button type="button" class="actions-item handoff-copy" '
            'role="menuitem" title="Copies a ready-to-paste prompt describing '
            'this failure, the commit range and the candidate causes, to run '
            'against a local liferay-portal checkout in your own Claude Code '
            'session.">'
            'Copy prompt for local verification</button>'
            f'<pre class="handoff-text" hidden>{_esc(prompt)}</pre>')
    items += [
        f'<a class="actions-item" role="menuitem" href="{_esc(jira_href)}" '
        f'target="_blank" rel="noopener" title="Opens a prefilled Jira draft '
        f'in a new tab for you to confirm — nothing is filed automatically">'
        f'Create Jira Ticket</a>',
        f'<span class="actions-item is-pending" role="menuitem" '
        f'aria-disabled="true" title="{_esc(_PENDING_ACTIONS[1][1])} '
        f'(not wired yet)">{_esc(_PENDING_ACTIONS[1][0])}'
        f'<span class="actions-soon">Soon</span></span>',
    ]
    return (f'<div class="actions-menu">'
            f'<button type="button" class="actions-kebab" aria-haspopup="menu" '
            f'aria-expanded="false" aria-label="{_esc(label)}" '
            f'title="{_esc(label)}">'
            f'<svg aria-hidden="true" height="14" viewBox="0 0 4 16" width="4">'
            f'<circle cx="2" cy="2" r="1.6"/><circle cx="2" cy="8" r="1.6"/>'
            f'<circle cx="2" cy="14" r="1.6"/></svg></button>'
            f'<div class="actions-list" role="menu" hidden>{"".join(items)}</div>'
            f'</div>')


def _ticket_cell(values, cluster: bool = False) -> str:
    """Issues ALREADY linked to the failure — not a place to create one.

    Its own column (ported from the CX's Tickets cell) because the question it
    answers is "has this been filed before?", which is read down the column,
    not per row. Creating a ticket is an action and lives in the kebab.

    An issue key must not break at its hyphen: normal wrapping still treats the
    "-" in LPD-99999 as a break opportunity and a narrow column rendered it as
    "LPD-" / "99999". One nowrap span per key, wrapping between keys instead.
    Testray's separator is not guaranteed, so split on commas and whitespace.
    """
    distinct = list(dict.fromkeys(v for v in (_text(x) for x in values) if v))
    cls = "col-ticket cluster-cell" if cluster else "col-ticket"
    if not distinct:
        # A cluster header leaves shared-but-absent cells blank; a member row
        # says "nothing linked" explicitly.
        inner = "" if cluster else '<span class="ticket-none">&mdash;</span>'
    elif len(distinct) > 1:
        inner = (f'<span class="cluster-culprit-none" '
                 f'title="{_esc(" · ".join(sorted(distinct)))}">'
                 f'{len(distinct)} values</span>')
    else:
        inner = "".join(f'<span class="ticket-key">{_esc(k)}</span>'
                        for k in re.split(r"[,\s]+", distinct[0]) if k)
    return f'<td class="{cls}">{inner}</td>'


def _flaky_badge(members) -> str:
    """FLAKY badge for a cluster header, when any member is marked flaky.

    Clusters render COLLAPSED, so a badge that only exists on member rows is
    invisible in the default view — which is the one a reader actually sees.
    "some" rather than "all" is spelled out, because a mixed cluster where one
    member is flaky and another is not is exactly the case where the reader
    must not assume the whole cluster can be discounted.
    """
    flags = [str(v).strip().lower() in ("true", "1", "1.0", "yes")
             for v in members.get("known_flaky", [])]
    if not flags or not any(flags):
        return ""
    label = "FLAKY" if all(flags) else "SOME FLAKY"
    return (f' <span class="flaky-badge" title="Marked flaky on the Testray '
            f'case — excluded from classification, shown for awareness.">'
            f'{label}</span>')


# What each verdict MEANS, for a reader who did not write the rubric. Wording
# tracks the classify prompt and submit._auto_label — if the rubric moves, these
# move with it, because a legend that disagrees with the classifier is worse
# than no legend.
_VERDICT_LEGEND = {
    "BUG":
        "A change in this range caused the failure, and that change is a real "
        "defect. Suspicious cause always names the file, with the ticket and "
        "commits that touched it.",
    "POSSIBLE_BUG":
        "A single plausible cause in the diff that could not be confirmed. "
        "Suspicious cause names that one candidate file.",
    "NEEDS_REVIEW":
        "Could not be narrowed to one cause \u2014 several changes could explain "
        "it, or nothing concrete could be tied to it. Suspicious cause lists "
        "the candidate tickets instead of a file. A human decides.",
    "TEST_FIX":
        "The diff did cause this, but the production change was intentional "
        "and correct \u2014 the test asserts the old behaviour. Fix the test, not "
        "production. Suspicious cause names the ticket behind the change, or "
        "the stale test, never the production file.",
    "NOT_ATTRIBUTABLE":
        "A NEEDS_REVIEW the classifier had low confidence in \u2014 it could not "
        "attribute the failure at all. Not a claim that a person must review "
        "every one.",
    "FALSE_POSITIVE":
        "A real failure, but nothing in this range caused it \u2014 a flake, or a "
        "failure that predates the change.",
    "ENV_FAILURE":
        "Infrastructure or environment, not the product.",
    "DID_NOT_RUN":
        "The test produced no result to compare \u2014 the build or batch failed, "
        "or no error text was recorded. Nothing was analysed, so someone "
        "should check why it did not run: a real failure can hide behind one "
        "of these.",
    "AUTO_CLASSIFIED":
        "Labelled upstream by pattern, never sent to the model.",
    "PENDING":
        "Not yet classified.",
}


# The verdicts the guide documents, in severity order. STATIC — the same six on
# every report, whether or not a given run used them. A key that changed shape
# per run would not be a reference: a reader who learned it on one report would
# find a different guide on the next, and could not look up a verdict they had
# seen last week. Sourced from verdicts.VERDICT_ORDER, so the guide is ordered
# the way the table sorts.
_LEGEND_VERDICTS = ("BUG", "POSSIBLE_BUG", "NEEDS_REVIEW", "TEST_FIX",
                    "NOT_ATTRIBUTABLE", "FALSE_POSITIVE", "ENV_FAILURE",
                    "DID_NOT_RUN")


def _verdict_legend() -> str:
    """The standing guide to the verdict vocabulary.

    Takes no run data on purpose — see _LEGEND_VERDICTS. This report is read by
    people who did not write the rubric, so the vocabulary has to be legible
    from the page itself rather than from the prompt.
    """
    shown = [v for v in verdicts.VERDICT_ORDER if v in _LEGEND_VERDICTS]
    items = "".join(
        f'<div class="legend-item">{_verdict_span(v)}'
        f'<span class="legend-text">{_esc(_VERDICT_LEGEND[v])}</span></div>'
        for v in shown)
    return f"""
  <details class="verdict-legend">
    <summary><strong>What the verdicts mean</strong>
      <span class="hint">A guide to the verdict vocabulary, most severe first.
      The same on every report &mdash; a run will not use all of
      them.</span></summary>
    <div class="legend-grid">{items}</div>
  </details>"""


def _pre_existing_section(pre_df, meta: dict) -> str:
    """Collapsed table of failures that were already failing on the baseline.

    Deliberately its own section rather than rows in the main table: these
    carry no verdict (nothing was asked of the classifier) and no culprit, so
    they would be five empty columns under a "root cause" heading. Collapsed by
    default because on a sticky routine this is the longest list on the page
    and it is context, not work.
    """
    if pre_df is None or not len(pre_df):
        return ""
    rows = []
    for i, r in enumerate(pre_df.to_dict("records")):
        test = _text(r.get("test_case")) or "(unnamed)"
        url  = _case_url(meta, r.get("caseresult_id"))
        cell = (f'<a class="test-link" href="{_esc(url)}" target="_blank" '
                f'rel="noopener">{_esc(test)}</a>' if url else _esc(test))
        if _is_flaky(r):
            cell += (' <span class="flaky-badge" title="Marked flaky on the '
                     'Testray case.">FLAKY</span>')
        rows.append(
            f'<tr class="pre-row" '
            f'data-team="{_esc(_text(r.get("team_name")))}" '
            f'data-component="{_esc(_text(r.get("component_name")))}" '
            f'data-transition="{_esc(_text(r.get("transition")))}">'
            f'<td class="col-idx col-num">{i + 1}</td>'
            f'<td class="col-test">{cell}</td>'
            f'<td class="col-team">{_esc(_text(r.get("team_name"))) or "—"}</td>'
            f'<td class="col-comp">{_esc(_text(r.get("component_name"))) or "—"}</td>'
            f'<td class="col-reasoning"><pre class="error">'
            f'{_esc(_truncate(_text(r.get("error_message"))))}</pre></td></tr>')
    return f"""
  <details class="pre-existing">
    <summary><strong>Pre-existing failures ({len(pre_df)})</strong>
      <span class="pre-count" id="pre-count"></span>
      <span class="hint">Already failing with the same error on the baseline —
      not caused by this range, and not analyzed.</span></summary>
    <table class="per-test-table">
<thead><tr><th class="col-idx">#</th><th class="col-test">Test</th>
<th class="col-team">Team</th><th class="col-comp">Component</th>
<th class="col-reasoning">Error</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
    </table>
  </details>"""


def _is_flaky(row) -> bool:
    """Whether a row is marked flaky upstream on the Testray case."""
    v = row.get("known_flaky")
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return False
    return str(v).strip().lower() in ("true", "1", "1.0", "yes")


# Ticket keys the classifier names in `specific_change` when it will not name a
# file. Mirrors prepare._LPD_RE — kept local rather than reaching into a private
# name, so the two have to be changed together.
# The candidate-ticket pattern lives in verdicts.py, because the display
# rule depends on it: three copies had already drifted apart in wording.
_TICKET_RE = verdicts.CANDIDATE_RE


def _jira_base(meta: dict) -> str:
    """Jira base URL for ticket links, resolved the same way _jira_href does."""
    jira = meta.get("jira")
    if not isinstance(jira, dict):
        jira = resolve_jira_settings({})
    return str(jira.get("base_url") or "https://liferay.atlassian.net").rstrip("/")


def _cause_tickets(meta: dict, specific_change) -> str:
    """Linked chips for every ticket named in `specific_change`, de-duplicated.

    The rubric tells the classifier to leave `culprit_file` NULL and list all
    candidate tickets in `specific_change` whenever two or more changes could
    explain a failure (prompt: "NEEDS_REVIEW — two or more candidate causes").
    Without this the actionable half of that answer never reaches the column:
    `culprit_commits` is derived from the FILE in submit.annotate_culprit_
    commits, so a null culprit yields no commits either, and the cell renders
    "—" for a row the classifier did have an opinion about.
    """
    text = _text(specific_change)
    if not text:
        return ""
    base, seen, chips = _jira_base(meta), set(), []
    for key in _TICKET_RE.findall(text):
        if key in seen:
            continue
        seen.add(key)
        chips.append(f'<a class="cause-ticket" href="{_esc(base)}/browse/{_esc(key)}" '
                     f'target="_blank" rel="noopener">{_esc(key)}</a>')
    return "".join(chips)


def _cause_cell(meta: dict, culprit: str, commits: str, specific_change) -> str:
    """The Suspicious cause cell.

    One column, two kinds of answer: the culprit file when the classifier
    committed to one (with the commits that touched it), else the candidate
    tickets it named instead. `culprit_file` itself is untouched — it is a
    stored verdict field that feeds defect-attribution training data and
    error_signature.cluster_key, so this is a rendering fallback, not a
    repurposing of the field.
    """
    if culprit:
        cell = f"<code>{_esc(culprit)}</code>"
        if commits:
            cell += f'<div class="culprit-commits">{_esc(commits)}</div>'
        return cell
    chips = _cause_tickets(meta, specific_change)
    if chips:
        return f'<div class="cause-tickets">{chips}</div>'
    return "—"


def _shared_in_cluster(shared: dict, ckey: str, column: str, value: str) -> bool:
    """True when every member of the cluster carries this same value."""
    vals = shared.get((ckey, column))
    return bool(vals) and len(vals) == 1 and value in vals


# Two vocabularies reach this guard. `prepare` emits lowercase tokens
# (TRANSITION_CHANGED == "changed"); the §12 fixtures use CHANGED_FAILURE.
# Accept both. Matching only the uppercase form is what shipped, and because
# prepare never produces it the baseline warning below never rendered on real
# data — the one omission this report's own comment calls the most misleading
# thing it could do.
_CHANGED_TRANSITIONS = {TRANSITION_CHANGED, "changed_failure"}


def _is_changed_failure(transition) -> bool:
    return _text(transition).strip().lower() in _CHANGED_TRANSITIONS


# Verdicts where a human still has to finish the job. A BUG or TEST_FIX has
# already named its cause, so a "go investigate" prompt would be noise there.
_NEEDS_HUMAN = {"NEEDS_REVIEW", "NOT_ATTRIBUTABLE", "POSSIBLE_BUG"}

# Tests listed in a handoff prompt before it collapses to a count.
_PROMPT_MAX_TESTS = 12


def _reviewer_prompt(r, meta: dict, sibling_tests: list[str],
                     verdict: str = "") -> str:
    """A copy-pasteable prompt for whoever picks this row up in their own
    Claude Code session.

    Written as an instruction to an agent, not a summary for a human: it names
    the repo and range so the agent can read the diff itself, lists the
    candidate tickets triage could not choose between, and — the part that
    makes it useful rather than a restatement — says what to DO with them.
    "Verify this" on its own gives an agent nothing to act on.

    The range is given as hashes rather than build ids: a Testray build id
    means nothing in a checkout, and the whole point is that the reviewer
    works in the repo.

    `verdict` is passed in rather than read off `r`. On a cluster header the
    label shown is the ROLLUP of the members, and reading the first member's
    own verdict instead produced prompts that opened "classified as
    FALSE_POSITIVE — it could not settle the cause", which is both wrong and
    self-contradicting.
    """
    verdict = verdict or _text(r.get("display_verdict")) or _text(r.get("classification"))
    ha, hb = _text(meta.get("git_hash_a")), _text(meta.get("git_hash_b"))
    branch = _text(meta.get("base_branch"))
    tickets = list(dict.fromkeys(
        _TICKET_RE.findall(_text(r.get("specific_change")))))
    tests = [t for t in (sibling_tests or [_text(r.get("test_case"))]) if t]
    conf = _text(r.get("confidence"))
    rng = f"{ha[:12]}..{hb[:12]}" if ha and hb else ""

    L = [f"A Liferay test-analysis run classified this failure as {verdict}"
         + (f" ({conf} confidence)" if conf else "")
         + " — it could not settle the cause. Please finish the triage.", ""]
    L.append("Repo:   liferay-portal" + (f", branch {branch}" if branch else ""))
    if rng:
        L.append(f"Range:  {rng}   (the target build ran at {hb[:12]})")
        L.append(f"        {_GITHUB_COMPARE.format(a=ha, b=hb)}")

    # A cluster can hold sixty tests. Listing them all buries the instructions
    # under a wall of names and bloats every copy of the prompt; the shared
    # error is what identifies the cluster, not the roster.
    L += ["", f"Failing test{'s' if len(tests) > 1 else ''}"
              + (f" ({len(tests)} in this cluster, first {_PROMPT_MAX_TESTS} shown)"
                 if len(tests) > _PROMPT_MAX_TESTS else "") + ":"]
    L += [f"  - {t}" for t in tests[:_PROMPT_MAX_TESTS]]
    if len(tests) > _PROMPT_MAX_TESTS:
        L.append(f"  … and {len(tests) - _PROMPT_MAX_TESTS} more")

    err = _text(r.get("error_message"))
    if err:
        L += ["", "Shared error:", f"  {_truncate(err, 400)}"]

    L += [""]
    if tickets:
        L += ["Candidate causes triage found but could not choose between:",
              "  " + ", ".join(tickets), "",
              "Please:",
              "1. Read each candidate's commits in the range",
              f"   (git log {rng} --grep={tickets[0]}) and judge whether that",
              "   change could produce this error."]
    else:
        L += ["Triage found no concrete candidate, so start from the range.", "",
              "Please:",
              "1. Find changes in the range touching the failing test's module",
              f"   (git log {rng} -- <module path>) and judge whether any could",
              "   produce this error."]
    L += ["2. If one is the cause, say which it is:",
          "     BUG      — the production change is a defect",
          "     TEST_FIX — the production change was intentional and the test",
          "                asserts the old behaviour, so the test needs updating",
          "3. Run the failing test locally to confirm before concluding.",
          "4. Report the verdict, the culprit file, and the evidence you used."]
    return "\n".join(L)


def _detail_row(r, rid: str, css: str, ckey: str, meta: dict,
                sibling_tests: list[str] | None = None) -> str:
    """The per-row detail panel: everything the table had to truncate."""
    items = []

    def add(label, value_html):
        items.append(f"<dt>{_esc(label)}</dt><dd>{value_html}</dd>")

    add("Status", _status_span(r.get("status_a"), r.get("status_b")) or "—")

    transition = _text(r.get("transition"))
    if transition:
        add("Transition", f"<code>{_esc(transition)}</code>")

    baseline_err = _text(r.get("baseline_error_message"))
    error = _truncate(r.get("error_message"))
    if baseline_err and _is_changed_failure(transition):
        # A changed failure was already failing on the baseline. Presenting it
        # as a fresh regression is the single most misleading thing this report
        # could do, so both errors are shown and labelled.
        add("Note", "<strong>This test was already failing on the baseline "
                    "&mdash; a changed failure, not a new one.</strong>")
        add("Was failing with", f'<pre class="error">{_esc(_truncate(baseline_err))}</pre>')
        add("Now failing with", f'<pre class="error">{_esc(error)}</pre>')
    elif error:
        add("Error", f'<pre class="error">{_esc(error)}</pre>')

    culprit = _text(r.get("culprit_file"))
    commits = _text(r.get("culprit_commits"))
    add("Suspicious cause",
        _cause_cell(meta, culprit, commits, r.get("specific_change")))

    if culprit and commits:
        add("Changed by", _esc(commits))

    change = _text(r.get("specific_change"))
    if change:
        add("Specific change", _esc(change))

    add("Reasoning", _esc(_text(r.get("reason"))) or "—")

    issues = _text(r.get("linked_issues"))
    add("Ticket already linked", _esc(issues) if issues else "—")

    add("Cluster key", f'<code>{_esc(ckey)}</code>')

    verdict = _text(r.get("display_verdict")) or _text(r.get("classification"))
    if verdict in _NEEDS_HUMAN:
        prompt = _reviewer_prompt(r, meta, sibling_tests or [])
        add("Verify locally",
            '<div class="handoff">'
            '<button type="button" class="handoff-copy">Copy prompt</button>'
            '<span class="handoff-hint">Paste into a Claude Code session in a '
            'liferay-portal checkout.</span>'
            f'<pre class="handoff-text">{_esc(prompt)}</pre></div>')

    for label, col in (("Case id", "testray_case_id"),
                       ("Case result id", "caseresult_id"),
                       ("Subtask id", "subtask_id"),
                       ("Match strategy", "match_strategy")):
        v = _text(r.get(col))
        if v:
            add(label, f"<code>{_esc(v)}</code>")

    return (f'<tr class="case-detail {css}" id="detail-{rid}" hidden>'
            f'<td colspan="11"><div class="case-detail-inner"><dl>'
            + "".join(items)
            + "</dl></div></td></tr>")


# Testray's own comparison order, so the matrix reads the same way it does in
# the product.
# The commit range behind a run, as a GitHub compare link. Points at the
# canonical public repo rather than whichever remote `prepare` happened to
# fetch the commits from (bchan, release-ee, a PR fork): this link is for a
# person to click, so it has to be the repo they can actually open.
#
# Testray's Build object has a `githubCompareURLs` field for this, but it reads
# "null" on every build measured on prod, so the URL is built here instead.
_GITHUB_COMPARE = "https://github.com/liferay/liferay-portal/compare/{a}...{b}"


_STATUS_ORDER = ["PASSED", "FAILED", "BLOCKED", "TESTFIX", "UNTESTED", "DIDNOTRUN"]
_STATUS_LABEL = {"TESTFIX": "Test Fix", "UNTESTED": "DNR", "DIDNOTRUN": "DNR"}

# What each PASSED/FAILED cell MEANS, printed under the number. Reading a
# cross-tab means holding "row = baseline, column = target" in your head and
# re-deriving the meaning four times; the label does that once. Keyed
# (A status, B status).
#
# Only the four pass/fail combinations are named. BLOCKED / Test Fix / DNR
# cells are left bare on purpose — a phrase for every combination would be
# nine notes of clutter to explain four that matter, and the ones that matter
# are the ones a reader acts on.
_CELL_NOTE = {
    ("PASSED", "PASSED"): "passed in both",
    ("PASSED", "FAILED"): "new failures",
    ("FAILED", "PASSED"): "now passing",
    ("FAILED", "FAILED"): "failed in both",
}


def _status_label(code: str) -> str:
    return _STATUS_LABEL.get(code, code.title())


def _status_matrix(meta: dict) -> str:
    """A x B status cross-tab for the whole comparison.

    Deliberately covers every joined case, not just the triaged ones: the
    headline counts describe the slice we acted on, and this describes the
    build. Reading "1038 now passing" next to "793 failed in both" is what
    makes a triage set of 557 legible.
    """
    raw = meta.get("status_matrix") or {}
    if not raw:
        return ""
    rows = [r for r in _STATUS_ORDER if r in raw]
    rows += sorted(k for k in raw if k not in _STATUS_ORDER)
    cols_seen = {c for v in raw.values() for c in v}
    cols = [c for c in _STATUS_ORDER if c in cols_seen]
    cols += sorted(c for c in cols_seen if c not in _STATUS_ORDER)
    if not rows or not cols:
        return ""

    head = "".join(f'<th>B<br><span>{_esc(_status_label(c))}</span></th>' for c in cols)
    body = []
    for r in rows:
        cells = []
        for c in cols:
            n = raw.get(r, {}).get(c, 0)
            # An unchanged diagonal is context, not news; a transition is news.
            cls = _cell_class(r, c)
            if not n:
                cells.append('<td class="zero"></td>')
                continue
            note = _CELL_NOTE.get((r, c))
            note_html = (f'<span class="cell-note">{_esc(note)}</span>'
                         if note else "")
            cells.append(f'<td class="{cls}"><span class="cell-n">{n:,}</span>'
                         f'{note_html}</td>')
        body.append(f'<tr><th>A<br><span>{_esc(_status_label(r))}</span></th>'
                    + "".join(cells) + "</tr>")
    return ('<div class="matrix">'
            '<div class="matrix-title">Where A is the previous build, '
            'and B is the new build</div>'
            f'<table><thead><tr><th></th>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _worse(a: str, b: str) -> bool:
    """Did the status get worse from A to B? PASSED is the only good state."""
    return a == "PASSED" and b != "PASSED"


def _cell_class(a: str, b: str) -> str:
    """Colour for one status-matrix cell.

    Green means ONLY "ended up passing", red means ONLY "was passing and no
    longer is". Everything else is uncoloured, because it is neither.

    This used to read `worse(a, b) ? red : green`, which made green the default
    for every off-diagonal cell — so DNR -> FAILED and FAILED -> DNR both
    rendered green, reading as good news when nothing good happened. Green is a
    claim about the target column, not the absence of a regression.
    """
    if a == b:
        return "same"           # the diagonal: context, not news
    if b == "PASSED":
        return "better"         # whatever it came from, it passes now
    if b == "FAILED" or _worse(a, b):
        # Red is the mirror of green: "ended up failing", wherever it came
        # from, PLUS "was passing and no longer is" (which also covers ending
        # up BLOCKED or not-run).
        #
        # UNTESTED -> FAILED is red for a reason worth keeping: that is
        # TRANSITION_NO_BASELINE, which prepare puts in TRIAGE_TRANSITIONS
        # deliberately because the usual cause is a NEW test that fails — the
        # thing triage exists to catch. Leaving it uncoloured made the matrix
        # quieter than the triage list it sits above.
        return "worse"
    # Neither: a move between two non-passing states that does not end in a
    # failure — FAILED -> BLOCKED, FAILED -> not-run. A lost signal rather
    # than a failure, and prepare already warns about coverage drops.
    return "neutral"



def _totals(df: pd.DataFrame, n_clusters: int, meta: dict,
            cluster_groups: list | None = None) -> str:
    """Headline counts, led by CLUSTERS with case rows as fan-out.

    The tool has already clustered, so the case count is the wrong headline
    unit: "6 possible bugs" was three defects each counted twice, and "153 need
    review" was 100 clusters, one of which held 23 cases. Leading with rows
    makes a tractable morning read like a crisis. Clusters are the unit a human
    actually works through; the fan-out says how much of the suite each one
    covers.
    """
    rows = (df["display_verdict"].value_counts().to_dict()
            if len(df) and "display_verdict" in df else {})

    # A cluster's verdict is its worst member's — the same rollup the cluster
    # header shows, so the pill and the table cannot disagree.
    clusters: dict[str, int] = {}
    for _label, positions in (cluster_groups or []):
        v = _rollup(df.iloc[positions].get("display_verdict", []))
        if v:
            clusters[v] = clusters.get(v, 0) + 1

    pills = []
    for verdict in _VERDICT_ORDER:
        if verdict not in rows and verdict not in clusters:
            continue
        n_cl = clusters.get(verdict, 0)
        n_rows = rows.get(verdict, 0)
        # Only show the fan-out when it actually differs — "3 (3 tests)" is
        # noise, "3 (6 tests)" is the point.
        fan = (f'<span class="fanout">{n_rows} tests</span>'
               if n_rows and n_rows != n_cl else "")
        pills.append(
            f'<a class="pill" href="#" data-pill-for="{_esc(verdict)}" '
            f'title="{n_cl} cluster(s), {n_rows} case row(s) — click to filter">'
            f'{_verdict_span(verdict)}'
            f'<span class="n" data-pill-verdict="{_esc(verdict)}">{n_cl or n_rows}</span>'
            f'{fan}</a>'
        )
    # "Total tests: 557" invited the reading that the build ran 557 tests. It
    # ran ~17.5k; 557 is the TRIAGE set. Say which, and show the denominator.
    pills.append(f'<span class="pill" title="New, changed and blocked failures '
                 f'— the rows this run triaged. Not the number of tests run.">'
                 f'<strong>Failures triaged:</strong> '
                 f'<span class="n" data-pill-total="1">{len(df)}</span></span>')
    target_rows = meta.get("target_rows")
    if target_rows:
        pills.append(f'<span class="pill" title="Case results in the target '
                     f'build."><strong>Tests in build:</strong> '
                     f'<span class="n">{int(target_rows):,}</span></span>')
    # How much of that build the comparison could actually see. Always shown:
    # "Tests in build: 3,579" beside a 54-row matrix read as a full comparison,
    # which is the same class of error as the old bare "Total tests: 557".
    compared, denom = _join_coverage(meta)
    if compared:
        # One decimal under 10%, none above: the banner quotes one decimal, and
        # rounding this to a whole percent made the two disagree (1.5% vs 2%)
        # about the same number.
        pct = (100.0 * compared / denom) if denom else 0.0
        low = bool(denom) and compared < denom * _LOW_COVERAGE
        share = (f" ({pct:.1f}%)" if 0 < pct < 10
                 else (f" ({pct:.0f}%)" if denom else ""))
        pills.append(
            f'<span class="pill{" warn" if low else ""}" title="Case results '
            f'that ran on BOTH builds, as a share of whichever build ran more. '
            f'The diff is an inner join on case id, so anything outside this '
            f'is invisible to it.">'
            f'<strong>Compared:</strong> <span class="n">{compared:,}</span>'
            f'<span class="fanout">{share}</span></span>')
    pills.append('<span class="pill" title="Distinct clusterKey values — one cluster '
                 'per normalized error signature (ARCHITECTURE §7)."><strong>'
                 f'Root-cause clusters:</strong> <span class="n" '
                 f'data-pill-clusters="1">{n_clusters}</span></span>')
    return '<div class="totals">' + "".join(pills) + "</div>"


def _select(el_id: str, label: str, values, size: str = "md",
            title: str = "") -> str:
    """One filter control: its label and its select, as a SINGLE flex item.

    The pair has to be wrapped. `.filters-row` wraps, and when the label and
    the select are two separate flex items the break can land between them —
    which is how "Transition:" ended up stranded on one line with its dropdown
    on the next.

    `size` tunes the width to the vocabulary the control actually holds, so
    five filters fit on one row: Confidence is four short words, Verdict is
    long but truncatable, Team and Component are free text.
    """
    opts = "".join(f'<option value="{_esc(v)}">{_esc(v)}</option>'
                   for v in sorted({_text(x) for x in values if _text(x)}))
    # The title sits on the wrapper so it fires over the label AND the select —
    # a reader hovers whichever of the two they happen to be pointing at.
    attr = f' title="{_esc(title)}"' if title else ""
    return (f'<span class="filter-field ff-{_esc(size)}"{attr}>'
            f'<label for="{el_id}">{_esc(label)}:</label>'
            f'<select id="{el_id}"><option value="">All</option>{opts}</select>'
            f'</span>')


# Below this share of the target build, the comparison covers so little that
# its verdicts describe a different test suite than the one that ran. 50% is a
# judgement call, but the failure it guards against is not subtle: the case that
# prompted it covered 1.6%.
_LOW_COVERAGE = 0.5


def _join_coverage(meta: dict) -> tuple[int, int]:
    """(cases compared, the number that could have been compared).

    The diff is an INNER join on case id, so a case that ran on only one side is
    invisible to it. The matrix is built from that join, so summing it gives the
    number actually compared — no extra field needed.

    The denominator is whichever build ran MORE cases, not the target. Measured
    against the target alone this read "7,699 (99%)" for a pair that shared 42%
    of its cases — true, and useless, because the target was the side that
    shrank. Both the Compared pill and the low-coverage banner read it from
    here: when they each computed their own, the pill warned at 42% while the
    banner stayed silent on the same run. Falls back to target_rows for a
    run.yml written before the `coverage` key existed.
    """
    matrix = meta.get("status_matrix") or {}
    compared = sum(int(n) for row in matrix.values() for n in row.values())
    cov = meta.get("coverage") or {}
    try:
        target = int(meta.get("target_rows") or 0)
        denom = max(int(cov.get("baseline_cases") or 0),
                    int(cov.get("target_cases") or 0)) or target
    except (TypeError, ValueError):
        denom = 0
    return compared, denom


def _banners(df: pd.DataFrame, meta: dict) -> str:
    out = []

    verdicts = {_text(v) for v in df.get("display_verdict", [])} if len(df) else set()
    if len(df) and verdicts <= {"", "PENDING"}:
        # Absence of BUG is not evidence of no bug. Say so loudly rather than
        # letting an unclassified run read as a clean one.
        out.append(
            '<section class="rationale"><h2>Classification did not run</h2>'
            "<p>Every row in this run is <code>PENDING</code> &mdash; these failures are "
            "<strong>not yet assessed</strong>. An empty BUG count here means the "
            "classifier never ran, not that no bugs exist.</p></section>"
        )

    # prepare emits lowercase transition keys ("same_failure"), the §12
    # constants are uppercase. Normalise, or this banner silently never fires
    # on real data — which is exactly what it did until 2026-08-18.
    # The "Excluded from triage" banner used to list same_failure / fixed /
    # no_baseline here. The A x B status matrix in the header now carries the
    # same facts in a denser and more complete form (1,038 fixed IS
    # FAILED->PASSED), so the banner was pure duplication and has been dropped.

    compared, denom = _join_coverage(meta)
    if compared and denom and compared < denom * _LOW_COVERAGE:
        pct = 100.0 * compared / denom
        out.append(
            '<section class="rationale warn"><h2>This comparison covers only '
            f'{pct:.1f}% of the build</h2>'
            f"<p>The diff is an inner join on case id: only <strong>"
            f"{compared:,}</strong> of the <strong>{denom:,}</strong> case "
            f"results the larger build ran were run on <em>both</em> "
            f"builds, so everything else is invisible to it. A verdict list "
            f"this short does not mean the build is healthy &mdash; it means "
            f"the two builds ran different test sets.</p>"
            "<p>Most often the suite was re-selected between them. Pick a pair "
            "from the same suite generation &mdash; consecutive builds of one "
            "routine usually share their whole case set, and a build and its "
            "retest always do.</p></section>"
        )

    notes = _text(meta.get("notes"))
    if notes:
        out.append('<section class="rationale"><h2>Run notes</h2><p>'
                   + _esc(notes) + "</p></section>")
    return "\n".join(out)


def render_run(run_dir, df: pd.DataFrame, meta: dict) -> Path:
    """Write report.html into run_dir and return its path."""
    run_dir = Path(run_dir)

    if df is None:
        df = pd.DataFrame()
    df = df.reset_index(drop=True)

    # Pre-existing failures come out of the main table and into their own
    # section. They were never classified (prepare tags them PRE_EXISTING and
    # keeps them out of the clustering), so mixing them into "Failures by root
    # cause" would put rows under a heading that does not describe them. Split
    # BEFORE the cluster index so grouping, ordering and the counts above are
    # computed on the triaged set alone.
    # Keyed on the TRANSITION, matching prepare's `is_report_only`: a
    # same_failure row whose error matched an env pattern carries NO_ERROR
    # rather than PRE_EXISTING, and keying on the tag would strand it in the
    # main table with an auto verdict and no cause.
    pre_df = df.iloc[0:0]
    pre_transitions: list[str] = []
    if len(df) and "transition" in df.columns:
        mask = df["transition"].astype(str).str.strip() == "same_failure"
        if mask.any():
            pre_df = df[mask].reset_index(drop=True)
            pre_transitions = [_text(x) for x in pre_df["transition"]]
            df = df[~mask].reset_index(drop=True)
    # Transition options are taken from the FULL frame: the pre-existing rows
    # are on the page, so a control that omitted `same_failure` described the
    # main table rather than the report. They carry the same data-* keys the
    # filter reads (see _pre_existing_section), so selecting it acts on them.
    all_transitions = list(pre_transitions) + [
        _text(x) for x in df.get("transition", [])]

    # One derived column so the pills, the filter, the cluster rollups, the
    # sort rank and the row cells cannot disagree about what a row is called.
    if len(df):
        df = df.copy()
        df["display_verdict"] = verdicts.display_series(df)
    ckeys, shared = _cluster_index(df)
    groups = _groups(df, ckeys)
    cluster_groups = groups.get("cluster", [])
    cluster_no = {label: i + 1 for i, (label, _) in enumerate(cluster_groups)}

    headers, order = [], {}
    for mode, _label in _GROUP_MODES:
        entries = []
        for idx, (label, positions) in enumerate(groups.get(mode, [])):
            members = df.iloc[positions]
            headers.append(_header_row(
                mode, idx, label, members,
                cluster_no.get(label) if mode == "cluster" else None, meta))
            entries.append({"h": f"grp-{mode}-{idx}",
                            "rows": [f"row-{p}" for p in positions]})
        order[mode] = entries

    body = (_member_rows(df, meta, cluster_no, ckeys, shared)
            if len(df) else '<tr><td colspan="11">No rows.</td></tr>')

    a_url, b_url = _build_url(meta, meta.get("build_id_a")), _build_url(meta, meta.get("build_id_b"))
    a_name = _text(meta.get("build_a_name")) or _text(meta.get("build_id_a")) or "baseline"
    b_name = _text(meta.get("build_b_name")) or _text(meta.get("build_id_b")) or "target"
    a_html = (f'<a class="build-link" href="{_esc(a_url)}" target="_blank" rel="noopener">{_esc(a_name)}</a>'
              if a_url else _esc(a_name))
    b_html = (f'<a class="build-link" href="{_esc(b_url)}" target="_blank" rel="noopener">{_esc(b_name)}</a>'
              if b_url else _esc(b_name))

    summary_bits = [f"Run: <code>{_esc(meta.get('run_id'))}</code>"]
    for label, key in (("Classifier", "classifier"), ("Routine", "routine_id"),
                       ("Mode", "mode"), ("Prepared", "prepared_at")):
        v = _text(meta.get(key))
        if v:
            summary_bits.append(f"{label}: <code>{_esc(v)}</code>")
    ha, hb = _text(meta.get("git_hash_a")), _text(meta.get("git_hash_b"))
    if ha and hb:
        # The FULL hashes go in the URL even though the abbreviations are what
        # is shown: 9 characters resolve today and collide eventually, and this
        # link outlives the run that produced it.
        compare = _GITHUB_COMPARE.format(a=ha, b=hb)
        summary_bits.append(
            f'Hashes: <a class="hash-link" href="{_esc(compare)}" '
            f'target="_blank" rel="noopener" '
            f'title="See every commit between these two builds on GitHub">'
            f'<code>{_esc(ha[:9])}</code> &rarr; <code>{_esc(hb[:9])}</code>'
            f'</a>')
    sig = _text(meta.get("signature_version")) or error_signature.SIGNATURE_VERSION
    summary_bits.append(f"Signatures: <code>{_esc(sig)}</code>")

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Analysis for: {_esc(a_name)} → {_esc(b_name)}</title>
<style>{_CSS}</style>
</head>
<body>
<main>
  <h1>Analysis for: {a_html} <span class="role">baseline</span> &rarr; {b_html} <span class="role">target</span></h1>
  <p class="summary">{" &middot; ".join(summary_bits)}</p>
  {_banners(df, meta)}

  <div class="headline">
   <div class="controls">
    {_totals(df, len(cluster_groups), meta, cluster_groups)}
  <div class="viewbar">
    <span class="viewbar-group">
      <label class="viewbar-label" for="group-by" title="Re-cuts the same rows into different groups. Nothing is re-classified.">Group by:</label>
      <select id="group-by">
        {"".join(f'<option value="{m}">{l}</option>' for m, l in _GROUP_MODES)}
      </select>
    </span>
  </div>

  <div class="filters">
    <div class="filters-row">
      {_select("team-filter", "Team", df.get("team_name", []), "lg")}
      {_select("component-filter", "Component", df.get("component_name", []), "lg")}
      {_select("verdict-filter", "Verdict", df.get("display_verdict", []), "md",
               "Show only rows the classifier gave this verdict.")}
      {_select("confidence-filter", "Confidence",
               [_text(c).lower() for c in df.get("confidence", [])], "sm",
               "Show only rows at this confidence. \'auto\' means pre-classified, never sent to the model.")}
      {_select("transition-filter", "Transition", all_transitions, "md")}
    </div>
    <div class="filters-row">
      <label for="search-filter">Search:</label>
      <input id="search-filter" type="search" autocomplete="off"
             placeholder="filter by test, component, suspicious cause, reasoning… (Esc to clear)">
      <button type="button" class="cluster-btn" id="expand-clusters">Expand all</button>
      <button type="button" class="cluster-btn" id="collapse-clusters">Collapse all</button>
      <span class="visible-count" id="filter-count"></span>
    </div>
  </div>
   </div>
   <div class="side">
    {_status_matrix(meta)}
   </div>
  </div>

  <h2>Failures by root cause</h2>
  <table class="per-test-table clustered">
<thead><tr>
<th class="col-idx">#</th>
<th class="col-test" title="Click a test name to open its Testray case result.">Test</th>
<th class="col-team">Team</th>
<th class="col-comp">Component</th>
<th class="col-status">Status</th>
<th class="col-verdict" title="What the classifier concluded about the failure. A cluster header shows its most severe member's verdict.">Verdict</th>
<th class="col-confidence" title="How sure the classifier was: high, medium or low. &quot;auto&quot; means the row was pre-classified and never sent to the model.">Confidence</th>
<th class="col-culprit" title="The file the classifier blamed. When it would not narrow to one, the candidate tickets it named instead.">Suspicious cause</th>
<th class="col-reasoning" title="Why the classifier reached this verdict, in its own words.">Reasoning</th>
<th class="col-ticket" title="Whether a ticket already exists for this failure. Once this report runs against Testray prod, this column shows the tickets already linked to the case result, if any — it is not a place to file a new one (see Actions).">Existing Ticket</th>
<th class="col-jira" title="What you can do about this row — file it, correct it, or ask for a test fix">Actions</th>
</tr></thead>
<tbody>
{chr(10).join(headers)}
{body}
</tbody>
  </table>
{_pre_existing_section(pre_df, meta)}
{_verdict_legend()}
</main>
<script type="application/json" id="group-order">{json.dumps(order)}</script>
<script>{_JS}</script>
</body>
</html>"""

    out = run_dir / "report.html"
    out.write_text(doc, encoding="utf-8")
    return out
