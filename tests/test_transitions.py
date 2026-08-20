"""
Unit tests for §12 transition detection and §7/§12 error normalization.

The FAILED→FAILED signal is only as good as `normalize()` (§12: "only
trustworthy once it's decent"), so these tests pin the two failure modes that
matter: volatile tokens must NOT read as a changed error (false positives
would flood a run with every pre-existing baseline failure), and a genuinely
different error MUST read as changed (false negatives silently undercount —
the very thing §12 exists to fix).
"""

import pandas as pd
import pytest

from testray_analytics.analysis import error_signature as es
from testray_analytics.analysis.prepare import (
    TRANSITION_AWARENESS, TRANSITION_BLOCKED, TRANSITION_CHANGED,
    TRANSITION_FIXED, TRANSITION_NEW, TRANSITION_NO_BASELINE,
    TRANSITION_SAME_FAILURE, TRANSITION_TESTFIX, classify_transition,
    compute_test_diff,
)


# --- normalize(): volatile tokens must collapse ----------------------------

@pytest.mark.parametrize("a,b", [
    # Line numbers in stack frames
    ("java.lang.NullPointerException at Foo.java:42",
     "java.lang.NullPointerException at Foo.java:87"),
    # Timing / durations
    ("Timed out after 30000ms waiting for element",
     "Timed out after 45000ms waiting for element"),
    # Timestamps
    ("Build failed at 2026-07-01T08:12:09Z",
     "Build failed at 2026-08-03T16:15:57Z"),
    # Jenkins workspace paths
    ("Cannot read /opt/jenkins/workspace/build-1234/foo.log",
     "Cannot read /opt/jenkins/workspace/build-9999/foo.log"),
    # Java identity hashes
    ("Expected com.liferay.Foo@1a2b3c4d but got null",
     "Expected com.liferay.Foo@9f8e7d6c but got null"),
    # UUIDs
    ("No such entry 3f2504e0-4f89-11d3-9a0c-0305e82c3301",
     "No such entry 550e8400-e29b-41d4-a716-446655440000"),
    # Hosts / ports in URLs
    ("Connection refused to http://localhost:8080/api/x",
     "Connection refused to http://localhost:9090/api/x"),
    # Whitespace + case
    ("Element   Not  Present", "element not present"),
])
def test_volatile_tokens_do_not_count_as_changed(a, b):
    assert es.normalize(a) == es.normalize(b)
    assert not es.signatures_differ(a, b)


@pytest.mark.parametrize("a,b", [
    ("java.lang.NullPointerException at Foo.java:42",
     "java.lang.IllegalStateException at Foo.java:42"),
    ("Timed out waiting for element",
     "ElementNotFoundPoshiRunnerException: selector #foo not found"),
    ("Expected 'Save' but got 'Submit'",
     "Expected 'Save' but got 'Publish'"),
])
def test_genuinely_different_errors_count_as_changed(a, b):
    assert es.normalize(a) != es.normalize(b)
    assert es.signatures_differ(a, b)


@pytest.mark.parametrize("a,b", [
    (None, "boom"), ("boom", None), ("", "boom"), ("boom", "   "), (None, None),
])
def test_unknown_error_is_never_reported_as_changed(a, b):
    """An unknown baseline error can't establish that the reason changed;
    treating it as changed would surface every pre-existing failure."""
    assert not es.signatures_differ(a, b)


def test_all_stack_frame_trace_does_not_normalize_to_empty():
    """A trace with no message line must keep its frames — otherwise it
    becomes an empty 'unknown' signature and silently compares equal."""
    trace = "\tat com.liferay.Foo.bar(Foo.java:42)\n\tat com.liferay.Baz.qux(Baz.java:7)"
    assert es.normalize(trace) != ""


def test_cluster_key_is_versioned_and_stable():
    k1 = es.cluster_key("modules/apps/Foo.java", "FooTest", "NPE at Foo.java:42")
    k2 = es.cluster_key("modules/apps/Foo.java", "FooTest", "NPE at Foo.java:99")
    assert k1 == k2, "volatile line number must not change the cluster key"
    assert k1.startswith(f"{es.SIGNATURE_VERSION}:")
    k3 = es.cluster_key("modules/apps/Bar.java", "FooTest", "NPE at Foo.java:42")
    assert k1 != k3


# --- classify_transition(): the §12 matrix --------------------------------

def test_transition_matrix():
    # New failures — the classic case
    assert classify_transition("PASSED", "FAILED", "", "boom") == TRANSITION_NEW
    assert classify_transition("PASSED", "BLOCKED", "", "boom") == TRANSITION_NEW
    assert classify_transition("PASSED", "UNTESTED", "", "") == TRANSITION_NEW

    # FAILED→FAILED hinges on the error signature, not the transition
    assert classify_transition("FAILED", "FAILED", "NPE at F.java:1",
                               "NPE at F.java:9") == TRANSITION_SAME_FAILURE
    assert classify_transition("FAILED", "FAILED", "NPE",
                               "IllegalState") == TRANSITION_CHANGED

    # Explicitly excluded buckets
    assert classify_transition("FAILED", "PASSED", "boom", "") == TRANSITION_FIXED
    assert classify_transition("FAILED", "BLOCKED", "boom", "x") == TRANSITION_AWARENESS

    # Best-effort rows
    assert classify_transition("BLOCKED", "FAILED", "", "boom") == TRANSITION_BLOCKED
    assert classify_transition("TESTFIX", "FAILED", "", "boom") == TRANSITION_TESTFIX

    # No baseline health signal
    assert classify_transition("UNTESTED", "FAILED", "", "boom") == TRANSITION_NO_BASELINE


