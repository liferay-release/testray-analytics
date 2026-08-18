"""
testray_writer.py — write triage verdicts to Testray as TriageResult objects.

The TriageResult Object, its two picklists, and the CaseResult→TriageResult
relationship ship in the `liferay-testray-analytics-site-initializer` client
extension (ARCHITECTURE.md §9). This module builds the payload and PUTs it over
headless REST — no DB credentials anywhere.

Write shape (all four confirmed live against a local DXP — tests/TESTING.md):

- **Picklist fields** (`classification`, `confidence`) are written as
  `{"key": ...}`, not bare strings. Picklist keys can't contain underscores, so
  the classification enum is flattened on the way out
  (`POSSIBLE_BUG` → `POSSIBLEBUG`); `confidence` keys are already the plain
  lowercase words. Read-back resolves to `{"key": "BUG", "name": "Bug"}`.
- **The CaseResult FK** is `r_caseResultToTriageResults_c_caseResultId`, set
  from the row's `caseresult_id` (the target build's caseResult object id).
  Rows without one are written *unlinked* rather than dropped — the verdict is
  still worth persisting, and the FK can be backfilled.
- **Upsert is PUT-by-ERC**, not `POST .../batch`. externalReferenceCode is
  `<buildB>_<caseId>_<classifier>`, so a rerun of the same (build pair,
  classifier) overwrites in place instead of duplicating (open-Q #1). Liferay's
  bulk `/batch` endpoint creates rather than upserts (and returns an async job),
  which loses the idempotency the ticket requires — so we fan out one PUT per
  item and collect per-item failures instead.

Inclusion policy (see `_include`): high-confidence FALSE_POSITIVE is excluded
(a confident "not a failure" — no decision value); the auto/env buckets
(DID_NOT_RUN, ENV_FAILURE) are excluded by default, toggleable via
`INCLUDE_AUTO_CLASSIFIED`.

`clusterKey` (§7) is computed here rather than in prepare, because the full key
includes `culprit_file` — an LLM output, so it is only knowable after
classification. `suspiciousCommits` (§9) is still not computed.

Note: TriageResult carries no buildId field — a build's verdicts are retrieved
either by ERC prefix (`startswith(externalReferenceCode,'<buildB>_')`) or
through the CaseResult side of the relationship.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

from . import error_signature
from .prepare import _testray_oauth_token

BATCH_FILENAME = "triageresults_batch.json"

ENDPOINT = "/o/c/triageresults"

# The relationship FK field, named by Liferay from the relationship name
# (`caseResultToTriageResults`) — see the site-initializer CX.
FK_FIELD = "r_caseResultToTriageResults_c_caseResultId"

# Classification enum -> picklist key. Picklist keys can't hold underscores;
# these keys must stay in lockstep with
# site-initializer/list-type-definitions/triage-classifications.list-type-entries.json.
_CLASSIFICATION_KEYS = {
    "BUG":            "BUG",
    "POSSIBLE_BUG":   "POSSIBLEBUG",
    "NEEDS_REVIEW":   "NEEDSREVIEW",
    "TEST_FIX":       "TESTFIX",
    "FALSE_POSITIVE": "FALSEPOSITIVE",
}


def _erc(build_id_b, case_id, classifier: str) -> str:
    """Deterministic upsert key: (buildB, caseId, classifier)."""
    return f"{build_id_b}_{int(case_id)}_{classifier}"


def _clean(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    return s or None


def _picklist(val):
    """Wrap a picklist value as Testray expects it, or None to omit the field."""
    v = _clean(val)
    return {"key": v} if v else None


def _classification_picklist(val):
    v = _clean(val)
    if v is None:
        return None
    try:
        return {"key": _CLASSIFICATION_KEYS[v]}
    except KeyError:
        raise ValueError(
            f"No triage-classifications picklist key for {v!r}. Known: "
            f"{sorted(_CLASSIFICATION_KEYS)}. Add an entry to the "
            f"site-initializer list-type-entries before writing it."
        ) from None


def _fk(val):
    """CaseResult object id, or None when the row can't be linked."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        cid = int(val)
    except (TypeError, ValueError):
        return None
    return cid or None  # 0 means 'no link' in Liferay's FK columns


