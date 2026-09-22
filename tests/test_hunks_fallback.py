"""
`hunks.txt` must never come back empty and call it a success.

There are two ways to end up with no filtered diff, and only one was handled.
No fragments at all (api x api, neither side carrying a case name) already fell
back to the full diff. Fragments that match no FILE did not: the extractor
logged "3 fragment(s) matched nothing", the step then printed
"3 fragments → hunks.txt" as though it had worked, and the classifier was
handed a zero-byte file.

That is the normal outcome on a Poshi routine, not an edge case — its
fragments are test class names (`DatabasePartitioning.java`) that appear
nowhere in a product diff. Pinned from ci:test:db-partition builds 389→390,
where hunks.txt was 0 bytes, the candidate-commit section was also empty, and
the model — with no diff text of any kind — attributed the failure to a commit
that merely had the word "audit" in its subject.
"""

import pandas as pd

from testray_analytics.analysis import prepare as P

DIFF = """diff --git a/modules/apps/journal/journal-web/src/Foo.java b/modules/apps/journal/journal-web/src/Foo.java
index 1111111..2222222 100644
--- a/modules/apps/journal/journal-web/src/Foo.java
+++ b/modules/apps/journal/journal-web/src/Foo.java
@@ -1,3 +1,3 @@
-int x = 1;
+int x = 2;
"""


def _paths(tmp_path):
    diff = tmp_path / "git_diff_full.diff"
    diff.write_text(DIFF)
    return diff, tmp_path / "test_fragments.txt", tmp_path / "hunks.txt"


def test_fragments_that_match_nothing_fall_back_to_the_full_diff(tmp_path):
    diff, frags, hunks = _paths(tmp_path)
    frags.write_text("DatabasePartitioning.java\nLocalFile\n")

    warning = P.write_hunks(diff, {"DatabasePartitioning.java", "LocalFile"},
                            frags, hunks)

    assert hunks.read_text() == DIFF
    assert warning and "matched no file" in warning


def test_no_fragments_at_all_still_falls_back(tmp_path):
    """The path that already worked — kept so the refactor cannot lose it."""
    diff, frags, hunks = _paths(tmp_path)
    frags.write_text("")

    warning = P.write_hunks(diff, set(), frags, hunks)

    assert hunks.read_text() == DIFF
    assert warning and "lack case_name" in warning


def test_a_matching_fragment_is_left_filtered_and_warns_about_nothing(tmp_path):
    """The fallback must not fire when filtering worked: replacing a narrow
    hunks.txt with the whole diff is what the filter exists to avoid."""
    diff, frags, hunks = _paths(tmp_path)
    frags.write_text("journal-web\n")

    warning = P.write_hunks(diff, {"journal-web"}, frags, hunks)

    assert warning is None
    assert hunks.stat().st_size
    assert "journal-web" in hunks.read_text()


def test_stable_shard_ids_keep_taking_the_no_fragment_path(tmp_path):
    """Stable's test names are shard ids and gradle task paths, so it derives
    no fragments and has always taken the full-diff branch — 14 of its 20
    bundles have hunks.txt byte-identical to git_diff_full.diff. It must keep
    doing exactly that: this change is for the routines that derive fragments
    and match nothing, and Stable is not one of them."""
    fragments = P.derive_test_fragments(pd.DataFrame([
        {"test_case": "semantic-versioning/0/0", "component_name": "Stable"},
    ]))

    assert fragments == set()

    diff, frags, hunks = _paths(tmp_path)
    frags.write_text("")
    warning = P.write_hunks(diff, fragments, frags, hunks)

    assert hunks.read_text() == DIFF
    assert "lack case_name" in warning
