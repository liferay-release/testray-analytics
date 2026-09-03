"""
submit.py — consume a classification bundle (prepare.py + classify.py, or a
dev's own Claude Code session), validate results.json, render a slim report,
and hand the verdicts to the Testray writer.

Two bundle modes — selected by `mode:` in run.yml:

- **per-test** (default): one results.json entry per testray_case_id.
- **by-subtask**: one entry per Testray Subtask with a `case_ids: [...]`
  array; the verdict is fanned out to each member case.

This step is deliberately provenance-blind: results.json is the same shape
whether it came from a Claude Code session (`classifier: agent:<model>`), the
Anthropic API via classify.py (`api:<model>`), or a local generator used to
exercise the write path without spend (`synthetic:<label>`). The classifier
string is the only record of which, and it rides in the ERC — so runs from
different classifiers coexist instead of overwriting each other. See the
CLASSIFIER SWITCH note in classify.py.

The writer (testray_writer.py) builds the `/o/c/triageresults` payload keyed by
externalReferenceCode = `<buildB>_<caseId>_<classifier>` and upserts it into
Testray over headless REST (LPD-95843). The payload is also written to the run
dir as `triageresults_batch.json` for inspection; `--dry-run` stops there.

Usage:
    testray-analysis submit runs/r_<id>
    testray-analysis submit runs/r_<id> --dry-run   # build payload, don't post
    testray-analysis submit runs/r_<id> --no-write  # report only
"""

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd
import yaml

