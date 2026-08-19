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
import urllib.parse
from pathlib import Path

import pandas as pd

from . import error_signature
from .jira_settings import DEFAULT_LABEL, resolve_jira_settings
from .prepare import TRANSITION_CHANGED

# Severity order — index doubles as the sort rank and drives _rollup().
# Severity order, as specified: BUG, POSSIBLE_BUG, NEEDS_REVIEW, TEST_FIX,
# FALSE_POSITIVE, DID_NOT_RUN. Index doubles as the sort rank and drives both
# _rollup() and the default ordering, so this list IS the report's opinion
# about what deserves attention first.
_VERDICT_ORDER = ["BUG", "POSSIBLE_BUG", "NEEDS_REVIEW", "TEST_FIX",
                  "NOT_ATTRIBUTABLE",
                  "FALSE_POSITIVE", "ENV_FAILURE", "DID_NOT_RUN",
                  "AUTO_CLASSIFIED", "PENDING"]

# NOT_ATTRIBUTABLE is a DISPLAY label, never a stored verdict. A low-confidence
# NEEDS_REVIEW is the classifier saying "I could not attribute this", not "a
# human must review 153 failures" — and reporting the latter to a dev team
# misrepresents what was actually said. The stored classification stays
# NEEDS_REVIEW, so nothing in §4, the picklist, the writer or the rubric moves.
_UNATTRIBUTED_FROM = "NEEDS_REVIEW"
_UNATTRIBUTED_AT = {"low", ""}