def _frame(case_id, status, errors, **kw):
    row = {
        "case_id": case_id, "caseresult_id": case_id * 10,
        "case_name": f"Test{case_id}", "case_flaky": None,
        "component_name": "Comp", "team_name": None,
        "status": status, "errors": errors, "jira_issue": None,
        "subtask_id": kw.get("subtask_id", 0),
    }
    return row


def test_compute_test_diff_surfaces_new_and_changed_only():
    baseline = pd.DataFrame([
        _frame(1, "PASSED", ""),            # → new failure
        _frame(2, "FAILED", "NPE at A:1"),  # → changed (different error)
        _frame(3, "FAILED", "NPE at A:1"),  # → same failure, filtered
        _frame(4, "FAILED", "boom"),        # → fixed, filtered
        _frame(5, "BLOCKED", ""),           # → blocked→failed
        _frame(6, "FAILED", "boom"),        # → awareness only, filtered
    ])
    target = pd.DataFrame([
        _frame(1, "FAILED", "kaboom"),
        _frame(2, "FAILED", "IllegalStateException"),
        _frame(3, "FAILED", "NPE at A:57"),   # volatile-only delta
        _frame(4, "PASSED", ""),
        _frame(5, "FAILED", "kaboom"),
        _frame(6, "BLOCKED", "infra"),
    ])

    df, counts = compute_test_diff(baseline, target)

    got = dict(zip(df["testray_case_id"], df["transition"]))
    assert got == {1: TRANSITION_NEW, 2: TRANSITION_CHANGED,
                   5: TRANSITION_BLOCKED}
    assert counts[TRANSITION_SAME_FAILURE] == 1
    assert counts[TRANSITION_FIXED] == 1
    assert counts[TRANSITION_AWARENESS] == 1

    # §12: the baseline error must survive for changed failures.
    changed = df[df["transition"] == TRANSITION_CHANGED].iloc[0]
    assert changed["baseline_error_message"] == "NPE at A:1"
    assert changed["error_message"] == "IllegalStateException"


def test_passed_to_failed_only_would_undercount():
    """Guards decision #13: a baseline with pre-existing failures must not
    collapse to the PASSED→FAILED count."""
    baseline = pd.DataFrame([_frame(1, "PASSED", ""), _frame(2, "FAILED", "NPE")])
    target = pd.DataFrame([_frame(1, "FAILED", "x"), _frame(2, "FAILED", "TimeoutException")])
    df, _ = compute_test_diff(baseline, target)
    assert len(df) == 2, "changed failure was dropped — undercount regression"


def test_env_churn_is_not_a_changed_failure():
    """Two runs failing on the same infrastructure bucket with different text
    are one env problem, not a regression. Without this guard every flaky
    container id churns into a fake 'changed' row."""
    baseline = pd.DataFrame([_frame(
        1, "FAILED", "The build failed prior to running the test on agent-7")])
    target = pd.DataFrame([_frame(
        1, "FAILED", "The build failed prior to running the test on agent-42")])
    df, counts = compute_test_diff(baseline, target)
    assert counts[TRANSITION_SAME_FAILURE] == 1
    assert df.empty, "env churn must not reach the classifier"


def test_real_regression_still_surfaces_alongside_env_churn():
    """The guard must not swallow a genuine changed failure in the same run."""
    baseline = pd.DataFrame([_frame(1, "FAILED", "NullPointerException in Foo")])
    target = pd.DataFrame([_frame(1, "FAILED", "IllegalStateException in Foo")])
    df, _ = compute_test_diff(baseline, target)
    assert list(df["transition"]) == [TRANSITION_CHANGED]


# --- PR builds: deriving the fetch ref from the build name ----------------

def test_pr_build_name_yields_the_receivers_fork_and_pr_head():
    """A PR build's commit is on a fork, so origin cannot see it.

    Nothing on the Build object references the pull request — `description`
    carries only a Jenkins link and the portal SHA — so the name is the only
    source. The head ref lives on the repo the PR was opened *against* (the
    receiver), not the sender's.
    """
    from testray_analytics.analysis.prepare import pr_fetch_spec

    spec = pr_fetch_spec(
        '[master] ci:test:object - shuyangzhou > shuyangzhou '
        '- PR#12591 - 2026-08-19[11:47:41]')

    assert spec == ('git@github.com:shuyangzhou/liferay-portal.git',
                    'pull/12591/head')


def test_sender_and_receiver_are_not_interchangeable():
    """Guards the side that matters. Every PR build seen so far had
    sender == receiver, so a swap would have passed unnoticed."""
    from testray_analytics.analysis.prepare import pr_fetch_spec

    remote, ref = pr_fetch_spec('[master] job - alice > liferay - PR#42 - x')

    assert 'liferay/liferay-portal' in remote, remote
    assert 'alice' not in remote, remote
    assert ref == 'pull/42/head'


def test_non_pr_build_names_derive_nothing():
    """Release and Stable builds are on origin; inventing a fork fetch for them
    would make every ordinary run hit the network for no reason."""
    from testray_analytics.analysis.prepare import pr_fetch_spec

    for name in ('2026.q1.12-lts',
                 'EE Package Tester - 2024.q1.29 - 9 - 2026-07-21[09:25:31]',
                 '', None):
        assert pr_fetch_spec(name) is None, name


def test_pr_remote_template_is_configurable():
    from testray_analytics.analysis.prepare import pr_fetch_spec

    remote, _ = pr_fetch_spec(
        '[master] j - a > bob - PR#7 - x',
        {'git': {'pr_remote_template': 'https://example.test/{owner}/lp.git'}})

    assert remote == 'https://example.test/bob/lp.git'
