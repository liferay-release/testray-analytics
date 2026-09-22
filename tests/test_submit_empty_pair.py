"""The clean pair: prepare found nothing to submit, and submit must still run.

`triage_pipeline.sh` skips classify when no cluster is classifiable and goes
straight to submit, because the report's pre-existing section and the coverage
figures are the whole point of a run like that. On 2026-09-21 pair
527271658 -> 527557324 reached that path with an EMPTY diff_list and submit
died in pandas, after prepare had already done all the work.

The mechanism is worth knowing, because it is silent until it is fatal: on an
empty frame `DataFrame.apply(fn, axis=1)` never sees a row. It probes `fn`
once, and when the probe raises it returns a frame shaped like the INPUT
instead of the function's own shape. So the concat duplicated all 15 diff_list
columns and the next single-column assignment got a DataFrame:
`ValueError: Columns must be same length as key`.
"""

import pandas as pd

from testray_analytics.analysis import submit as S

COLUMNS = ["testray_case_id", "test_case", "component_name", "team_name",
           "status_a", "status_b", "transition", "known_flaky",
           "linked_issues", "error_message", "baseline_error_message",
           "pre_classification", "caseresult_id", "subtask_id", "console_url"]


def empty_diff_list() -> pd.DataFrame:
    """The real shape: 15 columns, no rows."""
    return pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})


def test_the_subtask_path_survives_a_pair_with_nothing_to_submit():
    out = S.assemble_dataframe_subtask(empty_diff_list(), [], {})
    assert len(out) == 0
    assert out.columns.duplicated().sum() == 0, \
        "the concat duplicated diff_list's own columns"
    for col in ("classification", "reason", "subtask_id"):
        assert col in out.columns


def test_the_plain_path_survives_it_too():
    out = S.assemble_dataframe(empty_diff_list(), [])
    assert len(out) == 0
    assert out.columns.duplicated().sum() == 0
    assert "classification" in out.columns


def _one_row() -> pd.DataFrame:
    df = empty_diff_list()
    df.loc[0] = [101, "SomeTest#one", "Calendar", "Core", "PASSED", "FAILED",
                 "new", False, "", "boom", "", None, 9001, None, ""]
    return df


def test_a_non_empty_pair_is_unchanged_on_the_subtask_path():
    """The guard must not alter the path that was already working."""
    out = S.assemble_dataframe_subtask(
        _one_row(),
        [{"group_id": 1, "subtask_id": 7, "case_ids": [101],
          "classification": "BUG", "confidence": "high", "reason": "because",
          "culprit_file": "a/b.java"}],
        {})
    assert len(out) == 1
    assert out.iloc[0]["classification"] == "BUG"
    assert out.columns.duplicated().sum() == 0


def test_a_non_empty_pair_is_unchanged_on_the_plain_path():
    out = S.assemble_dataframe(
        _one_row(),
        [{"testray_case_id": 101, "classification": "BUG",
          "confidence": "high", "reason": "because",
          "culprit_file": "a/b.java"}])
    assert len(out) == 1
    assert out.iloc[0]["classification"] == "BUG"
    assert out.columns.duplicated().sum() == 0
