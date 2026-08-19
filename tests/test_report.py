"""
Tests for the subtask-level HTML report.

Layout follows RAP's `report_bysubtask.html`: one flat sortable/filterable row
per subtask plus detail sections, rather than nested expanding clusters — at
481 subtasks the nested form could not be sorted, filtered, or scanned across.

The invariants worth pinning:
  - one row per subtask, and unlinked cases never merge into a fake cluster
  - a CHANGED_FAILURE is never presented as a fresh regression
  - an unclassified run says so (absence of BUG ≠ no bug)
  - light mode only — no prefers-color-scheme branch
  - ids are unique and deterministic (anchors/deep-links must not collide)
"""

import re

import pandas as pd
import pytest

# --- 2026-08-18: the by-subtask renderer is NOT being built -----------------
# This file specifies a FLAT one-row-per-subtask layout (see the docstring
# above). What shipped instead is the nested clustered renderer, grouped on our
# own `clusterKey` rather than Testray's subtask_id — driven by `by-cluster`
# becoming the default classification mode (ARCHITECTURE §7).
#
# The tests describing the flat subtask layout are skipped rather than deleted:
# they are the only written record of that design, and if per-subtask reporting
# ever returns they are the spec to build against. Everything NOT marked here
# still guards the live renderer and must keep passing.
by_subtask = pytest.mark.skip(
    reason="by-subtask renderer intentionally not built — the nested "
           "clustered renderer ships instead (ARCHITECTURE §7)")

from testray_analytics.analysis import prepare
from testray_analytics.analysis.report import _rollup, render_run

META = {
    "run_id": "r_test", "build_id_a": 1, "build_id_b": 2, "routine_id": 82964,
    "classifier": "api:test", "mode": "by-subtask", "signature_version": "v1",
    "testflow_id": 999, "git_hash_a": "a" * 40, "git_hash_b": "b" * 40,
}


def _row(cid, sid, verdict="NEEDS_REVIEW", transition="NEW_FAILURE", **kw):
    base = dict(
        testray_case_id=cid, subtask_id=sid, test_case=f"Test{cid}",
        component_name="Comp", team_name="Team", status_a="PASSED",
        status_b="FAILED", transition=transition, classification=verdict,
        confidence="medium", culprit_file=None, specific_change=None,
        reason="because", error_message="boom", baseline_error_message="",
    )
    base.update(kw)
    return base


def _render(rows, tmp_path, meta=None):
    render_run(tmp_path, pd.DataFrame(rows), meta or META)
    return (tmp_path / "report.html").read_text(encoding="utf-8")


def _row_ids(html):
    return re.findall(r'<tr id="(subtask-[^"]+)"', html)


# --- one row per subtask ---------------------------------------------------

@by_subtask
def test_one_table_row_per_subtask(tmp_path):
    html = _render([_row(1, 555), _row(2, 555), _row(3, 777)], tmp_path)
    assert len(_row_ids(html)) == 2
    assert "subtask-555" in html and "subtask-777" in html


@by_subtask
def test_unlinked_cases_do_not_merge_into_one_row(tmp_path):
    """Testray never clustered these, so collapsing them would invent a
    cluster out of unrelated failures."""
    html = _render([_row(1, 0), _row(2, 0), _row(3, None)], tmp_path)
    assert len(_row_ids(html)) == 3


def test_row_ids_are_unique_and_deterministic(tmp_path):
    """Duplicate ids would send every cluster deep-link to the first match;
    hash-based ids would change on every render."""
    rows = [_row(1, 0), _row(2, 0), _row(3, 111)]
    first = _render(rows, tmp_path)
    second = _render(rows, tmp_path)
    ids = _row_ids(first)
    assert len(ids) == len(set(ids)), f"duplicate row ids: {ids}"
    assert _row_ids(second) == ids


def test_cluster_rollup_is_most_severe_member():
    assert _rollup(["NEEDS_REVIEW", "BUG", "FALSE_POSITIVE"]) == "BUG"
    assert _rollup(["FALSE_POSITIVE", "NEEDS_REVIEW"]) == "NEEDS_REVIEW"
    assert _rollup([None, ""]) == ""


# --- §12 transitions ------------------------------------------------------

def test_changed_failure_warning_fires_on_the_transition_prepare_emits(tmp_path):
    """The guard must match `prepare`'s vocabulary, not just the fixtures'.

    Regression: the guard compared against "CHANGED_FAILURE", which
    `prepare.compute_test_diff` never produces — it emits TRANSITION_CHANGED
    ("changed"). The test above passed on a value real data never carries, so
    on every real run the baseline warning silently did not render and a test
    that was already failing was presented as a fresh regression.
    """
    html = _render([_row(1, 900, transition=prepare.TRANSITION_CHANGED,
                         error_message="ElementNotFound",
                         baseline_error_message="TimeoutException")], tmp_path)
    assert "already failing on the baseline" in html
    assert "Was failing with" in html and "Now failing with" in html


def test_changed_failure_shows_both_errors_and_the_warning(tmp_path):
    html = _render([_row(1, 900, transition="CHANGED_FAILURE",
                         error_message="ElementNotFound",
                         baseline_error_message="TimeoutException")], tmp_path)
    assert "already failing on the baseline" in html
    assert "Was failing with" in html and "Now failing with" in html
    assert "TimeoutException" in html and "ElementNotFound" in html
    assert "changed failure" in html


@by_subtask
def test_new_failure_shows_single_shared_error(tmp_path):
    html = _render([_row(1, 901, error_message="NPE")], tmp_path)
    assert "Shared error" in html
    assert "already failing on the baseline" not in html