from . import verdicts
from .jira_settings import resolve_jira_settings
from .prepare import commits_touching_file, load_config, testray_target
from .report import render_run
from .testray_writer import (
    ENDPOINT, FK_FIELD, build_batch, build_triage_run, count_excluded,
    excluded_breakdown, format_exclusions, post_batch, write_batch_file,
    write_triage_run,
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_CLASSIFICATIONS = {"BUG", "POSSIBLE_BUG", "NEEDS_REVIEW", "FALSE_POSITIVE", "TEST_FIX"}
_CONFIDENCES     = {"high", "medium", "low"}

# The candidate-ticket pattern lives in verdicts.py, because the display
# rule depends on it: three copies had already drifted apart in wording.
_TICKET_RE = verdicts.CANDIDATE_RE

# Phrases that assert the PRODUCT misbehaved, as opposed to a test being flaky.
# Deliberately narrow: a false positive here understates a real drift signal,
# and this only ever prints a NOTE, never changes a verdict.
_MISBEHAVIOUR_RE = re.compile(
    r"\b(?:HTTP\s*5\d\d|\b5\d\d\s+(?:error|response)|probably a defect|"
    r"is a defect|never shows|no longer shows|not reflected|should (?:be|exist|show)|"
    r"incorrect|wrong value|returns? (?:an? )?error|stack trace)\b", re.I)

MODE_PER_TEST   = "per-test"
MODE_BY_SUBTASK = "by-subtask"
MODE_BY_CLUSTER = "by-cluster"
# Modes that classify a group once and fan the verdict out to its members.
GROUPED_MODES   = (MODE_BY_CLUSTER, MODE_BY_SUBTASK)


def validate_results(payload: dict, expected_case_ids: set[int]) -> list[dict]:
    """
    Validate results.json against the run's diff_list. Returns the
    results list on success; raises SystemExit on any violation with a
    human-readable message.
    """
    errs: list[str] = []

    for key in ("run_id", "classifier", "results"):
        if key not in payload:
            errs.append(f"missing top-level key: {key!r}")
    if errs:
        _fail(errs)

    results = payload["results"]
    if not isinstance(results, list):
        _fail([f"`results` must be a list, got {type(results).__name__}"])

    seen_ids: set[int] = set()
    for i, r in enumerate(results):
        prefix = f"results[{i}]"
        if not isinstance(r, dict):
            errs.append(f"{prefix} must be an object")
            continue
        cid = r.get("testray_case_id")
        if not isinstance(cid, int):
            errs.append(f"{prefix}.testray_case_id must be an int")
        elif cid in seen_ids:
            errs.append(f"{prefix} duplicate testray_case_id={cid}")
        else:
            seen_ids.add(cid)
            if cid not in expected_case_ids:
                errs.append(f"{prefix} testray_case_id={cid} not in diff_list.csv "
                            f"(pre-classified or flaky cases must not appear)")

        cls = r.get("classification")
        if cls not in _CLASSIFICATIONS:
            errs.append(f"{prefix}.classification must be one of {_CLASSIFICATIONS}, got {cls!r}")
        conf = r.get("confidence")
        if conf not in _CONFIDENCES:
            errs.append(f"{prefix}.confidence must be one of {_CONFIDENCES}, got {conf!r}")
        if not isinstance(r.get("reason"), str) or not r["reason"].strip():
            errs.append(f"{prefix}.reason must be a non-empty string")

        culprit = r.get("culprit_file")
        if cls == "BUG":
            if not isinstance(culprit, str) or not culprit.strip():
                errs.append(f"{prefix} classification=BUG requires non-empty culprit_file")
        elif culprit is not None and not isinstance(culprit, str):
            errs.append(f"{prefix}.culprit_file must be string or null")

        specific = r.get("specific_change")
        if specific is not None and not isinstance(specific, str):
            errs.append(f"{prefix}.specific_change must be string or null")

    if errs:
        _fail(errs)

    missing = expected_case_ids - seen_ids
    if missing:
        print(f"WARNING: {len(missing)} case(s) in diff_list.csv have no entry "
              f"in results.json — they will not be persisted: "
              f"{sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}",
              file=sys.stderr)

    return results


def _fail(errs: list[str]) -> None:
    print("results.json validation failed:", file=sys.stderr)
    for e in errs:
        print(f"  - {e}", file=sys.stderr)
    raise SystemExit(1)


def validate_results_subtask(payload: dict, expected_case_ids: set[int],
                              all_diff_case_ids: set[int],
                              canonical_members: dict[int, list[int]] | None = None,
                              ) -> list[dict]:
    """Validate subtask-mode results.json. One entry per subtask, each with
    a case_ids array.

    A subtask block in prompt.md may show mixed members (some classifiable,
    some pre-classified, some flaky) — the model only sees the subtask as
    a whole. Validation rules:

    - subtask_id is unique per entry (when integer; null is allowed once
      per unmapped singleton).
    - Every case_id named must be in `all_diff_case_ids` (i.e. is in the
      bundle at all). Case_ids the model fabricates are an error.
    - case_ids that are pre-classified or flaky are ALLOWED in case_ids
      arrays — they're ignored at fan-out time (auto wins; flaky is dropped).
      We just warn so the operator notices a mismatch.
    - No classifiable case_id may appear twice across the whole payload —
      a case can only inherit one verdict.
    """
    errs: list[str] = []

    for key in ("run_id", "classifier", "results"):
        if key not in payload:
            errs.append(f"missing top-level key: {key!r}")
    if errs:
        _fail(errs)

    results = payload["results"]
    if not isinstance(results, list):
        _fail([f"`results` must be a list, got {type(results).__name__}"])

    seen_classifiable_cids: set[int] = set()
    seen_subtasks: set[int] = set()
    nonclassifiable_in_results = 0
    for i, r in enumerate(results):
        prefix = f"results[{i}]"
        if not isinstance(r, dict):
            errs.append(f"{prefix} must be an object")
            continue

        # by-cluster identifies a group with group_id; by-subtask bundles
        # written before group_id existed use subtask_id. The identifier is
        # what the verdict is fanned out on, so a duplicate or a wrong type
        # here silently mis-assigns a whole group's members.
        id_field = "group_id" if r.get("group_id") is not None else "subtask_id"
        sid = r.get(id_field)
        if sid is not None and not isinstance(sid, int):
            errs.append(f"{prefix}.{id_field} must be int or null, "
                        f"got {type(sid).__name__}")
        elif isinstance(sid, int):
            if sid in seen_subtasks:
                errs.append(f"{prefix} duplicate {id_field}={sid}")
            else:
                seen_subtasks.add(sid)

        case_ids = r.get("case_ids")
        if not isinstance(case_ids, list) or not case_ids:
            errs.append(f"{prefix}.case_ids must be a non-empty array")
            continue
        for j, cid in enumerate(case_ids):
            if not isinstance(cid, int):
                errs.append(f"{prefix}.case_ids[{j}] must be int, got {type(cid).__name__}")
                continue
            if cid not in all_diff_case_ids:
                errs.append(f"{prefix}.case_ids[{j}] case_id={cid} not in diff_list.csv "
                            f"(model fabricated a case_id)")
                continue
            if cid in expected_case_ids:
                if cid in seen_classifiable_cids:
                    errs.append(f"{prefix}.case_ids[{j}] case_id={cid} appears in another result entry "
                                f"— each classifiable case may inherit only one verdict")
                    continue
                seen_classifiable_cids.add(cid)
            else:
                # pre-classified or flaky — model included it harmlessly
                nonclassifiable_in_results += 1

        cls = r.get("classification")
        if cls not in _CLASSIFICATIONS:
            errs.append(f"{prefix}.classification must be one of {_CLASSIFICATIONS}, got {cls!r}")
        conf = r.get("confidence")
        if conf not in _CONFIDENCES:
            errs.append(f"{prefix}.confidence must be one of {_CONFIDENCES}, got {conf!r}")
        if not isinstance(r.get("reason"), str) or not r["reason"].strip():
            errs.append(f"{prefix}.reason must be a non-empty string")

        culprit = r.get("culprit_file")
        if cls == "BUG":
            if not isinstance(culprit, str) or not culprit.strip():
                errs.append(f"{prefix} classification=BUG requires non-empty culprit_file")
        elif culprit is not None and not isinstance(culprit, str):
            errs.append(f"{prefix}.culprit_file must be string or null")

        specific = r.get("specific_change")
        if specific is not None and not isinstance(specific, str):
            errs.append(f"{prefix}.specific_change must be string or null")

    if errs:
        _fail(errs)

    if nonclassifiable_in_results:
        print(f"NOTE: {nonclassifiable_in_results} case_id(s) in results.json are "
              f"pre-classified or flaky — they were included in subtask blocks "
              f"for context but will not inherit the model's verdict (auto wins; "
              f"flaky drops).", file=sys.stderr)

    # Coverage must be judged against CANONICAL membership, not the case_ids
    # the model echoed. The prompt truncates long member lists ("+90 more"),
    # so the echo is always short for big groups — measuring against it
    # reported 129 of 449 cases as unclassified on a run where the fan-out
    # actually covered every one of them. That false alarm is worse than no
    # warning: it invites deleting a good run.
    covered = set(seen_classifiable_cids)
    if canonical_members:
        for gid in seen_subtasks:
            covered.update(canonical_members.get(gid, ()))
    missing = expected_case_ids - covered
    if missing:
        print(f"WARNING: {len(missing)} classifiable case(s) in diff_list.csv "
              f"are not covered by any group — they will default "
              f"to NEEDS_REVIEW: "
              f"{sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}",
              file=sys.stderr)

    return results


# ---------------------------------------------------------------------------
# Assembly — combine agent results + auto/env buckets + flaky-excluded
# ---------------------------------------------------------------------------

# Pre-classified env/infra rows map to human-readable, non-actionable labels
# (not real LLM verdicts, never diff-analyzed): the build broke / nothing ran
# vs. an environment failure. The specific pre_classification (BUILD_FAILURE,
# ENV_CHROME, …) is kept in `reason`.
# Pre-classifications that mean "this test never produced a real result", as
# opposed to "the environment broke it". BATCH_FAILURE is a CI-batch row —
# infrastructure, not a test — so there is nothing that DID run.
_DID_NOT_RUN = {"BUILD_FAILURE", "NO_ERROR", "BATCH_FAILURE"}

# NO_BASELINE is neither: the test ran and genuinely failed, there is simply no
# baseline result to compare it against — most often because the test is NEW.
# A new test that fails is the most interesting row in the run, not the least,
# so prepare keeps it in triage and it surfaces here as NEEDS_REVIEW. That is
# an existing verdict, so verdicts.py, the Testray picklist and util/verdict.ts
# are all untouched.
_NO_BASELINE = "NO_BASELINE"

# PRE_EXISTING is a third case: the test ran and genuinely failed, it just was
# not failing because of THIS diff — it had not passed for several builds
# before it. FALSE_POSITIVE is the existing verdict for "real failure, not
# caused by the change", so this needs no new entry either.
_PRE_EXISTING = "PRE_EXISTING"


def _auto_label(pre_classification) -> str:
    pc = str(pre_classification or "").strip().upper()
    if pc == _NO_BASELINE:
        return "NEEDS_REVIEW"
    if pc == _PRE_EXISTING:
        return "FALSE_POSITIVE"
    return "DID_NOT_RUN" if pc in _DID_NOT_RUN else "ENV_FAILURE"


# A known-flaky row is never sent to the classifier, so it has no verdict of
# its own. It still belongs on the report: a failure the reader cannot see is
# a failure they cannot judge, and the header count already includes it (the
# status matrix is built before this filter), so dropping the row left a number
# on the page that no row explained.
#
# NEEDS_REVIEW is the honest label — nothing was asked of the classifier, so
# nothing was concluded. Flakiness rides alongside as `known_flaky`, which
# report.py renders as a badge next to the pill: the verdict says what the
# failure is, the badge says how much to trust the signal. They are different
# questions and neither answer should overwrite the other.
_FLAKY_REASON = ("marked flaky on the Testray case — excluded from "
                 "classification, shown for awareness")


def _is_flaky(row) -> bool:
    v = row.get("known_flaky")
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return False
    return str(v).strip().lower() in ("true", "1", "1.0", "yes")


def _auto_reason(row) -> str:
    """Why an auto-classified row got its label.

    `pre_classification=X` is enough for an env or infra row, where the label
    IS the explanation. A no-baseline row needs more: NEEDS_REVIEW with no
    reason reads as the classifier giving up, when in fact nothing was asked
    of it. And for a pre-existing failure the streak IS the evidence, so it
    belongs in the reason a human reads rather than only in a CSV column.
    """
    pc = str(row.get("pre_classification") or "").strip().upper()
    if pc == _NO_BASELINE:
        # Say what the reader should DO with it. "No baseline, a human decides"
        # is true but reads as a shrug, and the commonest cause carries a clear
        # expectation: a test added in this range is supposed to pass in the
        # build that adds it, so one failing on its first run is unfinished
        # work rather than an unknown.
        return ("no baseline result for this case — most often a newly added "
                "test. A new test is expected to pass on the build that "
                "introduces it, so a failure on its first run means the test "
                "or the change it covers is not finished. There is no earlier "
                "signature to compare against, so a human decides")
    if pc == _PRE_EXISTING:
        streak = row.get("history_fail_streak")
        depth  = row.get("history_depth")
        if pd.notna(streak):
            return (f"pre-existing: no PASS in the last {int(streak)} "
                    f"consecutive result(s)"
                    + (f" of {int(depth)} read" if pd.notna(depth) else "")
                    + " — failing before this diff")
        # The transition-derived case (prepare.REPORTED_TRANSITIONS): no
        # history was read, but an identical signature on both sides is
        # evidence enough on its own.
        if str(row.get("transition") or "").strip() == "same_failure":
            return ("failed with the same error on the baseline — pre-existing, "
                    "not caused by this range")
    return f"pre_classification={row.get('pre_classification')}"


def assemble_dataframe(diff_list: pd.DataFrame, results: list[dict]) -> pd.DataFrame:
    """
    Merge classifier results back into the diff_list. Auto-classified rows
    become classification=AUTO_CLASSIFIED; known-flaky rows are kept and
    labelled NEEDS_REVIEW (see _FLAKY_REASON) rather than dropped — they are
    still excluded from the Testray write by testray_writer._include.
    """
    results_by_id = {r["testray_case_id"]: r for r in results}

    df = diff_list.copy()

    def _row(row):
        cid = row["testray_case_id"]
        if _is_flaky(row) and pd.isna(row.get("pre_classification")):
            return pd.Series({
                "classification":  "NEEDS_REVIEW",
                "confidence":      None,
                "culprit_file":    None,
                "specific_change": None,
                "reason":          _FLAKY_REASON,
                "match_strategy":  "flaky",
            })
        if pd.notna(row.get("pre_classification")):
            return pd.Series({
                "classification":  _auto_label(row.get("pre_classification")),
                "confidence":      None,
                "culprit_file":    None,
                "specific_change": None,
                "reason":          _auto_reason(row),
                "match_strategy":  "auto",
            })
        r = results_by_id.get(cid)
        if not r:
            return pd.Series({
                "classification":  "NEEDS_REVIEW",
                "confidence":      None,
                "culprit_file":    None,
                "specific_change": None,
                "reason":          "No entry in results.json — defaulted to NEEDS_REVIEW",
                "match_strategy":  "missing",
            })
        return pd.Series({
            "classification":  r["classification"],
            "confidence":      r.get("confidence"),
            "culprit_file":    r.get("culprit_file"),
            "specific_change": r.get("specific_change"),
            "reason":          r["reason"],
            "match_strategy":  f"confidence={r['confidence']}",
        })

    df = pd.concat([df.reset_index(drop=True),
                    df.apply(_row, axis=1).reset_index(drop=True)], axis=1)
    df["tokens_in"]  = 0
    df["tokens_out"] = 0
    df["api_error"]  = None
    df["batch_number"] = None
    return df


def assemble_dataframe_subtask(
    diff_list: pd.DataFrame,
    results: list[dict],
    subtask_members: dict[int, list[int]] | None = None,
) -> pd.DataFrame:
    """Subtask-mode assembly. Fan one verdict out across every member case_id
    in the subtask. Auto-classified rows still get classification=AUTO_CLASSIFIED;
    flaky rows still get dropped. The diff_list keeps its `subtask_id` column
    from prepare.py (informational, may be NaN for unmapped cases) — we
    propagate it onto the output so fact_triage_results.subtask_id reflects
    the Testray grouping that produced this verdict.

    `subtask_members` (subtask_id → full member case_ids) is the canonical
    expansion source — the model only sees a truncated member list in the
    prompt, so its `case_ids` array is not authoritative for big subtasks.
    When a results entry names an integer subtask_id, we use the canonical
    list; for unmapped singletons (subtask_id null) we use the entry's
    case_ids verbatim."""

    subtask_members = subtask_members or {}

    # Build case_id → verdict-payload from results entries, expanding via
    # canonical member list when subtask_id is known.
    verdict_by_case: dict[int, dict] = {}
    subtask_by_case: dict[int, int]  = {}
    for r in results:
        # by-cluster results identify their group with group_id; by-subtask
        # bundles predating it use subtask_id. Accept either so one fan-out
        # serves both grouped modes.
        sid = r.get("group_id")
        if not isinstance(sid, int):
            sid = r.get("subtask_id")
        if isinstance(sid, int) and sid in subtask_members:
            cids = subtask_members[sid]
        else:
            cids = r.get("case_ids", [])
        for cid in cids:
            verdict_by_case[int(cid)] = r
            if isinstance(sid, int):
                subtask_by_case[int(cid)] = sid

    df = diff_list.copy()

    def _row(row):
        cid = int(row["testray_case_id"])
        if _is_flaky(row) and pd.isna(row.get("pre_classification")):
            return pd.Series({
                "classification":  "NEEDS_REVIEW",
                "confidence":      None,
                "culprit_file":    None,
                "specific_change": None,
                "reason":          _FLAKY_REASON,
                "match_strategy":  "flaky",
            })
        if pd.notna(row.get("pre_classification")):
            return pd.Series({
                "classification":  _auto_label(row.get("pre_classification")),
                "confidence":      None,
                "culprit_file":    None,
                "specific_change": None,
                "reason":          _auto_reason(row),
                "match_strategy":  "auto",
            })
        r = verdict_by_case.get(cid)
        if not r:
            return pd.Series({
                "classification":  "NEEDS_REVIEW",
                "confidence":      None,
                "culprit_file":    None,
                "specific_change": None,
                "reason":          "No subtask in results.json claimed this case_id — defaulted to NEEDS_REVIEW",
                "match_strategy":  "missing",
            })
        return pd.Series({
            "classification":  r["classification"],
            "confidence":      r.get("confidence"),
            "culprit_file":    r.get("culprit_file"),
            "specific_change": r.get("specific_change"),
            "reason":          r["reason"],
            "match_strategy":  f"subtask · confidence={r['confidence']}",
        })

    df = pd.concat([df.reset_index(drop=True),
                    df.apply(_row, axis=1).reset_index(drop=True)], axis=1)

    # Subtask_id: prefer the value from the verdict (which is the Testray
    # subtask the classifier saw), falling back to the diff_list value
    # (set when prepare.py joined the caseresult API). Auto-classified
    # rows get whatever diff_list had.
    def _resolve_subtask(row):
        cid = int(row["testray_case_id"])
        sid_from_verdict = subtask_by_case.get(cid)
        if sid_from_verdict is not None:
            return sid_from_verdict
        v = row.get("subtask_id")
        if pd.notna(v) and v != 0:
            try:
                return int(v)
            except (ValueError, TypeError):
                return None
        return None

    df["subtask_id"]   = df.apply(_resolve_subtask, axis=1)
    df["tokens_in"]    = 0
    df["tokens_out"]   = 0
    df["api_error"]    = None
    df["batch_number"] = None
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def annotate_culprit_commits(df: pd.DataFrame, full_cfg: dict,
                             meta: dict) -> pd.DataFrame:
    """Add `culprit_commits` — "LPD-94237 (5cbd024, a040008)" per culprit file.

    The file alone says *where*; the ticket and commit say *who changed it and
    why*, which is the difference between a lead and a chore. Silent no-op when
    the checkout or the hashes are unavailable: this is presentation, and must
    never fail a submit that has already persisted its verdicts.
    """
    if df is None or df.empty or "culprit_file" not in df.columns:
        return df
    hash_a, hash_b = meta.get("git_hash_a"), meta.get("git_hash_b")
    repo = (full_cfg.get("git") or {}).get("repo_path")
    if not (hash_a and hash_b and repo):
        return df
    repo = Path(str(repo)).expanduser()

    annotations: dict[str, str] = {}
    for path in {str(x).strip() for x in df["culprit_file"].dropna() if str(x).strip()}:
        try:
            rows = commits_touching_file(repo, hash_a, hash_b, path)
        except Exception:
            rows = []
        if not rows:
            continue
        by_ticket: dict[str, list[str]] = {}
        for short, ticket, _subject in rows:
            by_ticket.setdefault(ticket or "(no ticket)", []).append(short[:7])
        annotations[path] = " · ".join(
            f"{t} ({', '.join(hs)})" for t, hs in by_ticket.items()
        )
    if annotations:
        df["culprit_commits"] = df["culprit_file"].map(
            lambda v: annotations.get(str(v).strip()) if pd.notna(v) else None
        )
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description="Submit a triage run bundle.")
    ap.add_argument("run_dir", type=Path, help="Path to runs/r_<id>/")
    ap.add_argument("--no-write", action="store_true",
                    help="Validate + summarize + render the report, but skip "
                         "building the Testray batch payload.")
    ap.add_argument("--jira-parent", default=None,
                    help="Parent ticket for the report's prefilled Jira "
                         "drafts. Overrides run.yml `jira_parent` and "
                         "config.yml `jira.parent`.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build and save triageresults_batch.json, but don't "
                         "upsert it into Testray.")
    args = ap.parse_args()

    run_dir: Path = args.run_dir.resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"Not a directory: {run_dir}")

    run_yml   = run_dir / "run.yml"
    results_f = run_dir / "results.json"
    diff_list = run_dir / "diff_list.csv"
    for p in (run_yml, results_f, diff_list):
        if not p.exists():
            raise SystemExit(f"Missing required file: {p}")

    meta = yaml.safe_load(run_yml.read_text())
    payload = json.loads(results_f.read_text())
    diff_list_df = pd.read_csv(diff_list)

    # mode defaults to per-test for older bundles that don't have the field.
    mode = meta.get("mode") or MODE_PER_TEST
    if mode not in (MODE_PER_TEST, MODE_BY_SUBTASK, MODE_BY_CLUSTER):
        raise SystemExit(f"Unknown mode in run.yml: {mode!r}")

    # Validation uses the set of case_ids the classifier was expected to handle
    # (non-flaky, no pre_classification)
    expected = set(diff_list_df[
        ~diff_list_df["known_flaky"].fillna(False)
        & diff_list_df["pre_classification"].isna()
    ]["testray_case_id"].astype(int).tolist())
    all_diff = set(diff_list_df["testray_case_id"].dropna().astype(int).tolist())

    # Canonical group membership, loaded BEFORE validation because both need
    # it: validation to judge coverage honestly, and the fan-out to expand a
    # verdict across members the prompt truncated away.
    subtask_members: dict[int, list[int]] = {}
    if mode in GROUPED_MODES:
        st_path = run_dir / "diff_list_subtasks.csv"
        if st_path.exists():
            try:
                st_df = pd.read_csv(st_path)
            except pd.errors.EmptyDataError:
                # A run with no groups at all — every failure pre-existing or
                # flaky, so prepare wrote a headerless empty frame. That is a
                # clean build, not a broken bundle: there is simply no
                # membership to resolve.
                st_df = pd.DataFrame()
            for _, row in st_df.iterrows():
                # group_id is the identifier for by-cluster and for any
                # bundle written since it was introduced; fall back to
                # subtask_id so older by-subtask bundles still resolve.
                sid_v = row.get("group_id")
                if sid_v is None or (isinstance(sid_v, float) and pd.isna(sid_v)) or sid_v == "":
                    sid_v = row.get("subtask_id")
                if sid_v is None or (isinstance(sid_v, float) and pd.isna(sid_v)) or sid_v == "":
                    continue
                try:
                    sid = int(sid_v)
                except (ValueError, TypeError):
                    continue
                cids_str = str(row.get("member_case_ids") or "")
                cids = [int(c) for c in cids_str.split("|") if c.strip().isdigit()]
                if cids:
                    subtask_members[sid] = cids

    if mode in GROUPED_MODES:
        validate_results_subtask(payload, expected, all_diff,
                                 canonical_members=subtask_members)
    else:
        validate_results(payload, expected)

    # Consistency checks against run.yml
    if payload["run_id"] != meta["run_id"]:
        print(f"WARNING: results.json run_id={payload['run_id']} "
              f"does not match run.yml run_id={meta['run_id']}",
              file=sys.stderr)
    if payload["classifier"] != meta["classifier"]:
        print(f"WARNING: results.json classifier={payload['classifier']} "
              f"overrides run.yml classifier={meta['classifier']}",
              file=sys.stderr)

    if mode in GROUPED_MODES:
        df = assemble_dataframe_subtask(diff_list_df, payload["results"],
                                          subtask_members=subtask_members)
    else:
        df = assemble_dataframe(diff_list_df, payload["results"])

    counts = df["classification"].value_counts().to_dict()
    bug_rows = df[df["classification"] == "BUG"]
    culprit_hits = int(bug_rows["culprit_file"].notna().sum()) if len(bug_rows) else 0
    culprit_pct = (100 * culprit_hits / len(bug_rows)) if len(bug_rows) else 0.0
    # POSSIBLE_BUG also carries a candidate culprit and feeds defect training
    # (classification IN ('BUG','POSSIBLE_BUG')) — report its coverage too.
    pbug_rows = df[df["classification"] == "POSSIBLE_BUG"]
    pbug_hits = int(pbug_rows["culprit_file"].notna().sum()) if len(pbug_rows) else 0
    # Ticket-grain attribution, counted separately from file-grain. A
    # POSSIBLE_BUG with no culprit_file is NOT unattributed: the rubric sends
    # its candidates to specific_change as LPD/LPP/LPS ids, and the report
    # renders those as chips. Reporting only the file-grain number made a run
    # where every row named a ticket read as 14% attributed, which understates
    # what a reviewer actually gets. `_single` is the useful sub-count — one
    # candidate ticket is a reviewer opening one ticket, and it is also the
    # cheapest population to promote to file-grain later.
    def _tickets(row) -> set[str]:
        return set(_TICKET_RE.findall(str(row.get("specific_change") or "")))
    _pbug_tk  = [_tickets(r) for _, r in pbug_rows.iterrows()]
    pbug_tkt  = sum(1 for t in _pbug_tk if t)
    pbug_one  = sum(1 for t in _pbug_tk if len(t) == 1)

    print(f"\nRun:        {meta['run_id']}")
    print(f"Classifier: {payload['classifier']}")
    print(f"Mode:       {mode}")
    print(f"Build pair: {meta['build_id_a']} → {meta['build_id_b']} "
          f"(routine {meta['routine_id']})")
    print(f"Totals:     {len(df)} rows — "
          + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"BUG culprit_file coverage: {culprit_hits}/{len(bug_rows)} "
          f"({culprit_pct:.0f}%; target ≥85%)")
    if len(pbug_rows):
        # Two grains, because they answer different questions: file-grain is
        # what the attribution training set can consume, ticket-grain is what a
        # reviewer can act on. A row can be strong on the second and empty on
        # the first entirely by design.
        n_pbug = len(pbug_rows)
        print(f"POSSIBLE_BUG attribution: {pbug_hits}/{n_pbug} file-grain "
              f"({100 * pbug_hits / n_pbug:.0f}%; feeds training with BUG), "
              f"{pbug_tkt}/{n_pbug} ticket-grain "
              f"({pbug_one} narrowed to a single ticket)")
        if pbug_tkt < n_pbug:
            # The one shape that is a real gap: probably-a-defect with nothing
            # to open. Silent when it does not happen, which is the point.
            print(f"  WARN: {n_pbug - pbug_tkt} POSSIBLE_BUG row(s) name neither "
                  f"a culprit_file nor a candidate ticket — nothing for a "
                  f"reviewer to open")

    # Rubric drift, not a data error. NEEDS_REVIEW now means "cannot tell a
    # defect from an intentional change" — the candidate COUNT no longer routes
    # a verdict, so a medium NEEDS_REVIEW naming candidates is a verdict the
    # rubric would rather have seen as POSSIBLE_BUG. Report it rather than
    # rewrite it: silently promoting a verdict would put words in the
    # classifier's mouth, and the count is the signal the prompt needs work.
    # Only rows whose own text says the PRODUCT misbehaved. The previous form
    # counted every multi-candidate NEEDS_REVIEW and reported 91 on one run,
    # which is not an alarm — it is wallpaper. The rubric's misbehaviour
    # trigger routes exactly these to POSSIBLE_BUG, so a non-zero count here
    # means the prompt is not landing.
    _demotable = [
        r for _, r in df.iterrows()
        if verdicts.canonical(r.get("classification")) == "NEEDS_REVIEW"
        and _TICKET_RE.search(str(r.get("specific_change") or ""))
        and _MISBEHAVIOUR_RE.search(
            f"{r.get('reason') or ''} {r.get('specific_change') or ''}")
    ]
    if _demotable:
        print(f"NOTE: {len(_demotable)} NEEDS_REVIEW row(s) describe the product "
              f"misbehaving AND name a candidate — the rubric's misbehaviour "
              f"trigger routes those to POSSIBLE_BUG. Left as classified.")
    if mode in GROUPED_MODES:
        n_subtasks = len(payload["results"])
        n_with_sid = int(df["subtask_id"].notna().sum())
        print(f"Subtask fan-out: {n_subtasks} subtask verdicts → "
              f"{n_with_sid} case-rows carry subtask_id "
              f"(remaining {len(df) - n_with_sid} are unmapped/auto/missing)")

    # Turn each culprit_file into something actionable: which ticket and
    # commit in this range actually touched it. One git call per DISTINCT
    # path — there are usually a handful, and an LLM-named path that does not
    # resolve simply yields nothing rather than a wrong attribution.
    # Same resolution as prepare(), env overrides included — the two halves of
    # the pipeline must never end up pointing at different instances. Loaded
    # here rather than at the upsert because the report needs it too.
    full_cfg = load_config()
    df = annotate_culprit_commits(df, full_cfg, meta)

    # Resolve the Jira draft fields once and hand them to the report. Without
    # this the buttons still render, but with no parent and no reporter — the
    # legacy CreateIssueDetails endpoint does not auto-fill Reporter, so every
    # draft would open with that field empty.
    meta = dict(meta, jira=resolve_jira_settings(
        full_cfg, meta, parent_override=getattr(args, "jira_parent", None)))

    report_path = render_run(run_dir, df, meta)
    print(f"Report:     {report_path}")

    if args.no_write:
        print("\n--no-write set → not building the Testray batch payload.")
        return

    items = build_batch(df, meta, classifier=payload["classifier"])
    out_path = write_batch_file(items, run_dir)
    n_excluded = count_excluded(df, items)
    n_linked = sum(1 for it in items if FK_FIELD in it)
    reasons = excluded_breakdown(df)
    why = format_exclusions(reasons)
    excl = (f" ({n_excluded} excluded by write policy"
            + (f": {why}" if why else "") + ")") if n_excluded else ""
    print(f"\nTriageResult batch ({len(items)} rows{excl}) → {out_path}")
    print(f"  CaseResult FK resolved on {n_linked}/{len(items)} rows"
          f"{' — rest write unlinked' if n_linked < len(items) else ''}")

    if args.dry_run:
        print("  --dry-run set → not upserting into Testray.")
        return

    cfg = full_cfg["testray"]
    print(f"  Testray: {testray_target(full_cfg)}")
    print(f"  Upserting into {cfg['base_url'].rstrip('/')}{ENDPOINT} …")
    n_ok, n_fail, failures = post_batch(items, cfg, progress=True)
    print(f"  Upserted {n_ok}/{len(items)} TriageResults"
          + (f", {n_fail} failed" if n_fail else ""))
    for f in failures[:10]:
        print(f"    ! {f['externalReferenceCode']}: "
              f"HTTP {f['status']} {f['error']}")
    if n_fail > 10:
        print(f"    … and {n_fail - 10} more (see above pattern)")
    # The TriageRun row goes last, and only once the results are in, so its
    # counts describe what actually landed rather than what was attempted.
    # A failure here is reported but never fatal: the verdicts are already
    # persisted, and losing the run row costs the build-index diamond and the
    # report header, not data.
    try:
        # Per-verdict cluster counts, computed the same way the report's
        # headline does — one clusterKey per root cause, ranked by its worst
        # member — so the CX can lead with the same number instead of a row
        # count that reads several times larger.
        # DISPLAY verdicts, not raw classifications. The Testray index reads
        # these counts straight onto a column, so storing the raw label made it
        # report 100 NEEDS_REVIEW clusters for a run whose report showed 11 —
        # the rest being NOT_ATTRIBUTABLE. Severity order and the relabel rule
        # both come from `verdicts` so the two can no longer drift.
        cluster_verdicts: dict[str, int] = {}
        if len(df):
            from . import error_signature as _es
            display = verdicts.display_series(df)
            worst: dict[str, str] = {}
            for i, (_, r) in enumerate(df.iterrows()):
                key = _es.cluster_key(r.get("culprit_file"), r.get("test_case"),
                                      r.get("error_message"))
                v = display[i]
                if key not in worst or verdicts.rank(v) < verdicts.rank(worst[key]):
                    worst[key] = v
            for v in worst.values():
                if v:
                    cluster_verdicts[v] = cluster_verdicts.get(v, 0) + 1

        run_payload = build_triage_run(
            meta, df, classifier=payload["classifier"],
            n_written=n_ok, n_excluded=n_excluded,
            cluster_verdicts=cluster_verdicts,
        )
        write_triage_run(run_payload, cfg)
        print(f"  TriageRun {run_payload['externalReferenceCode']} upserted "
              f"({run_payload['totalClusters']} clusters, "
              f"{run_payload['totalWritten']} written)")
    except Exception as e:
        print(f"  ! TriageRun upsert failed ({e}) — the {n_ok} TriageResults "
              f"still landed, but the build-index icon and the report header "
              f"will have no run to read.", file=sys.stderr)

    if n_fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
