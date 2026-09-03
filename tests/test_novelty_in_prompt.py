"""baseline_signature_count rides on the group and the CSV — and NOT the prompt.

Feeding novelty to the classifier (a NOVEL / rare / CHRONIC rubric plus a
per-group meta line) was tried on 2026-08-24 and reverted on 2026-08-26: it is
the prime suspect for the run where TEST_FIX collapsed 639 rows -> 0, with 599
cases flipping TEST_FIX -> NEEDS_REVIEW. The history filter explained only 38
of them.

So the count stays a diagnostic: report.py renders it per row, the group carries
it, diff_list_subtasks.csv records it, and the last test here guards the revert
by asserting it does not reach prompt.md. If the rubric is ever re-tested, that
guard is the thing to flip -- deliberately, not by accident.
"""
import pandas as pd
import pytest

from testray_analytics.analysis.prepare import (
    MODE_BY_CLUSTER, compute_subtask_groups, write_diff_list_subtasks,
)


# Distinct WORDS, not distinct digits: normalize() folds numbers to a volatile
# token, so "failure 0" and "failure 1" are one cluster, not two.
_WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]


def _rows(counts):
    """One triage row per baseline_signature_count, each its own cluster."""
    return pd.DataFrame([
        {"testray_case_id": 100 + i, "caseresult_id": 200 + i,
         "test_case": f"Test{i}", "component_name": "Comp", "team_name": "T",
         "status_a": "PASSED", "status_b": "FAILED",
         "transition": "new", "known_flaky": False,
         "linked_issues": "", "pre_classification": None,
         "error_message": f"NoSuchElementException on the {_WORDS[i]} selector",
         "subtask_id": 0, "baseline_signature_count": n}
        for i, n in enumerate(counts)
    ])


def _group_for(count):
    groups = compute_subtask_groups(_rows([count]), mode=MODE_BY_CLUSTER)
    assert len(groups) == 1
    return groups[0]


@pytest.mark.parametrize("count", [0, 3, 17])
def test_group_carries_the_count(count):
    assert _group_for(count)["baseline_signature_count"] == count


def test_group_takes_the_minimum_so_any_novel_member_makes_it_novel():
    """A cluster holding one brand-new failure and one chronic one is not
    chronic — the new member is the reason a human is looking at it."""
    df = _rows([0, 9])
    df["error_message"] = "TimeoutException waiting for the shared widget"
    groups = compute_subtask_groups(df, mode=MODE_BY_CLUSTER)
    assert len(groups) == 1, "fixture should produce a single shared cluster"
    assert groups[0]["baseline_signature_count"] == 0


def test_missing_column_degrades_to_none_not_a_crash():
    df = _rows([0]).drop(columns=["baseline_signature_count"])
    assert _group_for.__name__  # keep linters quiet
    groups = compute_subtask_groups(df, mode=MODE_BY_CLUSTER)
    assert groups[0]["baseline_signature_count"] is None


def test_count_reaches_the_group_csv(tmp_path):
    groups = compute_subtask_groups(_rows([0, 12]), mode=MODE_BY_CLUSTER)
    write_diff_list_subtasks(tmp_path, groups)
    csv = pd.read_csv(tmp_path / "diff_list_subtasks.csv")
    assert "baseline_signature_count" in csv.columns
    assert set(csv["baseline_signature_count"].dropna().astype(int)) == {0, 12}


def test_novelty_never_reaches_the_prompt(tmp_path):
    """Regression guard for the 2026-08-26 revert. The count is a diagnostic;
    the classifier must not see it, and must not be given a rubric that
    branches on novel vs chronic."""
    from testray_analytics.analysis.prepare import write_prompt_grouped

    groups = compute_subtask_groups(_rows([0, 12]), mode=MODE_BY_CLUSTER)
    (tmp_path / "hunks.txt").write_text("no hunks")
    (tmp_path / "git_diff_full.diff").write_text("")

    write_prompt_grouped(
        tmp_path, run_id="r_test", classifier="agent:test",
        build_a=1, build_b=2, hash_a="a" * 40, hash_b="b" * 40,
        routine_id=None, build_a_name="A", build_b_name="B",
        groups_to_classify=groups, groups_auto=[], groups_flaky=[],
        n_member_cases=2, n_auto_cases=0, n_flaky_cases=0,
        hunks_path=tmp_path / "hunks.txt",
        full_diff_path=tmp_path / "git_diff_full.diff",
        git_repo=tmp_path,
    )
    prompt = (tmp_path / "prompt.md").read_text()

    assert "baseline_signature_count" not in prompt
    assert "NOVEL" not in prompt and "CHRONIC" not in prompt

    # The rubric must not steer on novelty either. Collapse whitespace: the
    # rubric is wrapped prose, so line breaks fall wherever they fall.
    flat = " ".join(prompt.split())
    assert "chronic signature with no matching hunk" not in flat
    assert "prefer **POSSIBLE_BUG** over NEEDS_REVIEW" not in flat

    # ...but the rest of the rubric is untouched.
    assert "### Transitive dependencies" in prompt
