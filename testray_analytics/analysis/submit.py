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
import sys
from pathlib import Path

import pandas as pd
import yaml

from .prepare import load_config, testray_target
from .report import render_run
from .testray_writer import (
    ENDPOINT, FK_FIELD, build_batch, count_excluded, post_batch,
    write_batch_file,
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_CLASSIFICATIONS = {"BUG", "POSSIBLE_BUG", "NEEDS_REVIEW", "FALSE_POSITIVE", "TEST_FIX"}
_CONFIDENCES     = {"high", "medium", "low"}

MODE_PER_TEST   = "per-test"
MODE_BY_SUBTASK = "by-subtask"


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
                              all_diff_case_ids: set[int]) -> list[dict]:
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

        sid = r.get("subtask_id")
        if sid is not None and not isinstance(sid, int):
            errs.append(f"{prefix}.subtask_id must be int or null, got {type(sid).__name__}")
        elif isinstance(sid, int):
            if sid in seen_subtasks:
                errs.append(f"{prefix} duplicate subtask_id={sid}")
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

    missing = expected_case_ids - seen_classifiable_cids
    if missing:
        print(f"WARNING: {len(missing)} classifiable case(s) in diff_list.csv "
              f"have no entry in any subtask's case_ids — they will default "
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
_DID_NOT_RUN = {"BUILD_FAILURE", "NO_ERROR"}


def _auto_label(pre_classification) -> str:
    pc = str(pre_classification or "").strip().upper()
    return "DID_NOT_RUN" if pc in _DID_NOT_RUN else "ENV_FAILURE"


def assemble_dataframe(diff_list: pd.DataFrame, results: list[dict]) -> pd.DataFrame:
    """
    Merge classifier results back into the diff_list. Auto-classified rows
    become classification=AUTO_CLASSIFIED; known-flaky rows are dropped
    (already excluded from what the classifier saw).
    """
    results_by_id = {r["testray_case_id"]: r for r in results}

    # Drop flaky rows — not persisted
    df = diff_list[~diff_list["known_flaky"].fillna(False)].copy()

    def _row(row):
        cid = row["testray_case_id"]
        if pd.notna(row.get("pre_classification")):
            return pd.Series({
                "classification":  _auto_label(row.get("pre_classification")),
                "confidence":      None,
                "culprit_file":    None,
                "specific_change": None,
                "reason":          f"pre_classification={row['pre_classification']}",
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
        sid = r.get("subtask_id")
        if isinstance(sid, int) and sid in subtask_members:
            cids = subtask_members[sid]
        else:
            cids = r.get("case_ids", [])
        for cid in cids:
            verdict_by_case[int(cid)] = r
            if isinstance(sid, int):
                subtask_by_case[int(cid)] = sid

    # Drop flaky rows — not persisted
    df = diff_list[~diff_list["known_flaky"].fillna(False)].copy()

    def _row(row):
        cid = int(row["testray_case_id"])
        if pd.notna(row.get("pre_classification")):
            return pd.Series({
                "classification":  _auto_label(row.get("pre_classification")),
                "confidence":      None,
                "culprit_file":    None,
                "specific_change": None,
                "reason":          f"pre_classification={row['pre_classification']}",
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

def main() -> None:
    ap = argparse.ArgumentParser(description="Submit a triage run bundle.")
    ap.add_argument("run_dir", type=Path, help="Path to runs/r_<id>/")
    ap.add_argument("--no-write", action="store_true",
                    help="Validate + summarize + render the report, but skip "
                         "building the Testray batch payload.")
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
    if mode not in (MODE_PER_TEST, MODE_BY_SUBTASK):
        raise SystemExit(f"Unknown mode in run.yml: {mode!r}")

    # Validation uses the set of case_ids the classifier was expected to handle
    # (non-flaky, no pre_classification)
    expected = set(diff_list_df[
        ~diff_list_df["known_flaky"].fillna(False)
        & diff_list_df["pre_classification"].isna()
    ]["testray_case_id"].astype(int).tolist())
    all_diff = set(diff_list_df["testray_case_id"].dropna().astype(int).tolist())

    if mode == MODE_BY_SUBTASK:
        validate_results_subtask(payload, expected, all_diff)
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

    if mode == MODE_BY_SUBTASK:
        # Load canonical subtask membership from diff_list_subtasks.csv —
        # the prompt's per-subtask block truncates members for readability,
        # so the model's emitted case_ids may be incomplete. The CSV holds
        # the full member case_ids per subtask_id.
        subtask_members: dict[int, list[int]] = {}
        st_path = run_dir / "diff_list_subtasks.csv"
        if st_path.exists():
            st_df = pd.read_csv(st_path)
            for _, row in st_df.iterrows():
                sid_v = row.get("subtask_id")
                if pd.isna(sid_v) or sid_v == "":
                    continue
                try:
                    sid = int(sid_v)
                except (ValueError, TypeError):
                    continue
                cids_str = str(row.get("member_case_ids") or "")
                cids = [int(c) for c in cids_str.split("|") if c.strip().isdigit()]
                if cids:
                    subtask_members[sid] = cids
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
        print(f"POSSIBLE_BUG candidate-culprit coverage: {pbug_hits}/{len(pbug_rows)} "
              f"({100 * pbug_hits / len(pbug_rows):.0f}%) — feeds training with BUG")
    if mode == MODE_BY_SUBTASK:
        n_subtasks = len(payload["results"])
        n_with_sid = int(df["subtask_id"].notna().sum())
        print(f"Subtask fan-out: {n_subtasks} subtask verdicts → "
              f"{n_with_sid} case-rows carry subtask_id "
              f"(remaining {len(df) - n_with_sid} are unmapped/auto/missing)")

    report_path = render_run(run_dir, df, meta)
    print(f"Report:     {report_path}")

    if args.no_write:
        print("\n--no-write set → not building the Testray batch payload.")
        return

    items = build_batch(df, meta, classifier=payload["classifier"])
    out_path = write_batch_file(items, run_dir)
    n_excluded = count_excluded(df, items)
    n_linked = sum(1 for it in items if FK_FIELD in it)
    excl = f" ({n_excluded} excluded by write policy)" if n_excluded else ""
    print(f"\nTriageResult batch ({len(items)} rows{excl}) → {out_path}")
    print(f"  CaseResult FK resolved on {n_linked}/{len(items)} rows"
          f"{' — rest write unlinked' if n_linked < len(items) else ''}")

    if args.dry_run:
        print("  --dry-run set → not upserting into Testray.")
        return

    # Same resolution as prepare(), env overrides included — the two halves of
    # the pipeline must never end up pointing at different instances.
    full_cfg = load_config()
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
    if n_fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
