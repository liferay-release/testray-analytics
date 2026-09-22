"""
recurrence.py — "this is not new", for the Stable Slack message.

A build whose failures are all pre-existing has nothing to classify, so the run
produces no verdicts and the message used to say "no verdicts were produced —
needs a human". That is worse than silence: it sends someone to re-investigate
a failure that was explained days ago. This module answers the question the
message should be asking instead — *when did this error start, and in which
build* — so the reader can see at a glance that nothing changed.

**Answered from this routine's own build history, not from the verdict store.**
An earlier cut looked the signature up in `/o/c/triageresults` and reported
whatever it found, which on Stable meant inheriting a verdict recorded against
a 7.4.13-U153 *release* build — a different routine, a different branch, a
different commit range. For the control repo that is not a useful statement:
what a Stable reader needs is "this started two Stable builds ago, here is that
build". So the walk runs over the routine's own builds and never leaves it.

The walk is `ledger.SignatureIndex`'s, and deliberately so — the scanner
already decides what is new by walking predecessors, and a second, subtly
different notion of "first appeared" is how the diamond and the message start
disagreeing about the same build. Rule 3 in particular is not obvious and is
not re-derived here: a build that lacked the signature but never ran those
cases proves nothing, so the walk steps past it rather than stopping.

Stable only. On a release or acceptance routine a failure persisting across
builds is ordinary and the line would be noise.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .error_signature import cluster_key
from .verdicts import CANDIDATE_RE

# The control repo. See the module docstring for why this is not applied to
# every routine.
STABLE_ROUTINE_ID = 79529

# How far back the walk will look before giving up. Matches the scanner's own
# limit so the two cannot disagree about whether a signature has a beginning.
MAX_WALK = 40

# Builds considered as walk candidates. Larger than MAX_WALK so the target's
# own position in the list never truncates the walk.
WINDOW = 60


@dataclass
class Repeat:
    """A failure that this build inherited rather than introduced."""
    cluster_key:      str
    occurrences:      int          # builds carrying it, target included
    builds_ago:       int          # how many builds back it first appeared
    first_build_id:   int
    first_build_name: str = ""
    first_build_time: str = ""     # dueDate of that build, ISO-8601
    test_name:        str = ""
    error:            str = ""
    # The verdict recorded when this episode WAS analysed, if there is one.
    # Absent is normal and not an error: the write policy drops a cluster that
    # was auto-classified, flaky, pre-existing or never ran, so a repeat can
    # legitimately have no explanation on file.
    prior_verdict:    str = ""
    prior_reason:     str = ""
    prior_culprit:    str = ""
    # Jira keys named by that verdict. Carried separately from the prose so a
    # renderer can link the ticket without quoting the paragraph it sat in.
    prior_tickets:    tuple = ()


def signature_of(case_id, error) -> str:
    """The ledger's key for a failure — `cluster_key` with no culprit file.

    Matches `ledger.TestraySource.build_failures` exactly. It has to: the walk
    compares this against the signatures that function produced, and the two
    drifting apart is a silent "nothing is ever a repeat".
    """
    return cluster_key(None, f"case{int(case_id)}", error)


def prior_verdicts(cfg: dict) -> dict[tuple[int, int], dict]:
    """`(build_id, case_id) -> the TriageResult written for it`.

    Keyed off the ERC, which `testray_writer._erc` builds as
    `{build_id_b}_{case_id}_{classifier}` — so the pair falls straight out of
    a split, and the classifier suffix is ignored on purpose. A run submitted
    twice under `api:` and `agent:` labels is the same verdict about the same
    failure, and keying on it would return one arbitrarily.

    **This is deliberately not a clusterKey lookup.** The stored key comes from
    `cluster_key(culprit_file, test_case, error)` while the ledger computes
    `cluster_key(None, f"case{id}", errors)`, so any verdict that named a
    culprit file has a key the ledger can never reproduce — measured on prod:
    PortalLogAssertorTest stored `v3:e85e41e872b43d34` where the ledger says
    `v3:ca61ca98281c6797`. Build and case id are the two things both sides
    agree on, so the join goes through them and the mismatch cannot bite.

    Never fatal — an unreadable store means "no prior verdicts", which renders
    a repeat block without a cause rather than no message at all.
    """
    from .prepare import _testray_oauth_token, fetch_paginated

    try:
        rows = fetch_paginated(
            "/o/c/triageresults", {},
            token=_testray_oauth_token(cfg), base_url=cfg["base_url"],
            page_size=500,
        )
    except Exception:                                            # noqa: BLE001
        return {}

    out: dict[tuple[int, int], dict] = {}
    for row in rows:
        parts = (row.get("externalReferenceCode") or "").split("_")
        if len(parts) < 3:
            continue
        try:
            key = (int(parts[0]), int(parts[1]))
        except ValueError:
            continue
        out.setdefault(key, row)
    return out


def _tickets(row: dict, limit: int = 2) -> tuple:
    """The Jira keys a stored verdict names, best source first.

    `suspiciousCommits` is what `submit.annotate_culprit_commits` resolved from
    the commits that actually touched the culprit file, so a ticket found there
    is provably in the pair's range. The other two fields are model prose and
    can name a ticket the classifier merely mentioned — a fallback, not a peer,
    which is why the first field that yields anything wins outright.
    """
    out: list[str] = []
    for field in ("suspiciousCommits", "specificChange", "reason"):
        for key in CANDIDATE_RE.findall(str(row.get(field) or "")):
            if key not in out:
                out.append(key)
        if out:
            break
    return tuple(out[:limit])


def _picklist(value) -> str:
    """Liferay hands a picklist back as {"key": ..., "name": ...}."""
    if isinstance(value, dict):
        return str(value.get("key") or value.get("name") or "")
    return str(value or "")


def _build_ids(cfg: dict, routine_id: int) -> list[int]:
    from . import scan
    out = []
    for b in scan.recent_done_builds(cfg, routine_id, WINDOW):
        try:
            out.append(int(b["id"]) if isinstance(b, dict) else int(b))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _build_info(session, build_id: int) -> tuple[str, str]:
    """(name, dueDate). dueDate is when the build RAN, not when Testray
    imported it — the same field the scanner orders on, and the only one that
    makes "first seen 1h ago" true rather than an artefact of import lag."""
    try:
        body = session.request("GET", f"/o/c/builds/{build_id}")
        return str(body.get("name") or ""), str(body.get("dueDate") or "")
    except Exception:                                            # noqa: BLE001
        return "", ""


def lookup(cfg: dict, probes: dict, *, routine_id, target_build) -> dict:
    """`{id: (test_name, error, case_id)}` -> `{id: Repeat}`.

    Ids are the caller's own. A probe whose signature does not actually fail in
    the target build, or which has no earlier appearance, is simply absent from
    the result — "not a repeat" is a legitimate answer and the caller renders
    nothing for it.

    Never fatal. Every failure path returns "no repeats" so a message still
    goes out: the verdicts are the product, and this is context on top.
    """
    if not probes or target_build is None:
        return {}
    try:
        if int(routine_id) != STABLE_ROUTINE_ID:
            return {}
    except (TypeError, ValueError):
        return {}

    try:
        from .ledger import SignatureIndex, TestraySource
        from .testray_writer import _Session

        target_build = int(target_build)
        builds = _build_ids(cfg, int(routine_id))
        if target_build not in builds:
            return {}
        prior = builds[builds.index(target_build) + 1:]

        index = SignatureIndex(TestraySource(cfg, int(routine_id)))
        target_failures = index.failures(target_build)
        session = _Session(cfg)
        # One fetch for the whole message, not one per repeat.
        verdicts = prior_verdicts(cfg)
    except Exception:                                            # noqa: BLE001
        return {}

    out: dict[str, Repeat] = {}
    for probe_id, (test_name, error, case_id) in probes.items():
        try:
            sig = signature_of(case_id, error)
            members = target_failures.signatures.get(sig)
            if not members:
                continue               # not one of this build's failures

            carrying, first, steps = 1, target_build, 0
            for n, build_id in enumerate(prior[:MAX_WALK], start=1):
                failures = index.failures(build_id)
                if sig in failures.signatures:
                    carrying += 1
                    first, steps = build_id, n
                    continue
                if failures.ran(members):
                    break              # proven absence — the episode starts after
                # Lacked the signature but never ran the cases: proves nothing
                # (ledger rule 3). Keep walking without treating it as the edge.

            if steps == 0:
                continue               # first appears in THIS build — it is new

            # The verdict is looked up against the build where the episode
            # STARTED, because that is the build scan queues as the target: the
            # pair it registers is (baseline, first_seen), so the run that
            # explains an episode is always the one aimed at its first build.
            # NOT `prior`: that name holds the list of predecessor builds this
            # loop walks, and rebinding it here made every probe after the
            # first slice a dict, throw, and vanish into the except below — so
            # the section could only ever show one repeat.
            verdict_row = verdicts.get((first, int(case_id))) or {}
            name, when = _build_info(session, first)

            out[probe_id] = Repeat(
                cluster_key=sig,
                occurrences=carrying,
                builds_ago=steps,
                first_build_id=first,
                first_build_name=name,
                first_build_time=when,
                test_name=str(test_name or ""),
                error=str(error or ""),
                prior_verdict=_picklist(verdict_row.get("classification")),
                prior_reason=str(verdict_row.get("reason") or ""),
                prior_culprit=str(verdict_row.get("culpritFile") or ""),
                prior_tickets=_tickets(verdict_row),
            )
        except Exception:                                        # noqa: BLE001
            continue
    return out


def repeats_for_run(run_dir, meta: dict) -> dict:
    """The repeats for a finished bundle, keyed by case id.

    The one entry point. `submit` calls it ONCE and hands the result to both
    the report and the Slack message: it is a network call, and more
    importantly the two artifacts describing the same build must not disagree
    about when a failure started. Two independent lookups is how that drifts.

    Probes come from `diff_list.csv`, not `results.json`, because the build
    this exists for produced no verdicts — rows the write policy excluded still
    describe real failures, and they are exactly the ones a reader is being
    told are not new.

    Never fatal: every failure path returns "no repeats", and the caller simply
    renders nothing.
    """
    try:
        from .prepare import is_aggregate_row, load_config

        path = Path(run_dir) / "diff_list.csv"
        if not path.exists():
            return {}

        probes: dict[str, tuple[str, str, str]] = {}
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                case_id = str(row.get("testray_case_id") or "").strip()
                name    = str(row.get("test_case") or "").strip()
                if not case_id or is_aggregate_row(name):
                    continue
                probes[case_id] = (
                    name, str(row.get("error_message") or "").strip(), case_id)

        if not probes:
            return {}

        return lookup(load_config()["testray"], probes,
                      routine_id=meta.get("routine_id"),
                      target_build=meta.get("build_id_b"))
    except Exception:                                            # noqa: BLE001
        return {}


def _case_name(session, case_id) -> str:
    """Display name for a case id, or "" — one GET, failures are not fatal."""
    try:
        return str((session.request("GET", f"/o/c/cases/{int(case_id)}")
                    or {}).get("name") or "")
    except Exception:                                            # noqa: BLE001
        return ""


def repeats_for_build(cfg: dict, *, routine_id, build_id) -> dict:
    """The repeats for a build NO run was produced for, keyed by case id.

    The sibling of `repeats_for_run`, and the reason it exists: a tick whose
    every red pair is already analysed queues nothing, so `watch` runs no
    pipeline, `submit` never executes and there is no bundle to read probes
    from. That is precisely the tick where the channel most needs a message —
    Stable posts on every failure, and silence there reads as "the analyser
    missed it" rather than "this is the failure you were already told about".

    Probes are built from `ledger`'s OWN signature index rather than from the
    case results directly. Two reasons, both learned the hard way:

      * the ledger signs only `FAILED` rows, so probing every non-PASSED row
        produced signatures the walk had never seen and matched nothing;
      * one probe per SIGNATURE, not per case result, keeps the name lookups
        to a handful on a build where 467 rows share one error.
    """
    try:
        from .ledger import SignatureIndex, TestraySource
        from .prepare import fetch_build_caseresults_api, is_aggregate_row
        from .testray_writer import _Session

        build_id = int(build_id)
        index = SignatureIndex(TestraySource(cfg, int(routine_id)))
        failures = index.failures(build_id)
        if not failures.signatures:
            return {}

        df = fetch_build_caseresults_api(build_id, cfg)
        if df is None or df.empty:
            return {}
        errors = {int(r["case_id"]): str(r.get("errors") or "").strip()
                  for _, r in df.iterrows() if r.get("case_id") is not None}

        session = _Session(cfg)
        probes: dict[str, tuple[str, str, str]] = {}
        for members in failures.signatures.values():
            if not members:
                continue
            case_id = int(members[0])
            name = _case_name(session, case_id)
            # Testray's per-build row is not a test: it carries no error, so it
            # would recur forever while naming nothing a reader can act on.
            # Belt and braces now — `ledger.TestraySource` drops it before a
            # signature is ever made — and kept because this reads names and
            # that one reads ids.
            if is_aggregate_row(name):
                continue
            probes[str(case_id)] = (name or f"case{case_id}",
                                    errors.get(case_id, ""), str(case_id))

        if not probes:
            return {}

        return lookup(cfg, probes, routine_id=routine_id,
                      target_build=build_id)
    except Exception:                                            # noqa: BLE001
        # Same contract as repeats_for_run: context is never worth failing a
        # scan over. No repeats means no section, not no message.
        return {}


def triage_url_for(meta: dict, build_id) -> str:
    """Testray triage view for an ARBITRARY build — the run that explains a
    repeat, not this one. `testray_url` carries the site path, which differs
    per instance (`/web/testray` on prod, `/web/liferay-testray` locally), so
    it cannot be derived any other way."""
    base = str(meta.get("testray_url") or "").strip()
    if not (base and build_id):
        return ""
    return f"{base.rstrip('/')}/triage?buildId={build_id}"
