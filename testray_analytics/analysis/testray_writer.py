"""
testray_writer.py — write triage verdicts to Testray as TriageResult objects.

STUB (LPD-95842): builds the `/o/c/triageresults` batch payload — one entry per
verdict, keyed by an externalReferenceCode of `<buildB>_<caseId>_<classifier>`
so a rerun upserts rather than duplicates (ARCHITECTURE.md open-Q #1) — and
writes it to the run dir as `triageresults_batch.json` for inspection.

Inclusion policy (see `_include`): high-confidence FALSE_POSITIVE is excluded
(a confident "not a failure" — no decision value); the auto/env buckets
(DID_NOT_RUN, ENV_FAILURE) are excluded by default, toggleable via
`INCLUDE_AUTO_CLASSIFIED`.

LPD-95843 replaces the local write with a headless
`POST /o/c/triageresults/batch` against Testray, and wires the CaseResult
relationship FK (needs the target build's caseResult object ids + the deployed
TriageResult Object). `clusterKey` (§7) and `suspiciousCommits` (§9) are not
computed yet and are omitted here.
"""

import json
from pathlib import Path

import pandas as pd

BATCH_FILENAME = "triageresults_batch.json"


def _erc(build_id_b, case_id, classifier: str) -> str:
    """Deterministic upsert key: (buildB, caseId, classifier)."""
    return f"{build_id_b}_{int(case_id)}_{classifier}"


def _clean(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    return s or None


# --- Write inclusion policy ------------------------------------------------
# High-confidence FALSE_POSITIVE is a confident "not a failure" — no decision
# value, and excluding it keeps the store lean (T3 retention goal). Low/medium
# FALSE_POSITIVE is uncertain enough to keep for a human glance.
# The auto/env buckets (DID_NOT_RUN, ENV_FAILURE — env/infra pre-classification,
# not LLM verdicts, never diff-analyzed) are excluded by default: re-derivable
# and non-actionable. Flip INCLUDE_AUTO_CLASSIFIED to persist them.
INCLUDE_AUTO_CLASSIFIED = False

# The auto/env buckets produced by submit._auto_label.
_AUTO_LABELS = {"DID_NOT_RUN", "ENV_FAILURE"}


def _include(classification, confidence) -> bool:
    cls  = (classification or "").strip()
    conf = str(confidence or "").strip().lower()
    if cls == "FALSE_POSITIVE" and conf == "high":
        return False
    if cls in _AUTO_LABELS and not INCLUDE_AUTO_CLASSIFIED:
        return False
    return True


def build_batch(df: pd.DataFrame, meta: dict, classifier: str) -> list[dict]:
    """One TriageResult entry per classified case row. Rows without a
    testray_case_id are skipped — they can't be keyed to a CaseResult."""
    build_id_b    = meta.get("build_id_b")
    analysis_mode = meta.get("mode") or "per-test"

    items: list[dict] = []
    for _, row in df.iterrows():
        cid = row.get("testray_case_id")
        if cid is None or pd.isna(cid):
            continue
        if not _include(row.get("classification"), row.get("confidence")):
            continue
        items.append({
            "externalReferenceCode": _erc(build_id_b, cid, classifier),
            "classification": _clean(row.get("classification")),
            "confidence":     _clean(row.get("confidence")),
            "culpritFile":    _clean(row.get("culprit_file")),
            "specificChange": _clean(row.get("specific_change")),
            "reason":         _clean(row.get("reason")),
            "classifier":     classifier,
            "analysisMode":   analysis_mode,
            "gitHashA":       _clean(meta.get("git_hash_a")),
            "gitHashB":       _clean(meta.get("git_hash_b")),
            # For the CaseResult FK lookup in LPD-95843:
            "testrayCaseId":  int(cid),
            "testrayBuildId": build_id_b,
            # clusterKey (§7) + suspiciousCommits (§9): TODO, not computed yet.
        })
    return items


def write_triage_batch(df: pd.DataFrame, meta: dict, classifier: str,
                       run_dir) -> tuple[Path, int, int]:
    """Build the batch payload and write it locally (LPD-95842 stub).
    Returns (path, n_written, n_excluded). LPD-95843 will POST this instead."""
    items = build_batch(df, meta, classifier)
    eligible = (int(df["testray_case_id"].notna().sum())
                if "testray_case_id" in df.columns else len(df))
    out = Path(run_dir) / BATCH_FILENAME
    out.write_text(json.dumps({"items": items}, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    return out, len(items), max(0, eligible - len(items))
