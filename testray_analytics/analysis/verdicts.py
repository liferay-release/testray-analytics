"""
verdicts.py — the verdict vocabulary, defined once.

Three consumers need to agree about what a row is *called* and how severe it
is: `report.py` (the CLI artifact), `submit.py`/`testray_writer.py` (which
store the per-verdict counts), and the analytics custom element (which reads
them back). They have already disagreed once — the Testray index would have
reported 100 `NEEDS_REVIEW` clusters for a run whose report showed 11, because
the stored counts used the raw classification and the report used the display
label. Any new consumer imports from here rather than restating the rule.

`util/verdict.ts` in the custom element mirrors this file. If you change the
order or the relabel rule, change it there too.
"""

import re

# Severity order. The index doubles as the sort rank, so the sequence is the
# contract, not just documentation: it decides which verdict a mixed cluster
# rolls up to, and what a "worst first" sort puts on top.
VERDICT_ORDER = ["BUG", "POSSIBLE_BUG", "NEEDS_REVIEW", "TEST_FIX",
                 "NOT_ATTRIBUTABLE",
                 "FALSE_POSITIVE", "ENV_FAILURE", "DID_NOT_RUN",
                 "AUTO_CLASSIFIED", "PENDING"]

# NOT_ATTRIBUTABLE is a DISPLAY label, never a stored verdict. A low-confidence
# NEEDS_REVIEW is the classifier saying "I could not attribute this", not "a
# human must review 153 failures" — and reporting the latter to a dev team
# misrepresents what was actually said. The stored classification stays
# NEEDS_REVIEW, so the picklist, the writer's schema and the rubric are
# untouched; only what a reader is shown changes.
#
# Two things the label must NOT swallow, both found on the U152 -> Acceptance
# run where it relabelled all 56 NEEDS_REVIEW clusters and the report read as
# "nothing to review":
#
#   * A row with NO confidence was never sent to the model at all — it carries
#     an auto label from submit._auto_label (a no_baseline row, say, whose own
#     reason reads "nothing to compare a signature against, so a human
#     decides"). Nothing failed to attribute it; nothing was asked. Only a
#     genuine `low` from the classifier relabels.
#   * A low-confidence verdict that still NAMED candidate tickets did attribute
#     something — it just could not choose between them. 50 of those 56
#     clusters named at least one. Only a verdict naming nothing is honestly
#     "not attributable".
UNATTRIBUTED_FROM = "NEEDS_REVIEW"
UNATTRIBUTED_AT = {"low"}

# Candidate tickets the classifier names in `specific_change` when it will not
# commit to one file. Defined here because the display rule above depends on
# it; report.py and submit.py import it rather than keeping their own copies.
CANDIDATE_RE = re.compile(r"\b((?:LPD|LPP|LPS)-\d+)\b")


# Liferay strips underscores from picklist entry keys, so a verdict written as
# NEEDS_REVIEW reads back as NEEDSREVIEW. Anything reading verdicts *out of*
# Testray (as opposed to out of the run bundle, which keeps the underscored
# form) has to canonicalise first, or the relabel rule below silently never
# matches. `util/verdict.ts` carries the same map.
CANONICAL = {
    "AUTOCLASSIFIED": "AUTO_CLASSIFIED",
    "DIDNOTRUN": "DID_NOT_RUN",
    "ENVFAILURE": "ENV_FAILURE",
    "FALSEPOSITIVE": "FALSE_POSITIVE",
    "NEEDSREVIEW": "NEEDS_REVIEW",
    "NOTATTRIBUTABLE": "NOT_ATTRIBUTABLE",
    "POSSIBLEBUG": "POSSIBLE_BUG",
    "TESTFIX": "TEST_FIX",
}


def canonical(verdict) -> str:
    """Underscored form of a verdict, whichever spelling arrives."""
    v = text(verdict)
    return CANONICAL.get(v, v)


def text(value) -> str:
    """Normalise a cell to a stripped string. NaN and None become ''."""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none") else s


def display_verdict(classification, confidence, specific_change="") -> str:
    """The label to SHOW for a row. See UNATTRIBUTED_FROM above.

    `specific_change` is read, not just the confidence: a low-confidence
    verdict that named candidate tickets is not unattributable.
    """
    cls = canonical(classification)
    if cls != UNATTRIBUTED_FROM:
        return cls
    if text(confidence).lower() not in UNATTRIBUTED_AT:
        return cls
    if CANDIDATE_RE.search(text(specific_change)):
        return cls
    return "NOT_ATTRIBUTABLE"


def rank(verdict) -> int:
    v = text(verdict)
    return VERDICT_ORDER.index(v) if v in VERDICT_ORDER else len(VERDICT_ORDER)


def rollup(verdicts) -> str:
    """The most severe verdict in a group — what a cluster header shows.

    A cluster is only as safe as its worst member: one BUG among thirty
    FALSE_POSITIVEs is still a BUG, and rolling up to the majority would hide
    exactly the row worth acting on. Returns "" when nothing is classified.
    """
    best, best_rank = "", len(VERDICT_ORDER)
    for v in verdicts:
        t = text(v)
        if not t:
            continue
        r = rank(t)
        if r < best_rank:
            best, best_rank = t, r
    return best


def display_series(df) -> list[str]:
    """Every row's display verdict, in frame order.

    Kept here so the renderer and the writer derive the column identically —
    the writer counting raw `classification` while the report counted the
    display label is precisely the bug this module exists to prevent.
    """
    if df is None or not len(df):
        return []
    n = len(df)
    return [
        display_verdict(c, f, s)
        for c, f, s in zip(df.get("classification", [""] * n),
                           df.get("confidence", [""] * n),
                           df.get("specific_change", [""] * n))
    ]
