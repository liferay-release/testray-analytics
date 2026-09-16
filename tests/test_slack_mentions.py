"""
Tests for @-mentioning the author of a culprit commit.

The rule these encode: trust the classifier for WHICH COMMIT, ask git WHO.
The classifier emits an `author` alongside each candidate, picked out of the
prompt's commit list — and on a ticket that groups several authors it picks the
wrong one. That was survivable while the name was prose. It is not once the
name becomes a notification: a wrong mention pings an uninvolved engineer about
a red build they had nothing to do with.
"""

from testray_analytics.analysis import slack_message as S

META = {"repo_slug": "brianchandotcom/liferay-portal"}
AUTHORS = {"00df34b": ("Victor Galan", "victor.galan@liferay.com")}


def _result(**kw):
    cand = {"commit": "00df34b", "ticket": "LPD-105774",
            "author": "Somebody Else", "why": "bumps the module version",
            "explains": True}
    cand.update(kw)
    return {"classification": "POSSIBLE_BUG", "confidence": "medium",
            "candidates": [cand]}


def test_a_liferay_address_becomes_a_mention():
    assert S._slack_mention("victor.galan@liferay.com") == "<@victor.galan>"


def test_an_outside_address_renders_raw():
    """No handle to guess. A visible email still carries the attribution; a
    fabricated <@...> would fail to link, or ping whoever does own it."""
    assert S._slack_mention("someone@gmail.com") == "someone@gmail.com"
    assert S._slack_mention("") == ""


def test_the_mention_comes_from_git_not_from_the_classifier():
    """The candidate says 'Somebody Else'; git says Victor Galan."""
    row = S._cause_row(META, _result(), AUTHORS)

    assert "<@victor.galan>" in row
    assert "Somebody Else" not in row


def test_a_closest_in_range_candidate_is_named_but_never_pinged():
    """`explains: false` is a lead, not an accusation. Notifying someone their
    commit is merely nearest to a build they did not break is how an alert
    gets muted."""
    row = S._cause_row(META, _result(explains=False), AUTHORS)

    assert "Closest in range" in row
    assert "<@" not in row
    assert "Victor Galan" in row, "still attributed, just not pinged"


def test_an_unresolved_commit_falls_back_to_the_classifier_text():
    """No checkout, no mention — but the row must still say something."""
    row = S._cause_row(META, _result(), {})

    assert "Somebody Else" in row
    assert "<@" not in row


def test_git_name_beats_the_classifier_name_even_without_a_mention():
    """An outside-domain author still gets the right person's name."""
    row = S._cause_row(META, _result(),
                       {"00df34b": ("Real Person", "real@contractor.example")})

    assert "real@contractor.example" in row
    assert "Somebody Else" not in row