@pytest.mark.skip(reason="the Excluded-from-triage banner was removed on request 2026-08-18 — the A x B status matrix carries the same facts more completely")
def test_excluded_transitions_are_reported(tmp_path):
    """A run must say what it chose not to triage, or the report reads as
    'these were the only differences'."""
    html = _render([_row(1, 1)], tmp_path)
    assert "Excluded from triage" not in html
    meta = dict(META, transition_counts={"SAME_FAILURE": 690, "FIXED": 11657})
    html = _render([_row(1, 1)], tmp_path, meta)
    assert "690 already failing, same error" in html
    assert "11657 now passing" in html


def test_transition_filter_is_offered(tmp_path):
    html = _render([_row(1, 1), _row(2, 2, transition="CHANGED_FAILURE")], tmp_path)
    assert 'id="transition-filter"' in html
    assert 'data-transition="CHANGED_FAILURE"' in html


# --- analysis affordances -------------------------------------------------

def test_filters_and_sorting_are_present(tmp_path):
    html = _render([_row(1, 1)], tmp_path)
    for probe in ('id="verdict-filter"', 'id="team-filter"',
                  'id="component-filter"', 'id="search-filter"',
                  'id="filter-count"', "sort-ind"):
        assert probe in html, probe


def test_verdict_pills_are_clickable_filters(tmp_path):
    html = _render([_row(1, 1, verdict="BUG", culprit_file="Foo.java")], tmp_path)
    assert 'data-pill-for="BUG"' in html
    assert 'data-pill-verdict="BUG"' in html


@by_subtask
def test_actionable_verdicts_get_a_root_cause_cluster(tmp_path):
    html = _render([_row(1, 1, verdict="BUG", culprit_file="modules/Foo.java")],
                   tmp_path)
    assert 'class="clusters-collapse"' in html
    assert "modules/Foo.java" in html


def test_false_positives_are_kept_out_of_clusters(tmp_path):
    """The panel groups actionable work; hundreds of flakes would bury it."""
    html = _render([_row(1, 1, verdict="FALSE_POSITIVE")], tmp_path)
    assert 'class="clusters-collapse"' not in html


@by_subtask
def test_testray_and_jira_links_are_built(tmp_path):
    html = _render([_row(1, 555)], tmp_path)
    assert "testflow/999/subtasks/555" in html
    assert "CreateIssueDetails" in html


@by_subtask
def test_subtask_id_is_the_testray_link(tmp_path):
    """The id used to anchor to its own row (`#subtask-555`) while a separate
    column held the only real link. One link, on the identifier."""
    html = _render([_row(1, 555)], tmp_path)
    row = html.split('<tr id="subtask-555"')[1].split("</tr>")[0]
    assert "testflow/999/subtasks/555" in row
    assert 'href="#subtask-555"' not in row
    assert ">open<" not in row                  # the standalone cell is gone
    assert '<th class="col-link">' not in html   # ...and so is its header
    # Cluster-panel entries still link out to Testray — that is a different,
    # intentional link, so assert on the header, not on the word "Testray".


@by_subtask
def test_subtask_id_is_plain_text_without_a_testflow_id(tmp_path):
    meta = {k: v for k, v in META.items() if k != "testflow_id"}
    html = _render([_row(1, 555)], tmp_path, meta)
    row = html.split('<tr id="subtask-555"')[1].split("</tr>")[0]
    assert "<a" not in row.split("col-jira")[0]   # no link before the Jira cell
    assert "555" in row


def test_testray_links_omitted_without_a_testflow_id(tmp_path):
    meta = {k: v for k, v in META.items() if k != "testflow_id"}
    html = _render([_row(1, 555)], tmp_path, meta)
    assert "subtasks/555" not in html


# --- presentation ---------------------------------------------------------

def test_light_mode_only(tmp_path):
    """Explicitly requested: no dark mode from system config."""
    html = _render([_row(1, 1)], tmp_path)
    assert "prefers-color-scheme" not in html
    assert 'data-theme' not in html


def test_verdict_never_relies_on_color_alone(tmp_path):
    html = _render([_row(1, 1, verdict="BUG", culprit_file="Foo.java")], tmp_path)
    assert ">BUG<" in html


def test_pending_run_announces_itself(tmp_path):
    html = _render([_row(1, 1, verdict="PENDING")], tmp_path)
    assert "Classification did not run" in html
    assert "not yet assessed" in html


def test_classified_run_has_no_pending_banner(tmp_path):
    html = _render([_row(1, 1, verdict="BUG", culprit_file="Foo.java")], tmp_path)
    assert "Classification did not run" not in html


def test_empty_frame_renders(tmp_path):
    html = _render([], tmp_path)
    assert "No rows." in html


@pytest.mark.parametrize("bad", [None, float("nan")])
@by_subtask
def test_missing_transition_does_not_crash(tmp_path, bad):
    html = _render([_row(1, 5, transition=bad)], tmp_path)
    assert len(_row_ids(html)) == 1


@by_subtask
def test_missing_optional_columns_do_not_crash(tmp_path):
    """Per-test bundles and older runs lack transition/baseline columns."""
    rows = [{k: v for k, v in _row(1, 5).items()
             if k not in ("transition", "baseline_error_message",
                          "specific_change")}]
    html = _render(rows, tmp_path)
    assert len(_row_ids(html)) == 1


def test_long_error_is_truncated_not_dumped(tmp_path):
    html = _render([_row(1, 1, error_message="x" * 5000)], tmp_path)
    assert "[truncated]" in html
