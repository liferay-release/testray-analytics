"""
Tests for candidate-commit selection and the diffs inlined into prompt.md.

This exists because the release-master job runs `--engine api`, which has no
file-reading tool. Everything the classifier will ever see has to be IN the
prompt: `hunks.txt`, `git_diff_full.diff` and `commits/<sha>.diff` are all
unreachable there, so a prompt that only points at them ships a rubric the
engine cannot follow.

The case pinned throughout is Stable 19704, whose test name is a shard id
(`semantic-versioning/0/0`) that matches no path — so hunk matching yields
nothing and the error text is the only thing naming the module.
"""

import pathlib

from testray_analytics.analysis import prepare as P

STABLE_ERROR = (
    "[exec] Execution failed for task "
    "':apps:site:site-staticexport-api:baseline'. [exec] > Could not find any "
    "matches for com.liferay:com.liferay.site.staticexport.api:(,1.1.0)"
)

CULPRIT = {
    "hash": "00df34b2e8503", "author": "Victor Galan",
    "subject": "LPD-105774 Return the resources a site's pages reference",
    "modules": ["modules/apps/site/site-staticexport-api",
                "modules/apps/site/site-staticexport-impl"],
}
UNRELATED = {
    "hash": "deadbeef12345", "author": "Someone Else",
    "subject": "LPD-000000 Unrelated change",
    "modules": ["modules/apps/journal/journal-web"],
}


def test_the_module_name_is_recovered_from_the_error_text():
    """The whole point: the test name is a shard id, so the error text is the
    only signal that names what broke."""
    tokens = P.candidate_tokens([STABLE_ERROR, "semantic-versioning/0/0"])

    assert "site-staticexport-api" in tokens
    # The shard id tokenises too, and that is harmless: no commit path contains
    # it, so it scores nothing. Ranking is what discriminates, not extraction.
    assert "com" not in tokens, "bare words must not become tokens"


def test_the_commit_touching_the_named_module_is_the_candidate():
    ranked = P.rank_candidate_commits(
        [UNRELATED, CULPRIT], P.candidate_tokens([STABLE_ERROR]))

    assert [c["hash"] for c in ranked] == ["00df34b2e8503"]


def test_nothing_matching_yields_no_candidates():
    """An empty shortlist is a real answer — do not inline the whole range."""
    assert P.rank_candidate_commits([UNRELATED],
                                    P.candidate_tokens([STABLE_ERROR])) == []
    assert P.rank_candidate_commits([CULPRIT], set()) == []


def test_the_diff_is_inlined_and_the_budget_is_respected(tmp_path):
    """Prompt size is the bill: ~$8 per MB of prompt. A bulk commit must not
    take the run's budget with it."""
    (tmp_path / "commits").mkdir()
    big = "+" + ("x" * (P.CANDIDATE_DIFF_PER_COMMIT_CHARS + 5_000))
    (tmp_path / "commits" / "00df34b2e8503.diff").write_text(big)

    lines = P.render_candidate_diffs_section(tmp_path, [CULPRIT])
    body = "\n".join(lines)

    assert "## Candidate commits — diffs" in body
    assert "00df34b2e8503" in body
    assert "diff truncated" in body
    assert len(body) < P.CANDIDATE_DIFF_PER_COMMIT_CHARS + 5_000


def test_a_candidate_with_no_diff_on_disk_is_skipped(tmp_path):
    """Never fatal, and never a fabricated block."""
    assert P.render_candidate_diffs_section(tmp_path, [CULPRIT]) == []


def test_the_section_says_a_shortlist_is_not_an_accusation(tmp_path):
    """Listing a commit must not read as attributing to it — the prompt's own
    rule is that "nothing here explains it" is a legitimate verdict."""
    (tmp_path / "commits").mkdir()
    (tmp_path / "commits" / "00df34b2e8503.diff").write_text("+Bundle-Version: 1.1.0")

    body = "\n".join(P.render_candidate_diffs_section(tmp_path, [CULPRIT]))

    assert "not a claim that it caused anything" in body
    assert "Bundle-Version" in body