def display_verdict(classification, confidence) -> str:
    """The label to SHOW for a row. See _UNATTRIBUTED_FROM above."""
    cls = _text(classification)
    if cls == _UNATTRIBUTED_FROM and _text(confidence).lower() in _UNATTRIBUTED_AT:
        return "NOT_ATTRIBUTABLE"
    return cls

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
  .matrix-title { font-weight: 700; font-size: 13px; margin-bottom: 6px; }
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
  .matrix td.same   { background: #f4f6f8; color: var(--c-fg); }
  .matrix td.worse  { background: #fdecea; color: #a4302a; }
  .matrix td.better { background: #e9f7ef; color: #1e6b45; }
  .matrix td.zero   { background: #fbfcfd; }
  .totals .pill {
    display: inline-flex; align-items: baseline; gap: 6px; font-size: 13px;
  }
  .totals .pill .n { font-weight: 700; font-size: 16px; }
  /* The fan-out: how many case rows the clusters cover. Secondary on purpose
     — clusters are the unit of work, cases are the blast radius. */
  .totals .pill .fanout { color: var(--c-muted); font-size: 11.5px; }
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
  .filters label { font-weight: 600; }
  /* The selects must be able to shrink: five of them at a fixed 200px kept
     the controls column above 1000px, which forced the matrix to wrap onto
     its own row instead of sitting beside them. */
  .filters select { min-width: 0; flex: 1 1 140px; max-width: 220px; }
  .filters input[type="search"] { min-width: 0; flex: 1 1 220px; }
  .filters-row > label { white-space: nowrap; }
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
  th.col-baseline, td.col-baseline { width: 78px; text-align: center; }
  td.col-baseline { font-weight: 700; font-size: 12px; }
  td.col-baseline.novel   { color: var(--c-bug); }
  td.col-baseline.rare    { color: #b9770e; }
  td.col-baseline.chronic { color: var(--c-muted); font-weight: 400; }
  th.col-verdict { width: 130px; }
  th.col-confidence, td.col-confidence { width: 90px; text-align: center; }
  th.col-culprit, td.col-culprit { min-width: 220px; }
  th.col-reasoning, td.col-reasoning { min-width: 320px; }
  td.col-culprit, td.col-reasoning { white-space: normal; line-height: 1.45; }
  th.col-jira, td.col-jira { width: 110px; text-align: center; }
  th.col-jira { cursor: help; }
  a.jira-create {
    display: inline-block; padding: 4px 10px; background: #0052cc;
    color: #fff; border-radius: 3px; font-size: 12px;
    text-decoration: none; white-space: nowrap;
  }
  a.jira-create:hover { background: #0747a6; }
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
  tr.cluster-row a.jira-create { font-size: 11.5px; padding: 3px 8px; white-space: normal; }
  /* Member rows: indented, shared cells collapse to a pointer. */
  tr.case-row.in-cluster td.col-idx { padding-left: 18px; }
  .same-as-cluster { color: var(--c-muted); font-size: 12px; white-space: nowrap; }
  /* The arrow is only meaningful while the visible group IS the cluster. */
  table.per-test-table[data-mode="cluster"] .own-value { display: none; }
  table.per-test-table:not([data-mode="cluster"]) .same-as-cluster { display: none; }
  .same-as-cluster a { color: #0747a6; text-decoration: none; }
  .same-as-cluster a:hover { text-decoration: underline; }
  .cluster-key { font-size: 11px; color: var(--c-muted); }
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

  document.addEventListener('click', function (e) {
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
    document.querySelectorAll('tr.case-row').forEach(function (tr) {
      var hit = matches(tr);
      // A filter hit always shows, even inside a collapsed cluster —
      // otherwise a search silently misses rows that are folded away.
      var hide = !hit || (!filtering && tr.dataset.collapsed === '1');
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
    count.textContent = shown + ' of ' + document.querySelectorAll('tr.case-row').length + ' shown';
  }

  Object.keys(sel).forEach(function (k) {
    if (sel[k]) sel[k].addEventListener('change', applyFilters);
  });
  search.addEventListener('input', applyFilters);
  search.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { search.value = ''; applyFilters(); }
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


def _rank(verdict) -> int:
    v = _text(verdict)
    return _VERDICT_ORDER.index(v) if v in _VERDICT_ORDER else len(_VERDICT_ORDER)


def _rollup(verdicts) -> str:
    """The most severe verdict in a group — what a cluster header shows.

    A cluster is only as safe as its worst member: one BUG among thirty
    FALSE_POSITIVEs is still a BUG, and rolling up to the majority would hide
    exactly the row worth acting on. Returns "" when nothing is classified.
    """
    best, best_rank = "", len(_VERDICT_ORDER)
    for v in verdicts:
        t = _text(v)
        if not t:
            continue
        r = _rank(t)
        if r < best_rank:
            best, best_rank = t, r
    return best


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
        culprit_cell = '<span class="cluster-culprit-none">no culprit named</span>'

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
        + _novelty_cell(
            min((c for c in members.get("baseline_signature_count", [])
                 if pd.notna(c)), default=None))
        + f'<td class="col-verdict cluster-cell"{_verdict_rank_attr(verdict)}>{_verdict_span(verdict)}</td>'
        f'<td class="col-confidence cluster-cell"{_conf_rank_attr(top)}'
        + (f' title="Highest confidence in this group. Breakdown: {_esc(breakdown)}"' if breakdown else "")
        + f">{_conf_span(top)}</td>"
        f'<td class="col-culprit cluster-cell cluster-culprit">{culprit_cell}</td>'
        f'<td class="col-reasoning cluster-cell cluster-reason">{reason_cell}</td>'
        + f'<td class="col-jira cluster-cell">'
        + _jira_link(meta, verdict=verdict,
                     summary_text=(next(iter(reasons)) if len(reasons) == 1 else ''),
                     rows=[members.iloc[0]] if len(members) else [],
                     n=n)
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
            culprit_cell = f"<code>{_esc(culprit)}</code>" if culprit else "—"
            commits = _text(r.get("culprit_commits"))
            if culprit and commits:
                culprit_cell += f'<div class="culprit-commits">{_esc(commits)}</div>'

        # A cluster with one verdict says it once, on its header. Repeating it
        # on every member is the noise that made the flat table unreadable.
        # When a cluster IS mixed, the per-row value is the whole point, so it
        # stays.
        if cno is not None and _shared_in_cluster(shared, ckey, "display_verdict", verdict):
            verdict_cell = _collapsible(_verdict_span(verdict), verdict)
        else:
            verdict_cell = _verdict_span(verdict)
        conf = _text(r.get("confidence"))
        if cno is not None and _shared_in_cluster(shared, ckey, "confidence", conf):
            conf_cell = _collapsible(_conf_span(conf), conf)
        else:
            conf_cell = _conf_span(r.get("confidence"))

        issues = _text(r.get("linked_issues"))
        # An already-linked issue wins: the button exists to avoid filing
        # a duplicate, so where Testray already has one we show that.
        jira_cell = _esc(issues) if issues else _jira_link(
            meta, verdict=verdict, summary_text=reason, rows=[r], n=1)

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
            + _novelty_cell(r.get("baseline_signature_count"))
            + 
            f'<td class="col-verdict"{_verdict_rank_attr(verdict)}>{verdict_cell}</td>'
            f'<td class="col-confidence"{_conf_rank_attr(conf)}>{conf_cell}</td>'
            f'<td class="col-culprit">{culprit_cell}</td>'
            f'<td class="col-reasoning">{reason_cell}</td>'
            f'<td class="col-jira">{jira_cell}</td>'
            "</tr>"
        )
        parts.append(_detail_row(r, rid, css, ckey, meta))
    return "\n".join(parts)



def _jira_link(meta: dict, *, verdict: str, summary_text: str, rows: list,
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

    href = (f"{base}/secure/CreateIssueDetails!init.jspa?"
            + urllib.parse.urlencode(params))
    return (f'<a class="jira-create" href="{_esc(href)}" target="_blank" '
            f'rel="noopener" title="Opens a prefilled Jira draft — nothing is '
            f'filed automatically">Create ticket</a>')


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


def _detail_row(r, rid: str, css: str, ckey: str, meta: dict) -> str:
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
    add("Culprit file", f"<code>{_esc(culprit)}</code>" if culprit else "—")

    commits = _text(r.get("culprit_commits"))
    if commits:
        add("Changed by", _esc(commits))

    change = _text(r.get("specific_change"))
    if change:
        add("Specific change", _esc(change))

    add("Reasoning", _esc(_text(r.get("reason"))) or "—")

    issues = _text(r.get("linked_issues"))
    add("Ticket already linked", _esc(issues) if issues else "—")

    add("Cluster key", f'<code>{_esc(ckey)}</code>')

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


_HINT = """Cluster headers sit on the same columns as their member rows: the test count and
  cluster key under <em>Test</em>, the component and team rollups under their own columns
  (hover for the full breakdown), the shared culprit file and reasoning under theirs.
  <strong>Group by</strong> re-cuts the same rows in place &mdash; the default
  <strong>error signature</strong> is the <code>clusterKey</code> the pipeline persists;
  component, team and verdict regroup without a re-render.
  Click a cluster header to fold its members in or out; click a row to expand its own
  details. Member rows don't repeat the shared culprit and reasoning &mdash; those are on
  the header, in the row's hover title, and in full in its detail panel.
  Searching or filtering opens every cluster that has a hit.
  Press <kbd>Esc</kbd> in the search box to clear."""



# Testray's own comparison order, so the matrix reads the same way it does in
# the product.
_STATUS_ORDER = ["PASSED", "FAILED", "BLOCKED", "TESTFIX", "UNTESTED", "DIDNOTRUN"]
_STATUS_LABEL = {"TESTFIX": "Test Fix", "UNTESTED": "DNR", "DIDNOTRUN": "DNR"}


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
            cls = "same" if r == c else ("worse" if _worse(r, c) else "better")
            cells.append(f'<td class="{cls}">{n:,}</td>' if n else '<td class="zero"></td>')
        body.append(f'<tr><th>A<br><span>{_esc(_status_label(r))}</span></th>'
                    + "".join(cells) + "</tr>")
    return ('<div class="matrix"><div class="matrix-title">Runs</div>'
            f'<table><thead><tr><th></th>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _worse(a: str, b: str) -> bool:
    """Did the status get worse from A to B? PASSED is the only good state."""
    return a == "PASSED" and b != "PASSED"



# R2: signature novelty. Confidence cannot rank this data — the run split
# 140 low / 13 medium / 0 high — so a reader falls back to the transition
# label, which invites a cut that looks obvious and destroys real defects.
# Novelty is uniform across `new` and `changed`, and it is the honest key.
_NOVELTY = [(0, 0, "novel", "Never seen in the baseline"),
            (1, 4, "rare", "Rare in the baseline (1-4)"),
            (5, 10 ** 9, "chronic", "Already chronic in the baseline (5+)")]


def _novelty_bucket(count) -> tuple[str, str]:
    try:
        n = int(count)
    except (TypeError, ValueError):
        return "", ""
    for lo, hi, key, label in _NOVELTY:
        if lo <= n <= hi:
            return key, label
    return "", ""


def _novelty_cell(count) -> str:
    key, label = _novelty_bucket(count)
    if not key:
        return '<td class="col-baseline"></td>'
    n = int(count)
    return (f'<td class="col-baseline {key}" data-sort="{n:07d}" '
            f'title="{_esc(label)}">{n}</td>')


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
    pills.append('<span class="pill" title="Distinct clusterKey values — one cluster '
                 'per normalized error signature (ARCHITECTURE §7)."><strong>'
                 f'Root-cause clusters:</strong> <span class="n" '
                 f'data-pill-clusters="1">{n_clusters}</span></span>')
    return '<div class="totals">' + "".join(pills) + "</div>"


def _select(el_id: str, label: str, values) -> str:
    opts = "".join(f'<option value="{_esc(v)}">{_esc(v)}</option>'
                   for v in sorted({_text(x) for x in values if _text(x)}))
    return (f'<label for="{el_id}">{_esc(label)}:</label>'
            f'<select id="{el_id}"><option value="">All</option>{opts}</select>')


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

    # One derived column so the pills, the filter, the cluster rollups, the
    # sort rank and the row cells cannot disagree about what a row is called.
    if len(df):
        df = df.copy()
        df["display_verdict"] = [
            display_verdict(c, f)
            for c, f in zip(df.get("classification", [""] * len(df)),
                            df.get("confidence", [""] * len(df)))
        ]
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
        summary_bits.append(f"Hashes: <code>{_esc(ha[:9])}</code> &rarr; <code>{_esc(hb[:9])}</code>")
    sig = _text(meta.get("signature_version")) or error_signature.SIGNATURE_VERSION
    summary_bits.append(f"Signatures: <code>{_esc(sig)}</code>")

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Triage report — {_esc(a_name)} → {_esc(b_name)}</title>
<style>{_CSS}</style>
</head>
<body>
<main>
  <h1>Triage report &mdash; {a_html} <span class="role">baseline</span> &rarr; {b_html} <span class="role">target</span></h1>
  <p class="summary">{" &middot; ".join(summary_bits)}</p>
  {_banners(df, meta)}

  <div class="headline">
   <div class="controls">
    {_totals(df, len(cluster_groups), meta, cluster_groups)}
  <div class="viewbar">
    <span class="viewbar-group">
      <label class="viewbar-label" for="group-by">Group by:</label>
      <select id="group-by">
        {"".join(f'<option value="{m}">{l}</option>' for m, l in _GROUP_MODES)}
      </select>
      <span class="viewbar-note">re-cuts the same rows; nothing is re-classified</span>
    </span>
  </div>

  <div class="filters">
    <div class="filters-row">
      {_select("team-filter", "Team", df.get("team_name", []))}
      {_select("component-filter", "Component", df.get("component_name", []))}
      {_select("verdict-filter", "Verdict", df.get("display_verdict", []))}
      {_select("confidence-filter", "Confidence",
               [_text(c).lower() for c in df.get("confidence", [])])}
      {_select("transition-filter", "Transition", df.get("transition", []))}
    </div>
    <div class="filters-row">
      <label for="search-filter">Search:</label>
      <input id="search-filter" type="search" autocomplete="off"
             placeholder="filter by test, component, culprit file, reasoning… (Esc to clear)">
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
  <p class="hint">{_HINT}</p>
  <table class="per-test-table clustered">
<thead><tr>
<th class="col-idx">#</th>
<th class="col-test" title="Click a test name to open its Testray case result.">Test</th>
<th class="col-team">Team</th>
<th class="col-comp">Component</th>
<th class="col-status">Status</th>
<th class="col-baseline" title="How many times this error signature already occurred among the BASELINE build\'s own failures. 0 = genuinely new failure mode.">Baseline</th>
<th class="col-verdict">Verdict</th>
<th class="col-confidence" title="Classifier confidence: high / medium / low / auto.">Confidence</th>
<th class="col-culprit">Culprit file</th>
<th class="col-reasoning">Reasoning</th>
<th class="col-jira" title="Tickets already linked on the Testray case result. Creation is LPD-95849, not wired up.">Ticket</th>
</tr></thead>
<tbody>
{chr(10).join(headers)}
{body}
</tbody>
  </table>
</main>
<script type="application/json" id="group-order">{json.dumps(order)}</script>
<script>{_JS}</script>
</body>
</html>"""

    out = run_dir / "report.html"
    out.write_text(doc, encoding="utf-8")
    return out
