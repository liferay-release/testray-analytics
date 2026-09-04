"""
ledger.py — signature history, read from Testray. No local state.

The durable entity is the **failure signature**, not the build pair. What the
pipeline needs to know about one is: has it been explained already, when did it
first appear, and which commits should be blamed. All three are answerable from
Testray, so nothing is stored on disk:

    has it been explained?   TriageResult filtered by `clusterKey`
    when did it appear?      the oldest such row (`sort=dateCreated:asc`)
    which commits?           `gitHashA` / `gitHashB`, already on that row
    what is red right now?   the target build's own case results

That keeps ARCHITECTURE decision #11 intact — Testray is the single sink — and
means there is no cache to place, back up, or lose. An earlier version of this
module kept an append-only JSONL of observations; it worked, but it was a second
copy of data Testray already holds, and it needed a durable path nobody had.

**Bounded work, not full history.** Rebuilding every signature's story on each
tick would be minutes of REST. It is never necessary: a tick only needs the
target build's signatures, plus — for each *unexplained* one — a walk back far
enough to find a build that lacked it. That walk is one build in the common case
(median gap 2.2h on Stable) and is capped so a pathological case cannot run away.

The three decisions this implements:

  D1  A signature's attribution range runs from the last build where THAT
      signature was absent to the build where it appeared. Usually N-1; wider
      only to cover an observation gap. Never "the last green build" — a build
      can be red with unrelated failures and still be the right baseline.

  D2  Attribute once, on entry to ACTIVE. A recurring failure's cause has left
      the current range, so re-asking against today's diff can only answer
      "nothing in range".

  D4  Absence proves a fix only when the cases actually ran. Every Stable build
      carries ~199 UNTESTED rows, so a signature vanishing is routinely
      meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .error_signature import cluster_key

# Statuses meaning "no verdict this build" — absence here proves nothing (D4).
NOT_RUN_STATUSES = ("UNTESTED", "DID_NOT_RUN", "DIDNOTRUN", "BLOCKED", "INCOMPLETE")

# How far back a baseline search will walk before giving up. Generous against
# the measured worst case (20 builds), but finite: without a cap, a signature
# present in every build we can see would walk the entire routine's history on
# every tick.
MAX_BASELINE_WALK = 40

STATE_NEW       = "NEW"
STATE_ACTIVE    = "ACTIVE"
STATE_RESOLVED  = "RESOLVED"
STATE_REGRESSED = "REGRESSED"


def _status(item: dict) -> str:
    v = item.get("dueStatus")
    if isinstance(v, dict):
        v = v.get("key")
    return str(v or "").strip().upper()


@dataclass
class BuildFailures:
    """One build's failure picture: what failed, what never ran, and what the
    build contained at all."""
    build_id:   int
    signatures: dict[str, list[int]] = field(default_factory=dict)
    not_run:    set[int]             = field(default_factory=set)
    present:    set[int]             = field(default_factory=set)

    def ran(self, case_ids) -> bool:
        """True when at least one of `case_ids` produced a verdict here.

        Presence is checked first, and that is not a formality: a case ADDED
        since the baseline is absent from it entirely, so "not in the not-run
        set" was trivially true and absence read as "ran and passed". The
        scanner then queued a range whose baseline predated the test's
        existence, and `prepare` — which classes that `no_baseline` — refused
        the work after paying for the run.

        "Any" rather than "all" is still deliberate: a signature covering 30
        cases is proven gone if even one of them ran without reproducing it,
        whereas requiring all 30 would let one permanently-skipped case block
        resolution forever.
        """
        return any(c in self.present and c not in self.not_run for c in case_ids)


@dataclass
class Attribution:
    """An existing verdict for a signature, as recorded on a TriageResult."""
    cluster_key: str
    git_hash_a:  str | None
    git_hash_b:  str | None
    date:        str
    classification: str | None = None


@dataclass
class SignatureState:
    cluster_key: str
    state:       str
    case_ids:    list[int] = field(default_factory=list)
    baseline_build: int | None = None
    target_build:   int | None = None
    prior:       Attribution | None = None

    @property
    def needs_attribution(self) -> bool:
        return self.state in (STATE_NEW, STATE_REGRESSED)


# ---------------------------------------------------------------------------
# The Testray-backed index
# ---------------------------------------------------------------------------

class SignatureIndex:
    """Answers signature questions against Testray, caching within one run.

    `source` is injected so the logic is testable without a server; the default
    is the live REST reader below. Nothing here writes — the only writer is
    `submit`, into TriageResult, unchanged.
    """

    def __init__(self, source):
        self.source = source
        self._failures: dict[int, BuildFailures] = {}
        self._attributions: dict[str, Attribution] | None = None

    # -- cached reads -------------------------------------------------------

    def failures(self, build_id: int) -> BuildFailures:
        """A build's failure picture, fetched at most once per run.

        The cache is what keeps a baseline walk cheap: consecutive signatures in
        the same build re-examine the same handful of predecessors, and without
        it each would re-fetch them.
        """
        if build_id not in self._failures:
            self._failures[build_id] = self.source.build_failures(build_id)
        return self._failures[build_id]

    def attributions(self) -> dict[str, Attribution]:
        """cluster_key -> the earliest verdict recorded for it.

        Earliest, not latest: the first verdict is the one judged against the
        range where the signature actually appeared (D2). A later row for the
        same signature is the same verdict fanned onto another build's case
        results, and its gitHashA/B describe that later range, not the cause.
        """
        if self._attributions is None:
            self._attributions = self.source.attributions()
        return self._attributions

    # -- the questions that matter -----------------------------------------

    def classify_build(self, build_id: int, recent_builds: list[int]) -> list[SignatureState]:
        """State of every signature failing in `build_id`.

        `recent_builds` is that build's predecessors, newest first — the search
        space for a baseline. Supplying it rather than fetching it here keeps
        one build-list request per tick instead of one per signature.
        """
        known = self.attributions()
        out: list[SignatureState] = []
        for sig, members in self.failures(build_id).signatures.items():
            prior = known.get(sig)
            if prior is None:
                state = STATE_NEW
            elif self._resolved_between(sig, members, prior, recent_builds):
                # Seen before, but there is a build since that verdict where the
                # cases ran clean. It came back, so its cause is not the one
                # already on file and the old verdict must not be reused.
                state = STATE_REGRESSED
            else:
                out.append(SignatureState(cluster_key=sig, state=STATE_ACTIVE,
                                          case_ids=list(members), prior=prior))
                continue
            rng = self.find_range(sig, members, build_id, recent_builds)
            baseline, first_seen = rng if rng else (None, build_id)
            out.append(SignatureState(
                cluster_key=sig, state=state, case_ids=list(members),
                baseline_build=baseline, target_build=first_seen, prior=prior))
        return out

    def find_range(self, cluster_key: str, members, target_build: int,
                   recent_builds: list[int]) -> tuple[int, int] | None:
        """D1: (last build without this signature, build where it first appeared).

        The walk must pass THROUGH predecessors that still carry the signature —
        they are the same episode continuing — and stop at the first build that
        genuinely lacked it. Stopping at the first build that still had it was a
        bug: a signature failing for four builds would report "no baseline" and
        never be attributed, even though the build before its debut was sitting
        one step further back.

        The second element matters as much as the first. Attribution runs
        against the range that ends where the signature APPEARED, not where we
        happen to be looking from — otherwise a failure noticed four builds late
        gets blamed on four builds of unrelated commits.

        A build that lacked the signature but never ran its cases proves nothing
        (D4), so the walk continues through those without treating them as the
        boundary.

        None when no genuine absence is found within the walk limit — a real
        answer, since attributing without a baseline means inventing a range.
        """
        first_seen = target_build
        for build_id in recent_builds[:MAX_BASELINE_WALK]:
            f = self.failures(build_id)
            if cluster_key in f.signatures:
                first_seen = build_id          # episode reaches further back
                continue
            if f.ran(members):
                return build_id, first_seen
        return None

    def _resolved_between(self, cluster_key: str, members,
                          prior: Attribution, recent_builds: list[int]) -> bool:
        """Was there a clean run between the recorded verdict and now?"""
        for build_id in recent_builds[:MAX_BASELINE_WALK]:
            f = self.failures(build_id)
            if cluster_key in f.signatures:
                return False         # unbroken since the verdict: still ACTIVE
            if f.ran(members):
                return True
        return False


# ---------------------------------------------------------------------------
# Live Testray source
# ---------------------------------------------------------------------------

class TestraySource:
    """The real reader. Two endpoints, no writes."""

    def __init__(self, cfg: dict, routine_id: int):
        self.cfg = cfg
        self.routine_id = routine_id

    def _token(self):
        from .prepare import _testray_oauth_token
        return _testray_oauth_token(self.cfg)

    def build_failures(self, build_id: int) -> BuildFailures:
        from .prepare import fetch_paginated
        items = fetch_paginated(
            "/o/c/caseresults",
            {"filter": f"r_buildToCaseResult_c_buildId eq '{build_id}'",
             "fields": "dueStatus,errors,r_caseToCaseResult_c_caseId"},
            token=self._token(), base_url=self.cfg["base_url"],
            progress_label=f"caseresults build {build_id}",
        )
        sigs: dict[str, list[int]] = {}
        not_run: set[int] = set()
        for it in items:
            cid = it.get("r_caseToCaseResult_c_caseId")
            if cid is None:
                continue
            cid = int(cid)
            st = _status(it)
            if st in NOT_RUN_STATUSES:
                not_run.add(cid)
            elif st == "FAILED":
                sig = cluster_key(None, f"case{cid}", it.get("errors") or "")
                sigs.setdefault(sig, []).append(cid)
        present = {int(it["r_caseToCaseResult_c_caseId"]) for it in items
                   if it.get("r_caseToCaseResult_c_caseId") is not None}
        return BuildFailures(build_id=build_id, signatures=sigs,
                             not_run=not_run, present=present)

    def attributions(self) -> dict[str, Attribution]:
        """Earliest TriageResult per clusterKey.

        One pass sorted oldest-first, keeping the first row seen per key — far
        cheaper than a per-signature query, and the ordering makes "first wins"
        equivalent to "earliest".
        """
        from .prepare import fetch_paginated
        items = fetch_paginated(
            "/o/c/triageresults",
            {"fields": "clusterKey,gitHashA,gitHashB,dateCreated,classification",
             "sort": "dateCreated:asc"},
            token=self._token(), base_url=self.cfg["base_url"],
            progress_label="triageresults",
        )
        out: dict[str, Attribution] = {}
        for it in items:
            ck = (it.get("clusterKey") or "").strip()
            if not ck or ck in out:
                continue
            cls = it.get("classification")
            if isinstance(cls, dict):
                cls = cls.get("key")
            out[ck] = Attribution(
                cluster_key=ck, git_hash_a=it.get("gitHashA"),
                git_hash_b=it.get("gitHashB"),
                date=str(it.get("dateCreated") or ""), classification=cls)
        return out
