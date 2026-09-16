

# --- Testray's aggregate row is not a test ----------------------------------

def test_the_aggregate_row_is_dropped_from_the_triage_set():
    """It carries no error text and is FAILED whenever anything under it is, so
    it could never be classified — but it used to stay in the frame and count
    toward total_failures, making a one-failure build report two."""
    import pandas as pd
    from testray_analytics.analysis import prepare as P

    df = pd.DataFrame({
        "test_case": ["Top Level Build", "semantic-versioning/0/0"],
        "error_message": [None, "baseline task failed"],
    })

    out, dropped = P.drop_aggregate_rows(df)

    assert list(out["test_case"]) == ["semantic-versioning/0/0"]
    assert list(out.index) == [0], "index is reset so downstream .loc is safe"
    assert dropped == {}, "no transition column here, so nothing to decrement"


def test_the_aggregate_row_is_matched_by_label_not_case_id():
    """The case id is 42588 on prod and assigned per instance elsewhere, so an
    id check stops matching the moment this runs against a local mirror."""
    from testray_analytics.analysis import prepare as P

    assert P.is_aggregate_row("Top Level Build")
    assert P.is_aggregate_row("  top level build  ")
    assert not P.is_aggregate_row("semantic-versioning/0/0")
    assert not P.is_aggregate_row(None)


def test_dropping_the_aggregate_row_leaves_an_ordinary_frame_alone():
    import pandas as pd
    from testray_analytics.analysis import prepare as P

    df = pd.DataFrame({"test_case": ["a", "b"], "error_message": ["x", "y"]})

    out, dropped = P.drop_aggregate_rows(df)
    assert len(out) == 2 and dropped == {}


def test_the_dropped_row_is_subtracted_from_the_transition_counts():
    """`transitionCounts` is computed before the drop and reaches Testray as
    the field the CX reads to say how many pre-existing failures there were.
    Left alone it reported `same_failure: 2` against `totalFailures: 1`."""
    import pandas as pd
    from testray_analytics.analysis import prepare as P

    df = pd.DataFrame({
        "test_case": ["Top Level Build", "semantic-versioning/0/0"],
        "transition": ["same_failure", "same_failure"],
        "error_message": [None, "baseline task failed"],
    })

    out, dropped = P.drop_aggregate_rows(df)

    assert len(out) == 1
    assert dropped == {"same_failure": 1}

    transitions = {"same_failure": 2, "other": 538}
    for t, n in dropped.items():
        transitions[t] = max(0, transitions.get(t, 0) - n)
    assert transitions == {"same_failure": 1, "other": 538}