# --- Write inclusion policy ------------------------------------------------
# High-confidence FALSE_POSITIVE is a confident "not a failure" — no decision
# value, and excluding it keeps the store lean (T3 retention goal). Low/medium
# FALSE_POSITIVE is uncertain enough to keep for a human glance.
# The auto/env buckets (DID_NOT_RUN, ENV_FAILURE — env/infra pre-classification,
# not LLM verdicts, never diff-analyzed) are excluded by default: re-derivable
# and non-actionable. Flip INCLUDE_AUTO_CLASSIFIED to persist them — note that
# doing so also needs picklist entries for them, or build_batch will raise.
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
    testray_case_id are skipped — they can't be keyed to a CaseResult.
    Null/blank fields are omitted rather than sent as null."""
    build_id_b    = meta.get("build_id_b")
    analysis_mode = meta.get("mode") or "per-test"

    items: list[dict] = []
    for _, row in df.iterrows():
        cid = row.get("testray_case_id")
        if cid is None or pd.isna(cid):
            continue
        if not _include(row.get("classification"), row.get("confidence")):
            continue

        item = {
            "externalReferenceCode": _erc(build_id_b, cid, classifier),
            "classification": _classification_picklist(row.get("classification")),
            "confidence":     _picklist(row.get("confidence")),
            "culpritFile":    _clean(row.get("culprit_file")),
            "specificChange": _clean(row.get("specific_change")),
            "reason":         _clean(row.get("reason")),
            "classifier":     classifier,
            "analysisMode":   analysis_mode,
            "gitHashA":       _clean(meta.get("git_hash_a")),
            "gitHashB":       _clean(meta.get("git_hash_b")),
            # §7: the full key needs culprit_file, which is an LLM output — so
            # it can only be computed here, after classification, not during
            # prepare. Rows sharing a root cause get the same key and the view
            # groups them; the version prefix makes a later normalize() change
            # visibly incomparable rather than silently different (open-Q #9).
            "clusterKey":     error_signature.cluster_key(
                                  row.get("culprit_file"),
                                  row.get("test_case"),
                                  row.get("error_message")),
            # suspiciousCommits (§9): not computed yet.
        }
        # Link to the target build's CaseResult when prepare captured its id;
        # otherwise write unlinked.
        fk = _fk(row.get("caseresult_id"))
        if fk is not None:
            item[FK_FIELD] = fk

        items.append({k: v for k, v in item.items() if v is not None})
    return items


# ---------------------------------------------------------------------------
# Headless write
# ---------------------------------------------------------------------------

class _Session:
    """Bearer-token REST session that re-mints its token once on a 401.
    Testray tokens expire in ~10 min; a large batch can outlive one."""

    def __init__(self, cfg: dict):
        self.cfg   = cfg
        self.base  = cfg["base_url"].rstrip("/")
        self.token = _testray_oauth_token(cfg)

    def _raw(self, method: str, path: str, body=None, timeout: int = 60):
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.base}{path}", data=data,
                                     method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def request(self, method: str, path: str, body=None, timeout: int = 60):
        try:
            return self._raw(method, path, body, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            self.token = _testray_oauth_token(self.cfg)  # expired — re-mint once
            return self._raw(method, path, body, timeout)


def _erc_path(erc: str) -> str:
    return f"{ENDPOINT}/by-external-reference-code/{urllib.parse.quote(erc, safe='')}"


RUN_ENDPOINT = "/o/c/triageruns"


def _run_erc_path(erc: str) -> str:
    return (f"{RUN_ENDPOINT}/by-external-reference-code/"
            f"{urllib.parse.quote(erc, safe='')}")


def build_triage_run(meta: dict, df: pd.DataFrame, *, classifier: str,
                     n_written: int, n_excluded: int,
                     n_clusters: int | None = None) -> dict:
    """The TriageRun row for a finished run (ARCHITECTURE.md §9).

    `TriageResult` answers *what the verdict was*; this answers *whether a run
    exists at all and how it went* — which results alone cannot, since a failed
    or still-running run has no results. It is also what the build-index
    diamond queries, so without it that column is blank no matter how many
    results landed.

    ERC is the run id, so a local bundle and its Testray row share one
    identity and a re-submit updates in place rather than accumulating.
    """
    counts = (df["classification"].value_counts().to_dict()
              if len(df) and "classification" in df else {})
    if n_clusters is None:
        # Count distinct clusterKey the same way report.py does, so the number
        # in Testray matches the number the report shows.
        keys = {
            error_signature.cluster_key(r.get("culprit_file"),
                                        r.get("test_case"),
                                        r.get("error_message"))
            for _, r in df.iterrows()
        } if len(df) else set()
        n_clusters = len(keys)

    prepared = _clean(meta.get("prepared_at"))
    payload = {
        "externalReferenceCode": _clean(meta.get("run_id")),
        "triageRunStatus":       {"key": "DONE"},
        "analysisMode":          _clean(meta.get("analysis_mode")
                                        or "build-vs-build"),
        "classifier":            classifier,
        "totalFailures":         int(meta.get("total_failures") or len(df)),
        "totalClusters":         int(n_clusters),
        "totalClassified":       int(len(df)),
        "totalWritten":          int(n_written),
        "totalExcluded":         int(n_excluded),
        "verdictCounts":         json.dumps(counts, sort_keys=True),
        # NOTE: the classification granularity (§7 — by-cluster / per-test) is
        # a different axis from analysisMode (§8) and TriageRun has no field
        # for it, so it exists only in the local run.yml. A run's granularity
        # is therefore not recoverable from Testray alone — known gap.
        "errorMessage":          None,
        "startedAt":             prepared,
        "finishedAt":            prepared,
        "r_buildToTriageRuns_c_buildId":         _fk(meta.get("build_id_b")),
        "r_baselineBuildToTriageRuns_c_buildId": _fk(meta.get("build_id_a")),
        "r_routineToTriageRuns_c_routineId":     _fk(meta.get("routine_id")),
    }
    return {k: v for k, v in payload.items() if v is not None}


def write_triage_run(payload: dict, cfg: dict, *, timeout: int = 60) -> dict:
    """Upsert one TriageRun by ERC. Raises on failure — the caller decides
    whether a missing run row is fatal (it is not: the results already
    landed)."""
    session = _Session(cfg)
    erc = payload["externalReferenceCode"]
    return session.request("PUT", _run_erc_path(erc), payload, timeout=timeout)


def post_batch(items: list[dict], cfg: dict, *, max_retries: int = 2,
               timeout: int = 60, progress: bool = False):
    """Upsert each item via PUT /o/c/triageresults/by-external-reference-code/{erc}.

    Partial-batch semantics: one item's failure never aborts the rest. Transient
    failures (5xx, URLError/timeout) are retried with linear backoff; 4xx other
    than 401 is a payload problem and is not retried.

    Returns (n_ok, n_fail, failures), where each failure is
    {"externalReferenceCode", "status", "error"} — `status` is the HTTP code or
    None for a transport error.
    """
    if not items:
        return 0, 0, []

    session = _Session(cfg)
    n_ok = 0
    failures: list[dict] = []

    for i, item in enumerate(items, start=1):
        # The ERC rides in both the path and the body — redundant, but it keeps
        # the persisted payload self-describing and Liferay accepts it.
        erc = item["externalReferenceCode"]
        status, error = None, None
        for attempt in range(max_retries + 1):
            try:
                session.request("PUT", _erc_path(erc), item, timeout=timeout)
                status, error = None, None
                break
            except urllib.error.HTTPError as e:
                status = e.code
                error = e.read().decode("utf-8", "replace")[:300]
                if e.code < 500:
                    break                      # payload/permission — retrying won't help
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                status, error = None, str(e)[:300]
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))

        if error is None:
            n_ok += 1
        else:
            failures.append({"externalReferenceCode": erc,
                             "status": status, "error": error})
        if progress and (i % 25 == 0 or i == len(items)):
            print(f"   [triageresults] {i}/{len(items)} "
                  f"({n_ok} ok, {len(failures)} failed)", flush=True)

    return n_ok, len(failures), failures


# ---------------------------------------------------------------------------
# Local artifact
# ---------------------------------------------------------------------------

def write_batch_file(items: list[dict], run_dir) -> Path:
    """Persist the exact payload posted, for inspection / replay."""
    out = Path(run_dir) / BATCH_FILENAME
    out.write_text(json.dumps({"items": items}, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    return out


def count_excluded(df: pd.DataFrame, items: list[dict]) -> int:
    """How many FK-eligible rows the write policy dropped."""
    eligible = (int(df["testray_case_id"].notna().sum())
                if "testray_case_id" in df.columns else len(df))
    return max(0, eligible - len(items))


def write_triage_batch(df: pd.DataFrame, meta: dict, classifier: str,
                       run_dir) -> tuple[Path, int, int]:
    """Build the batch payload and write it locally, without posting.
    Returns (path, n_written, n_excluded)."""
    items = build_batch(df, meta, classifier)
    return write_batch_file(items, run_dir), len(items), count_excluded(df, items)
