"""
apps/triage/prepare.py

Build a triage run bundle for the dev's own Claude Code session to classify.

Usage:
    python3 -m apps.triage.prepare \
        --baseline-build-id <N> [--baseline-hash <sha>] [--baseline-name <str>] \
        --target-build-id   <N> [--target-hash   <sha>] [--target-name <str>] \
        [--classifier <label>]

api-only: both sides load case results from the Testray REST api. There is no
database dependency; the git hash is auto-resolved from the api response, and
--{side}-hash is an optional override.

Emits runs/r_<ts>_<A>_<B>/:
    run.yml              metadata (build ids, hashes, routine, sources)
    diff_list.csv        one row per PASSED→FAILED/BLOCKED/UNTESTED case,
                         enriched with component/team + pre_classification
    hunks.txt            filtered git diff (hunks matching failing tests)
    git_diff_full.diff   full unfiltered diff (for fallback inspection)
    test_fragments.txt   fragments fed to extract_relevant_hunks.py
    prompt.md            instructions for the dev's Claude Code session
    results.schema.json  JSON schema validating results.json

The dev's Claude Code session reads prompt.md, classifies, writes
results.json. Then `testray-analysis submit <run_dir>` validates and writes.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from . import prompt_helpers
from .config import find_config_file

TRIAGE_DIR   = Path(__file__).resolve().parent
# Run bundles default to ./runs (cwd-relative), NOT inside the installed
# package. Override with --out (e.g. a Jenkins workspace/artifacts dir).
DEFAULT_RUNS_DIRNAME = "runs"


def _disp(p) -> str:
    """Display path — relative to cwd when possible, else absolute. Run bundles
    live wherever --out points, not necessarily under cwd or the package."""
    p = Path(p).resolve()
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        return str(p)

DEFAULT_CLASSIFIER = "agent:claude-opus-4-8"

SOURCE_API = "api"
SOURCES    = (SOURCE_API,)

# Triage mode — per-test (default) or by-subtask (testflow-aware grouping).
# Subtask mode classifies once per Testray Subtask and fans the verdict out
# to the member case-rows in fact_triage_results. Target source must be `api`
# for subtask mode (the subtask link lives on the caseresult object — see
# r_subtaskToCaseResults_c_subtaskId — which only the api fetch reads).
MODE_PER_TEST   = "per-test"
MODE_BY_SUBTASK = "by-subtask"
MODES           = (MODE_PER_TEST, MODE_BY_SUBTASK)

GIT_DIFF_EXCLUDES = [
    ":!**/artifact.properties",
    ":!**/.releng/**",
    ":!**/liferay-releng.changelog",
    ":!**/app.changelog",
    ":!**/app.properties",
    ":!**/bnd.bnd",
    ":!**/packageinfo",
    ":!**/*.xml",
    ":!**/*.properties",
    ":!**/*.yml",
    ":!**/*.yaml",
    ":!**/*.tf",
    ":!**/*.sh",
    ":!**/*.scss",
    ":!**/*.css",
    ":!**/*.gradle",
    ":!**/package.json",
    ":!**/*.json",
    ":!cloud/**",
]

# Commit subjects matching these patterns are bot-generated module-version
# bumps (artifact:ignore X Y.Z.W ...) or per-module "prep next" tags. They
# pollute the cluster section without being plausible root-cause candidates.
def _is_noise_commit_subject(subj: str) -> bool:
    s = subj.strip()
    return s.startswith("artifact:ignore") or "prep next" in s


# ---------------------------------------------------------------------------
# Side specification
# ---------------------------------------------------------------------------

@dataclass
class SideSpec:
    """One side of the triage pair (baseline or target)."""
    role:     str            # "baseline" or "target"
    build_id: int
    hash:     str  | None = None
    name:     str  | None = None
    source:   str = SOURCE_API   # retained only for run.yml provenance

    @property
    def flag_prefix(self) -> str:
        return f"--{self.role}"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(find_config_file()) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Step 1: test_diff — api fetcher, one output shape
# ---------------------------------------------------------------------------
#
# Downstream (git diff → hunks → prompt) needs a DataFrame with:
#
#   case_id, case_name, case_flaky, component_name, team_name,
#   status, errors, jira_issue
#
# api-only: the Testray REST fetch supplies case_id + status; case_name,
# component_name, team_name, and jira_issue are backfilled post-diff via the
# enrichment functions (name/component) or left blank (team/jira — backlog).
# ---------------------------------------------------------------------------

# Worst-status-wins order for de-duping retry rows.
_STATUS_RANK = {"FAILED": 4, "BLOCKED": 3, "UNTESTED": 2, "PASSED": 1}


def _testray_oauth_token(cfg: dict) -> str:
    """OAuth2 client_credentials flow against the Testray Liferay instance.
    Returns a bearer token. Mirrors extract/extract_testray.R::get_token()."""
    base = cfg["base_url"].rstrip("/")
    if not cfg.get("client_id") or not cfg.get("client_secret"):
        raise SystemExit(
            "testray.client_id / testray.client_secret missing from config.yml. "
            "Both are required for api sources."
        )
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     cfg["client_id"],
        "client_secret": cfg["client_secret"],
    }).encode()
    req = urllib.request.Request(f"{base}/o/oauth2/token", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    token = body.get("access_token")
    if not token:
        raise SystemExit(f"OAuth2 token response had no access_token: {body}")
    return token


def _testray_fetch_paginated(
    endpoint: str, params: dict, token: str, base_url: str,
    page_size: int = 500, sleep_between: float = 0.3,
    progress_label: str | None = None,
) -> list[dict]:
    """Follow Liferay Objects pagination until lastPage. When `progress_label`
    is set, prints one stderr line per page so long fetches don't look hung.

    Paging is forced into a stable `id:asc` order (callers can override via
    `params["sort"]`). Without an explicit sort the server is free to order
    pages inconsistently, and a large fetch then returns some rows twice while
    skipping others entirely — silently, since the row count still looks
    plausible. Observed live: a 15,386-row caseresult fetch yielded only 14,104
    distinct ids."""
    base = base_url.rstrip("/")
    items: list[dict] = []
    page = 1
    while True:
        q = dict(params)
        q.setdefault("sort", "id:asc")
        q["page"]     = page
        q["pageSize"] = page_size
        url = f"{base}{endpoint}?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise SystemExit("Testray API 401 — token expired. Re-run.")
            raise
        items.extend(data.get("items", []))
        last_page = data.get("lastPage", 1)
        if progress_label:
            print(
                f"   [{progress_label}] page {page}/{last_page} "
                f"({len(items)} rows so far)",
                file=sys.stderr, flush=True,
            )
        if page >= last_page:
            break
        page += 1
        time.sleep(sleep_between)
    return items


def fetch_case_metadata(case_ids: list[int], cfg: dict) -> dict[int, dict]:
    """Fetch per-case metadata (name, flaky flag, component_id) from Testray's
    case object. Returns
    {case_id: {"name": str, "flaky": bool, "component_id": int|None}};
    case_ids that 404 are omitted. Used to backfill `test_case` /
    `component_name` columns on api-source rows so the join + fragment matcher
    have something to anchor on.

    One GET /o/c/cases/{id} per case_id; expected ≤ ~100 case_ids per run
    after diff dedup, so per-id calls are acceptable. Batch via
    `filter=id in (...)` if this becomes a hot path."""
    if not case_ids:
        return {}
    token = _testray_oauth_token(cfg)
    base = cfg["base_url"].rstrip("/")
    total = len(case_ids)
    step = max(1, total // 10)  # ~10 progress lines for any total
    print(f"   [case metadata] fetching {total} case(s) …",
          file=sys.stderr, flush=True)
    out: dict[int, dict] = {}
    for i, cid in enumerate(case_ids, start=1):
        url = f"{base}/o/c/cases/{cid}"
        def _do_request(tok):
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {tok}"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        try:
            body = _do_request(token)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                token = _testray_oauth_token(cfg)
                try:
                    body = _do_request(token)
                except urllib.error.HTTPError as e2:
                    if e2.code == 404:
                        continue
                    raise
            elif e.code == 404:
                continue
            else:
                raise
        comp_id = body.get("r_componentToCases_c_componentId")
        out[int(cid)] = {
            "name":         body.get("name") or "",
            "flaky":        str(body.get("flaky")).lower() == "true",
            "component_id": int(comp_id) if comp_id else None,
        }
        if i % step == 0 or i == total:
            print(f"   [case metadata] {i}/{total}",
                  file=sys.stderr, flush=True)
        time.sleep(0.05)
    return out


def fetch_component_metadata(component_ids: list[int], cfg: dict) -> dict[int, str]:
    """Resolve {component_id: name} via /o/c/components/{id}. Used to backfill
    `component_name` on api-source caseresults so (case_name, component_name)
    joins work for csv/tar × api combos."""
    if not component_ids:
        return {}
    token = _testray_oauth_token(cfg)
    base = cfg["base_url"].rstrip("/")
    total = len(component_ids)
    step = max(1, total // 10)
    print(f"   [component metadata] fetching {total} component(s) …",
          file=sys.stderr, flush=True)
    out: dict[int, str] = {}
    for i, cid in enumerate(component_ids, start=1):
        url = f"{base}/o/c/components/{cid}"
        def _do_request(tok):
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {tok}"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        try:
            body = _do_request(token)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                token = _testray_oauth_token(cfg)
                try:
                    body = _do_request(token)
                except urllib.error.HTTPError as e2:
                    if e2.code == 404:
                        continue
                    raise
            elif e.code == 404:
                continue
            else:
                raise
        name = body.get("name")
        if name:
            out[int(cid)] = name
        if i % step == 0 or i == total:
            print(f"   [component metadata] {i}/{total}",
                  file=sys.stderr, flush=True)
        time.sleep(0.05)
    return out


def enrich_api_caseresults(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Backfill `case_name`, `case_flaky`, and `component_name` on a pre-diff
    api-source dataframe so it can be joined against a csv/tar side on
    (case_name, component_name). Idempotent — does nothing if every row
    already has both name and component_name. One /o/c/cases call per
    case_id, plus one /o/c/components call per unique component_id."""
    if df.empty:
        return df
    needs = (df["case_name"].isna() | (df["case_name"].astype(str).str.strip() == "")) \
          | (df["component_name"].isna() | (df["component_name"].astype(str).str.strip() == ""))
    if not needs.any():
        return df
    case_ids = sorted({
        int(x) for x in df.loc[needs, "case_id"].dropna()
    })
    if not case_ids:
        return df
    print(f"   enriching {len(case_ids)} api case(s) with name + component …")
    case_meta = fetch_case_metadata(case_ids, cfg)
    if not case_meta:
        print(f"   no cases returned — leaving rows unenriched", file=sys.stderr)
        return df
    comp_ids = sorted({
        m["component_id"] for m in case_meta.values() if m.get("component_id")
    })
    comp_names = fetch_component_metadata(comp_ids, cfg)

    df = df.copy()
    name_filled = comp_filled = flaky_marked = 0
    for cid, meta in case_meta.items():
        row_mask = df["case_id"] == cid
        if not row_mask.any():
            continue
        if meta.get("name"):
            cur = df.loc[row_mask, "case_name"]
            blank = cur.isna() | (cur.astype(str).str.strip() == "")
            df.loc[row_mask & blank, "case_name"] = meta["name"]
            name_filled += int(blank.sum())
        if meta.get("component_id") and meta["component_id"] in comp_names:
            cur = df.loc[row_mask, "component_name"]
            blank = cur.isna() | (cur.astype(str).str.strip() == "")
            df.loc[row_mask & blank, "component_name"] = comp_names[meta["component_id"]]
            comp_filled += int(blank.sum())
        if meta.get("flaky"):
            df.loc[row_mask, "case_flaky"] = True
            flaky_marked += int(row_mask.sum())
    print(f"   filled {name_filled} case_name, {comp_filled} component_name, "
          f"{flaky_marked} flaky flag(s)")
    return df


def enrich_api_case_names(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """When an api source contributed rows lacking `test_case` (api
    caseresults don't carry case names), fetch names from the Testray case
    object and backfill. Idempotent — does nothing if every row already has
    a name. Also backfills `known_flaky` from the case-level flaky flag."""
    if df.empty:
        return df
    name_col = "test_case"
    if name_col not in df.columns:
        return df
    needs_mask = df[name_col].isna() | (df[name_col].astype(str).str.strip() == "")
    if not needs_mask.any():
        return df
    case_ids = sorted({
        int(x) for x in df.loc[needs_mask, "testray_case_id"].dropna()
    })
    if not case_ids:
        return df

    print(f"   enriching {len(case_ids)} case name(s) from Testray REST …")
    metadata = fetch_case_metadata(case_ids, cfg)
    if not metadata:
        print(f"   no cases returned — leaving rows unenriched", file=sys.stderr)
        return df

    df = df.copy()
    name_filled = 0
    flaky_marked = 0
    for cid, meta in metadata.items():
        row_mask = df["testray_case_id"] == cid
        if meta.get("name"):
            current = df.loc[row_mask, name_col]
            blank = current.isna() | (current.astype(str).str.strip() == "")
            df.loc[row_mask & blank, name_col] = meta["name"]
            name_filled += int(blank.sum())
        if meta.get("flaky"):
            df.loc[row_mask, "known_flaky"] = True
            flaky_marked += int(row_mask.sum())
    print(f"   filled {name_filled} test_case value(s); marked {flaky_marked} as known_flaky")
    return df


def fetch_build_caseresults_api(build_id: int, cfg: dict) -> pd.DataFrame:
    """Fetch all case results for a build via Testray REST. `case_name`,
    `component_name`, `team_name`, `jira_issue` are left blank.
    `case_name` is backfilled post-diff via `enrich_api_case_names()` so the
    fragment-based hunk matcher has something to anchor on. Component, team,
    and jira remain blank (separate per-case lookups — backlog).

    The caseResult's own object `id` is kept as `caseresult_id`: it is the FK
    the TriageResult write needs
    (`r_caseResultToTriageResults_c_caseResultId`), and it is only resolvable
    here — nothing downstream can recover it from a case id alone. Target-side
    ids are the ones that matter; see `compute_test_diff`.

    Also pulls `r_subtaskToCaseResults_c_subtaskId` so subtask-mode triage
    (--by-subtask) can group failures by Testray Subtask without a second
    round-trip. The field is 0/null on builds that don't have a testflow,
    which is the common case for baselines and pre-testflow targets — the
    subtask_id column is left as 0 in that case and downstream code treats
    0/NaN as 'no subtask link'.
    """
    token = _testray_oauth_token(cfg)
    items = _testray_fetch_paginated(
        "/o/c/caseresults",
        {
            "filter": f"r_buildToCaseResult_c_buildId eq '{build_id}'",
            # gitHash is a Build property, not a CaseResult one — resolved
            # separately via fetch_build_metadata().
            "fields": "id,dueStatus,errors,"
                      "r_caseToCaseResult_c_caseId,"
                      "r_subtaskToCaseResults_c_subtaskId,"
                      "r_componentToCaseResult_c_componentId,"
                      "r_teamToCaseResult_c_teamId",
        },
        token=token, base_url=cfg["base_url"],
        progress_label=f"caseresults build {build_id}",
    )
    if not items:
        return pd.DataFrame()
    return pd.DataFrame({
        "case_id":        [it.get("r_caseToCaseResult_c_caseId") for it in items],
        "caseresult_id":  [it.get("id") for it in items],
        "case_name":      [None] * len(items),
        "case_flaky":     [None] * len(items),
        "component_name": [None] * len(items),
        "team_name":      [None] * len(items),
        "status":         [(it.get("dueStatus") or {}).get("key") for it in items],
        "errors":         [it.get("errors") for it in items],
        "jira_issue":     [None] * len(items),
        "subtask_id":     [it.get("r_subtaskToCaseResults_c_subtaskId") or 0
                           for it in items],
    })


def fetch_build_metadata(build_id: int, cfg: dict) -> dict:
    """Read a build's metadata from /o/c/builds/{id}. gitHash and routine id
    are Build properties (not carried on caseresults). Returns
    {"git_hash": str|None, "routine_id": int|None}; empty values on 404."""
    token = _testray_oauth_token(cfg)
    base = cfg["base_url"].rstrip("/")
    url = f"{base}/o/c/builds/{build_id}"

    def _do_request(tok):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    try:
        body = _do_request(token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            body = _do_request(_testray_oauth_token(cfg))
        elif e.code == 404:
            return {"git_hash": None, "routine_id": None}
        else:
            raise
    h = body.get("gitHash")
    rid = body.get("r_routineToBuilds_c_routineId")
    return {
        "git_hash":   str(h).strip() if h and str(h).strip() else None,
        "routine_id": int(rid) if rid else None,
    }


def fetch_caseresults(spec: SideSpec, cfg: dict) -> pd.DataFrame:
    """Fetch case results for a SideSpec via the Testray REST api."""
    return fetch_build_caseresults_api(spec.build_id, cfg["testray"])


def _aggregate_baseline(df: pd.DataFrame, key_cols: list[str]) -> pd.DataFrame:
    """Per key, status='PASSED' if any retry passed; else worst status wins.
    Matches test_diff.sql semantics — any passing run counts as passing in A."""
    if df.empty:
        return df
    df = df.copy()
    df["_is_pass"] = (df["status"] == "PASSED").astype(int)
    df["_rank"]    = df["status"].map(_STATUS_RANK).fillna(0).astype(int)
    df = df.sort_values(["_is_pass", "_rank"], ascending=[False, False])
    out = df.drop_duplicates(subset=key_cols, keep="first") \
            .drop(columns=["_is_pass", "_rank"]) \
            .reset_index(drop=True)
    return out


def _aggregate_target(df: pd.DataFrame, key_cols: list[str]) -> pd.DataFrame:
    """Per key, keep the worst-status row — any failed retry should surface."""
    if df.empty:
        return df
    df = df.copy()
    df["_rank"] = df["status"].map(_STATUS_RANK).fillna(0).astype(int)
    out = df.sort_values("_rank", ascending=False) \
            .drop_duplicates(subset=key_cols, keep="first") \
            .drop(columns="_rank") \
            .reset_index(drop=True)
    return out


def compute_test_diff(baseline: pd.DataFrame, target: pd.DataFrame) -> pd.DataFrame:
    """Inner-join baseline and target; keep cases that PASSED in A and
    FAILED/BLOCKED/UNTESTED in B.

    Join key: `case_id` if the target has one (db, api targets); otherwise
    `(case_name, component_name)` (csv targets). A final dropna on
    `testray_case_id` discards rows with no persistable id."""
    if baseline.empty or target.empty:
        return pd.DataFrame()

    target_has_ids   = target["case_id"].notna().any()
    baseline_has_ids = baseline["case_id"].notna().any()
    both_have_ids    = target_has_ids and baseline_has_ids
    key_cols = ["case_id"] if both_have_ids else ["case_name", "component_name"]

    b = _aggregate_baseline(baseline, key_cols=key_cols)
    t = _aggregate_target(target,     key_cols=key_cols)

    merged = b.merge(t, on=key_cols, how="inner", suffixes=("_a", "_b"))
    diff = merged[
        (merged["status_a"] == "PASSED")
        & merged["status_b"].isin(["FAILED", "BLOCKED", "UNTESTED"])
    ].copy()

    if both_have_ids:
        case_id_col = "case_id"
        name_col    = "case_name_a"
        comp_col    = "component_name_a"
    else:
        name_col = "case_name"
        comp_col = "component_name"
        # Prefer target-side case_id (api targets carry real ids; csv baselines don't).
        case_id_col = "case_id_b" if target_has_ids else "case_id_a"
    # case_flaky may live on baseline (db) or target (api after enrichment).
    if "case_flaky_a" in diff.columns and "case_flaky_b" in diff.columns:
        diff["_flaky_combined"] = diff["case_flaky_a"].where(
            diff["case_flaky_a"].notna(), diff["case_flaky_b"]
        )
        flaky_col = "_flaky_combined"
    elif "case_flaky_a" in diff.columns:
        flaky_col = "case_flaky_a"
    else:
        flaky_col = "case_flaky_b"

    out = pd.DataFrame({
        "testray_case_id":        diff[case_id_col],
        "test_case":              diff[name_col],
        "known_flaky":            diff[flaky_col].fillna(False).astype(bool),
        "testray_component_name": diff[comp_col],
        "status_a":               diff["status_a"],
        "status_b":               diff["status_b"],
        "error_message":          diff["errors_b"],
        "linked_issues":          diff["jira_issue_b"],
    })

    # Propagate the TARGET side's caseresult_id — the TriageResult FK must
    # point at build B's caseResult (the failure being triaged), never the
    # baseline's. Same column-suffix cases as subtask_id below.
    if "caseresult_id_b" in diff.columns:
        crid = diff["caseresult_id_b"]
    elif "caseresult_id" in diff.columns and target_has_ids:
        crid = diff["caseresult_id"]
    else:
        crid = None
    if crid is not None:
        # Nullable Int64 so the id round-trips through diff_list.csv as an
        # integer rather than 5.05505733e+08.
        out["caseresult_id"] = pd.to_numeric(crid, errors="coerce").astype("Int64")

    # Propagate target-side subtask_id when present. Column name depends on
    # which sides carried it through the merge: `subtask_id_b` when both
    # sides had it (api×api), bare `subtask_id` when only target carried it,
    # absent when target source is db/csv/tar.
    if "subtask_id_b" in diff.columns:
        out["subtask_id"] = diff["subtask_id_b"]
    elif "subtask_id" in diff.columns and target_has_ids:
        out["subtask_id"] = diff["subtask_id"]
    # else: leave subtask_id off the dataframe; subtask mode will reject the
    # combo upstream in validate_combo_for_mode().

    out = out.dropna(subset=["testray_case_id"]).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Step 2: metadata — resolved from spec args (api-only, no DB)
# ---------------------------------------------------------------------------

def resolve_side_metadata(spec: SideSpec, cfg: dict) -> dict:
    """Resolve git_hash / routine_id / build_name for one side (api-only).

    git_hash comes from the build object (/o/c/builds/{id}); --{role}-hash is an
    optional override. No DB fallback.
    """
    build = fetch_build_metadata(spec.build_id, cfg)
    git_hash = spec.hash or build["git_hash"]
    if not git_hash:
        raise SystemExit(
            f"Build {spec.build_id} has no gitHash (and no {spec.flag_prefix}-hash "
            f"was supplied). Pass {spec.flag_prefix}-hash <sha> as an override."
        )
    return {
        "build_name": spec.name or f"api:{spec.build_id}",
        "git_hash":   git_hash,
        "routine_id": build["routine_id"],
    }


# ---------------------------------------------------------------------------
# Step 3: git diff with exclusions
# ---------------------------------------------------------------------------

def run_git_diff(git_repo: Path, hash_a: str, hash_b: str, out_path: Path,
                 fetch_specs: list | None = None) -> int:
    git_dir = Path(git_repo).expanduser()
    if not (git_dir / ".git").is_dir():
        raise SystemExit(f"Not a git repo: {git_dir}. "
                         f"Set git.repo_path in config/config.yml.")

    def _have(h: str) -> bool:
        return subprocess.run(
            ["git", "-C", str(git_dir), "cat-file", "-e", f"{h}^{{commit}}"],
            capture_output=True,
        ).returncode == 0

    # 1. Explicit fetches first — for commits not on origin (e.g. a temp
    #    mitigation branch on a fork). --fetch-ref <remote-or-url> <ref>.
    for src, ref in (fetch_specs or []):
        print(f"Fetching {ref} from {src} …", file=sys.stderr)
        subprocess.run(["git", "-C", str(git_dir), "fetch", "--quiet", src, ref],
                       check=False)

    # 2. Anything still missing → try origin (gets any official branch).
    if not (_have(hash_a) and _have(hash_b)):
        print("Fetching from origin …", file=sys.stderr)
        subprocess.run(["git", "-C", str(git_dir), "fetch", "--quiet", "origin"],
                       check=False)

    # 3. Still missing → fail with actionable guidance rather than a cryptic
    #    "fatal: bad object" from git diff.
    missing = [h for h in (hash_a, hash_b) if not _have(h)]
    if missing:
        raise SystemExit(
            f"Commit(s) not found in {git_dir} after fetching: "
            f"{', '.join(h[:12] for h in missing)}.\n"
            f"If a build ran on a temp/fork branch not on origin, pass "
            f"--fetch-ref <remote-or-url> <branch> so prepare can fetch it."
        )

    cmd = ["git", "-C", str(git_dir), "diff", hash_a, hash_b, "--"] + GIT_DIFF_EXCLUDES
    with open(out_path, "wb") as f:
        subprocess.run(cmd, stdout=f, check=True)
    # Count lines reading bytes — portal diffs can contain non-UTF-8 bytes
    # (e.g. Latin-1 in test fixtures) that crash default text-mode decoding.
    with open(out_path, "rb") as f:
        return sum(1 for _ in f)


# ---------------------------------------------------------------------------
# Step 4: fragments + relevant hunks
# ---------------------------------------------------------------------------

def derive_test_fragments(df: pd.DataFrame) -> set[str]:
    """Module/class tokens from test_case names — fed to
    extract_relevant_hunks.py to filter the diff to relevant files."""
    fragments: set[str] = set()
    for name in df["test_case"].dropna():
        name = str(name)
        if ".spec.ts" in name or ".spec.js" in name:
            spec = re.split(r"[/\s>]", name)[0].split("/")[-1]
            if spec:
                fragments.add(spec)
            parts = name.split("/")
            if len(parts) > 1:
                fragments.add(parts[0])
        elif "." in name and ">" not in name:
            classname = name.split(".")[-1].split("#")[0].strip()
            if classname:
                fragments.add(classname + ".java")
            for part in name.split("."):
                if part not in ("com", "liferay", "internal", "test", "impl") \
                        and len(part) > 4:
                    fragments.add(part)
                    break
        elif name.startswith("LocalFile."):
            module = name.replace("LocalFile.", "").split("#")[0]
            fragments.add(module.lower())
    return fragments


def run_extract_hunks(diff_path: Path, fragments_path: Path, out_path: Path) -> None:
    subprocess.run(
        ["python3", str(TRIAGE_DIR / "extract_relevant_hunks.py"),
         str(diff_path), str(fragments_path),
         "--auto", "--stats", "--unmatched",
         "-o", str(out_path)],
        check=True,
    )


# ---------------------------------------------------------------------------
# Step 5: component/team enrichment (optional) + pre-classification
# ---------------------------------------------------------------------------

def enrich_and_pre_classify(df: pd.DataFrame) -> pd.DataFrame:
    cfg        = prompt_helpers.load_triage_config()
    extra_pats = cfg.get("auto_classify_patterns") or {}

    df = df.rename(columns={"testray_component_name": "component_name"}).copy()

    if "team_name" not in df.columns:
        df["team_name"] = None

    df["team_name"] = df["component_name"].apply(
        prompt_helpers.team_for_component
    ).fillna(df["team_name"])

    df["pre_classification"] = df["error_message"].apply(
        lambda e: prompt_helpers.pre_classify(e, extra_pats)
    )

    return df


# ---------------------------------------------------------------------------
# Step 6: artifacts
# ---------------------------------------------------------------------------

RESULTS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "TriageResults",
    "type": "object",
    "required": ["run_id", "classifier", "results"],
    "additionalProperties": False,
    "properties": {
        "run_id":     {"type": "string"},
        "classifier": {"type": "string"},
        "notes":      {"type": "string"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["testray_case_id", "classification", "confidence", "reason"],
                "additionalProperties": False,
                "properties": {
                    "testray_case_id": {"type": "integer"},
                    "classification":  {"enum": ["BUG", "POSSIBLE_BUG", "NEEDS_REVIEW", "FALSE_POSITIVE", "TEST_FIX"]},
                    "confidence":      {"enum": ["high", "medium", "low"]},
                    "culprit_file":    {"type": ["string", "null"]},
                    "specific_change": {"type": ["string", "null"]},
                    "reason":          {"type": "string"},
                },
                "if":   {"properties": {"classification": {"const": "BUG"}}},
                "then": {"required": ["culprit_file"],
                          "properties": {"culprit_file": {"type": "string"}}},
            },
        },
    },
}


def write_results_schema(run_dir: Path) -> None:
    (run_dir / "results.schema.json").write_text(
        json.dumps(RESULTS_SCHEMA, indent=2), encoding="utf-8",
    )


# Subtask-mode results schema: one entry per Testray Subtask, with the
# member case_ids the verdict fans out to. submit.py replicates the verdict
# across every case_id in the array when writing fact_triage_results.
RESULTS_SCHEMA_SUBTASK = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "TriageResultsBySubtask",
    "type": "object",
    "required": ["run_id", "classifier", "results"],
    "additionalProperties": False,
    "properties": {
        "run_id":     {"type": "string"},
        "classifier": {"type": "string"},
        "notes":      {"type": "string"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["subtask_id", "case_ids", "classification",
                             "confidence", "reason"],
                "additionalProperties": False,
                "properties": {
                    "subtask_id":      {"type": ["integer", "null"]},
                    "case_ids":        {"type": "array",
                                          "items": {"type": "integer"},
                                          "minItems": 1},
                    "classification":  {"enum": ["BUG", "POSSIBLE_BUG", "NEEDS_REVIEW",
                                                  "FALSE_POSITIVE", "TEST_FIX"]},
                    "confidence":      {"enum": ["high", "medium", "low"]},
                    "culprit_file":    {"type": ["string", "null"]},
                    "specific_change": {"type": ["string", "null"]},
                    "reason":          {"type": "string"},
                },
                "if":   {"properties": {"classification": {"const": "BUG"}}},
                "then": {"required": ["culprit_file"],
                          "properties": {"culprit_file": {"type": "string"}}},
            },
        },
    },
}


def write_results_schema_subtask(run_dir: Path) -> None:
    (run_dir / "results.schema.json").write_text(
        json.dumps(RESULTS_SCHEMA_SUBTASK, indent=2), encoding="utf-8",
    )


def write_run_yml(run_dir: Path, *, run_id: str,
                  baseline_source: str, target_source: str,
                  build_a: int, build_b: int, hash_a: str, hash_b: str,
                  routine_id: int | None, build_a_name: str, build_b_name: str,
                  classifier: str, total_failures: int, auto_classified: int,
                  flaky_excluded: int, mode: str = MODE_PER_TEST) -> None:
    metadata = {
        "run_id":              run_id,
        "mode":                mode,
        "baseline_source":     baseline_source,
        "target_source":       target_source,
        "classifier":          classifier,
        "build_id_a":          build_a,
        "build_id_b":          build_b,
        "git_hash_a":          hash_a,
        "git_hash_b":          hash_b,
        "routine_id":          routine_id,
        "build_a_name":        build_a_name,
        "build_b_name":        build_b_name,
        "total_failures":      total_failures,
        "auto_classified":     auto_classified,
        "flaky_excluded":      flaky_excluded,
        "prepared_at":         datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    (run_dir / "run.yml").write_text(
        yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Step 7: prompt.md
# ---------------------------------------------------------------------------

PROMPT_HEADER = """# Triage run `{run_id}`

Classify PASSED→FAILED/BLOCKED/UNTESTED test regressions between two builds.
The diff hunks relevant to each failure are already extracted; your job is to
judge whether each failure is caused by a hunk in the diff.

## Context

- **Baseline (A):** {build_a} — `{hash_a_short}` — {build_a_name}
- **Target   (B):** {build_b} — `{hash_b_short}` — {build_b_name}
- **Routine:** {routine_id}
- **Classifier:** `{classifier}`
- **Failures to classify:** {n_to_classify}  (+ {n_auto} auto-classified, + {n_flaky} known-flaky excluded)

## Files in this run

| File | What it is |
|---|---|
| `diff_list.csv` | One row per failure with component/team, error text, linked Jira, and `pre_classification` (non-null = already auto-classified, skip) |
| `hunks.txt` | Git diff filtered to files matching failing tests — your primary evidence |
| `git_diff_full.diff` | Full unfiltered diff — consult if `hunks.txt` looks too narrow |
| `results.schema.json` | JSON schema for the `results.json` you will write |

## Rubric

**Confidence is structural, not metadata.** Your `confidence` field gates which classification you may use:

- **BUG** (confirmed) — only when confidence is **`high`** AND a hunk in the diff (direct or via imports/lifecycle) clearly caused the failure, **and the production change is a genuine defect** (not an intentional change the test merely failed to keep up with — that is TEST_FIX). **MUST name a `culprit_file`.** A linked Jira ticket confirming the regression also qualifies. BUG and POSSIBLE_BUG `culprit_file`s are the labeled defect-attribution training data — only use BUG when you have actually verified the culprit.
- **POSSIBLE_BUG** — exactly **one** plausible diff-caused theory at **`medium`** confidence that you cannot verify to `high` from this prompt: a single changed file (or single ticket cluster) that most likely caused the failure, and it looks like a defect rather than an intentional change. **Name the single candidate in `culprit_file`** (this is what separates POSSIBLE_BUG from NEEDS_REVIEW — a concrete, single attribution). If you have **two or more** competing candidates, that is NEEDS_REVIEW (multi-cause), not POSSIBLE_BUG. If you cannot name any concrete file, that is NEEDS_REVIEW (transitive/low). POSSIBLE_BUG is the "likely a bug, needs a human to confirm the one culprit" tier.
- **TEST_FIX** — the failure **is** caused by the diff, but the production change was **intentional and correct** and only a stale test lags behind it. Tells:
  - the test asserts on a UI label / selector / element / API shape that the diff deliberately changed (e.g. a control changed from a button to a combobox, a label was renamed, an endpoint signature changed),
  - the diff migrated one test layer to the new behavior but left another stale (classic: Playwright updated, legacy Poshi/Selenium selector not), or
  - the fix is to update the test, not to revert or repair production code.
  Do **NOT** name the production file as `culprit_file` — that would mislabel a correct change as a defect (BUG culprit_files feed defect training data). Leave `culprit_file` null (or name the stale **test** file) and describe the required test change in `specific_change`. Use `high` confidence when the intentional-change evidence is in the diff.
- **NEEDS_REVIEW** — the safe default when you can't narrow to a single culprit. Any of:
  - **Two or more candidate causes** — multiple changed files or ticket clusters (LPD/LPP/LPS-XXXXX) in this diff plausibly affect the failing test's space. List ALL candidates separated by `; ` in `specific_change`. Do not pick the most plausible one; the human reviewer disambiguates. (A *single* candidate at medium confidence is POSSIBLE_BUG, not this.)
  - **Transitive / unverifiable** — the failing test plausibly imports, extends, or depends on changed code but you cannot name a concrete single culprit file, or confidence is `low`.
  - The error message is generic enough (e.g. "compileTestIntegrationJava failed", "BUILD FAILED", aggregate batch status, "Failed to run test on CI") that multiple changes in this range could explain it
  - You can see the diff caused it but cannot tell whether the production change or the test is wrong — prefer NEEDS_REVIEW over guessing
  - You'd want a human to confirm before calling it
- **FALSE_POSITIVE** — clearly environmental or genuinely unrelated. May be `high` confidence (timeouts, gradle build infrastructure, chrome version, TEST_SETUP_ERROR are confidently environmental). Common patterns:
  - Environmental (DB, chrome version, CI infra, TEST_SETUP_ERROR, gradle build infrastructure)
  - Timeout/timing tolerance — almost never diff-caused
  - Chronic intermittent (>30% fail rate across recent runs in unrelated builds)
  - No relevant hunk + error unrelated to any changed module **AND** no plausible import / lifecycle / framework dependency

### Transitive dependencies — do not dismiss without verification

Per-failure hunks are matched by path tokens, but **a test class can fail because a file it _imports_ changed, even if the test's own file has no hunk in the diff**. You cannot read source files from this prompt — when:

- the failing test's class name plausibly imports, extends, or depends on code in another changed module,
- multiple commits in this range cluster under the same ticket (e.g. LPD-XXXXX) and touch related infrastructure,
- a smoke test or site-initializer test fails and shared lifecycle / layout / importer code changed,

…**default to NEEDS_REVIEW**, not FALSE_POSITIVE. Note the suspected file in `specific_change` so the human reviewer can verify the import. Do not invent reasons to dismiss — explicit dismissal of a plausible cluster ("the test's name doesn't match the changed file's name") is exactly the failure mode this rule is here to prevent.

### Multiple candidate causes — list, don't pick

If two or more ticket clusters in the diff plausibly affect the failing test's module (e.g. one cluster rewrote the persistence layer the test depends on, a second cluster restructured the test framework or build tooling), **classify NEEDS_REVIEW even at high confidence and list ALL candidates** in `specific_change`, separated by `; `. Locking in a single theory hides the alternatives from the human reviewer; enumerating them lets the reviewer pick. Generic error messages (build failed, compile error, batch failed) are a strong signal that multiple changes could explain the failure.

Rows in `diff_list.csv` with `pre_classification` already set (BUILD_FAILURE, ENV_*, NO_ERROR) are auto-classified upstream and should **not** appear in `results.json`.

## How to classify, per row

1. Read `error_message` in `diff_list.csv`.
2. Scan `hunks.txt` for files whose path contains tokens from `component_name` or `test_case`.
3. If a hunk plausibly causes the error AND it looks like a genuine defect → **BUG**, name `culprit_file` = the specific file path from the diff.
4. If a hunk shows the production change was **intentional** and the test simply asserts on the old behavior (renamed label, changed selector/element/API the diff deliberately changed) → **TEST_FIX**. Leave `culprit_file` null (or name the stale test file); describe the test change in `specific_change`.
5. If a hunk is thematically related but not clearly the cause → **NEEDS_REVIEW**.
6. If no per-failure hunk matches, **check the changed-files manifest and commit cluster sections below** for transitive candidates (test class name → likely importee in a changed module). Note the candidate in `specific_change` and classify NEEDS_REVIEW.
7. If the error is a classic flake pattern (timeout, element-not-present, concurrent-thread assertion, setup error) AND no hunk touches the relevant module AND no transitive candidate exists → **FALSE_POSITIVE**.
8. When the filtered `hunks.txt` seems too narrow, consult `git_diff_full.diff`.

## Output

Write `results.json` in this directory, validating against `results.schema.json`:

```json
{{
  "run_id": "{run_id}",
  "classifier": "{classifier}",
  "results": [
    {{
      "testray_case_id": 12345,
      "classification": "BUG",
      "confidence": "high",
      "culprit_file": "modules/apps/.../Foo.java",
      "specific_change": "Foo.java:42 removed null check in bar()",
      "reason": "Diff removed the null check the test relies on — test asserts behavior when input is null."
    }}
  ]
}}
```

Then submit:

```
testray-analysis submit {run_dir_path}
```

Add `--no-write` to inspect the validated summary without writing the Testray batch.

"""

_FAILURES_HEADER = "\n---\n\n## Failures to classify\n\n"

PROMPT_HEADER_SUBTASK = """# Triage run `{run_id}` — subtask mode

Classify PASSED→FAILED/BLOCKED/UNTESTED test regressions between two builds.
**Unit of analysis: Testray Subtask.** Each block below groups N case results
that share a single error fingerprint (Testray's testflow algorithm); you
write **one verdict per subtask**, and `submit.py` fans the verdict out to
every member case-row in `fact_triage_results`.

## Context

- **Baseline (A):** {build_a} — `{hash_a_short}` — {build_a_name}
- **Target   (B):** {build_b} — `{hash_b_short}` — {build_b_name}
- **Routine:** {routine_id}
- **Classifier:** `{classifier}`
- **Subtasks to classify:** {n_subtasks}  (covering {n_member_cases} case results;
  + {n_auto} auto-classified, + {n_flaky} known-flaky excluded)

## Files in this run

| File | What it is |
|---|---|
| `diff_list.csv` | One row per failure (case-grain) — same as per-test mode |
| `diff_list_subtasks.csv` | One row per subtask group — `subtask_id`, `case_count`, `member_case_ids`, shared `error`, `pre_classification` if every member auto-classified |
| `hunks.txt` | Git diff filtered to files matching failing tests — your primary evidence |
| `git_diff_full.diff` | Full unfiltered diff — consult if `hunks.txt` looks too narrow |
| `results.schema.json` | JSON schema for the `results.json` you will write (subtask-mode shape) |

## Rubric

Same rubric as per-test mode, applied at the subtask level — write one verdict per group:

- **BUG** (confirmed) — only when confidence is **`high`** AND a hunk in the diff (direct or via imports/lifecycle) clearly caused the *shared* error across all members **and the production change is a genuine defect** (not an intentional change the tests merely lag behind — that is TEST_FIX). **MUST name a `culprit_file`.** BUG and POSSIBLE_BUG culprit_files feed defect-attribution training data — only use BUG when the culprit is actually verified.
- **POSSIBLE_BUG** — exactly **one** plausible diff-caused theory at **`medium`** confidence for the shared error that you cannot verify to `high`: a single changed file (or single ticket cluster) that most likely caused it, looking like a defect rather than an intentional change. **Name the single candidate in `culprit_file`.** Two or more competing candidates → NEEDS_REVIEW (multi-cause). No concrete file → NEEDS_REVIEW (transitive/low).
- **TEST_FIX** — the shared failure **is** diff-caused, but the production change was **intentional and correct** and the member tests simply assert on the old behavior (renamed label, changed selector/element/API the diff deliberately changed; or one test layer was migrated and a legacy one left stale). The fix is to update the tests, not production. Do **NOT** name the production file as `culprit_file` (that mislabels a correct change as a defect); leave it null or name the stale test, and describe the test change in `specific_change`.
- **NEEDS_REVIEW** — the safe default when you can't narrow to a single culprit. Any of:
  - **Two or more candidate causes** — multiple changed files / ticket clusters (LPD/LPP/LPS-XXXXX) plausibly affect this group's space — list ALL candidates separated by `; ` in `specific_change`. (A *single* candidate at medium confidence is POSSIBLE_BUG.)
  - **Transitive / unverifiable** — members plausibly import/extend/depend on changed code but you cannot name a concrete single culprit file, or confidence is `low`
  - The error is generic enough (build failed, batch failed, "Failed prior to running test") that multiple changes could explain it
  - You'd want a human to confirm before calling it
- **FALSE_POSITIVE** — clearly environmental or genuinely unrelated. May be `high` confidence (timeouts, gradle build infrastructure, TEST_SETUP_ERROR, Poshi `ElementNotFoundPoshiRunnerException`, Selenium `NoSuchElementException`). The fact that one verdict covers many tests makes the rubric *more* useful here, not less — a Poshi flake pattern is still a Poshi flake pattern when 30 tests share it.

### Transitive dependencies — do not dismiss without verification

A subtask can fail because a file the member tests _import_ changed, even if no member test's file has a direct hunk in the diff. When members plausibly import, extend, or depend on code in another changed module, default to NEEDS_REVIEW. Note the suspected file in `specific_change`.

### Multiple candidate causes — list, don't pick

If two or more ticket clusters in the diff plausibly affect the group's space, classify NEEDS_REVIEW (even at high confidence) and list ALL candidates in `specific_change`, separated by `; `.

Subtasks where every member already has `pre_classification` set are auto-classified upstream and **must not** appear in `results.json` — they are listed in this prompt for traceability only.

## How to classify, per subtask

1. Read the **shared error** at the top of the subtask block — it's the same error string Testray clustered all member case-results under.
2. Scan `hunks.txt` for files whose path contains tokens from any member's `test_case` or `component_name`. The matched hunks for representative members are embedded inline.
3. Hunk plausibly causes the *shared* error as a genuine defect → **BUG**, name `culprit_file` = the specific file path from the diff.
4. Hunk shows the production change was **intentional** and the tests assert on old behavior → **TEST_FIX** (culprit_file null or the stale test; describe the test change in `specific_change`).
5. Hunk thematically related but not the clear cause → **NEEDS_REVIEW**.
6. No per-member hunk matches, **check the changed-files manifest and commit cluster sections below** for transitive candidates (member class names → likely importees in changed modules). Note the candidate in `specific_change` and classify NEEDS_REVIEW.
7. Classic flake pattern (timeout, element-not-present, concurrent-thread assertion, TEST_SETUP_ERROR) AND no hunk touches a relevant module AND no transitive candidate → **FALSE_POSITIVE**.
8. When the filtered `hunks.txt` seems too narrow, consult `git_diff_full.diff`.

## Output

Write `results.json` in this directory, validating against `results.schema.json`. **One entry per subtask** (not per case):

```json
{{
  "run_id": "{run_id}",
  "classifier": "{classifier}",
  "results": [
    {{
      "subtask_id": 469572218,
      "case_ids": [65638, 65644, 65650],
      "classification": "FALSE_POSITIVE",
      "confidence": "high",
      "reason": "Classic Poshi ElementNotFoundPoshiRunnerException — selector/timing flake. No hunks touch the cookies-banner selectors involved."
    }},
    {{
      "subtask_id": null,
      "case_ids": [12345],
      "classification": "BUG",
      "confidence": "high",
      "culprit_file": "modules/apps/.../Foo.java",
      "specific_change": "Foo.java:42 removed null check in bar()",
      "reason": "Diff removed the null check the test relies on."
    }}
  ]
}}
```

`subtask_id` may be `null` for cases the testflow didn't group (one-off failures); `case_ids` always lists every member that should inherit this verdict.

Then submit:

```
testray-analysis submit {run_dir_path}
```

Add `--no-write` to inspect the validated summary without writing the Testray batch.

"""

_DIFF_HDR_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_LPD_RE      = re.compile(r"\b((?:LPD|LPP|LPS)-\d+)\b")


def _module_key(path: str) -> str:
    """Group key for the file manifest. modules/<group>/<module>/... → that
    module folder; other paths fall back to the top two segments."""
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "modules" and parts[1] in ("apps", "dxp", "test"):
        return "/".join(parts[:3]) + "/"
    if len(parts) >= 2:
        return "/".join(parts[:2]) + "/"
    return parts[0]


def parse_full_diff_manifest(diff_path: Path) -> dict[str, int]:
    """Walk git_diff_full.diff once and return {file_path: lines_changed}.
    Counts +/- lines (mirrors `git diff --stat`)."""
    files: dict[str, int] = {}
    current: str | None = None
    count = 0
    if not diff_path.exists():
        return files
    for line in diff_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _DIFF_HDR_RE.match(line)
        if m:
            if current is not None:
                files[current] = count
            current = m.group(2)
            count = 0
            continue
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            count += 1
    if current is not None:
        files[current] = count
    return files


def fetch_commits_in_range(git_repo: Path, hash_a: str, hash_b: str) -> list[tuple[str, str]]:
    """Run `git log A..B --pretty=format:'%h\\t%s'` in the liferay-portal
    repo. Returns [(short_hash, subject), ...] in newest-first order."""
    if not (git_repo / ".git").is_dir():
        return []
    try:
        out = subprocess.run(
            ["git", "-C", str(git_repo), "log",
             "--pretty=format:%h\t%s", f"{hash_a}..{hash_b}"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    commits = []
    for line in out.splitlines():
        if "\t" in line:
            h, subj = line.split("\t", 1)
            if _is_noise_commit_subject(subj):
                continue
            commits.append((h, subj))
    return commits


def fetch_commits_for_file(git_repo: Path, hash_a: str, hash_b: str,
                           file_path: str) -> list[tuple[str, str]]:
    """Commits in A..B that touched `file_path`, newest-first, noise-filtered.

    Used as a fallback when ticket-based attribution finds nothing but a
    verdict names a culprit_file. Returns [(short_hash, subject), ...];
    empty list on any failure (attribution is purely additive)."""
    if not (file_path and (git_repo / ".git").is_dir()):
        return []
    try:
        out = subprocess.run(
            ["git", "-C", str(git_repo), "log",
             "--pretty=format:%h\t%s", f"{hash_a}..{hash_b}",
             "--", file_path],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    commits = []
    for line in out.splitlines():
        if "\t" in line:
            h, subj = line.split("\t", 1)
            if _is_noise_commit_subject(subj):
                continue
            commits.append((h, subj))
    return commits


def render_changed_files_section(manifest: dict[str, int]) -> list[str]:
    """Markdown block listing every changed file by module folder, sorted
    by total lines descending. Gives the model a manifest for transitive-dep
    inference when per-failure hunk matching is empty."""
    if not manifest:
        return []
    by_module: dict[str, list[tuple[str, int]]] = {}
    for path, lc in manifest.items():
        by_module.setdefault(_module_key(path), []).append((path, lc))
    total_files = len(manifest)
    total_lines = sum(manifest.values())

    lines = [
        "## All changed files in this diff",
        "",
        f"_{total_files} files, {total_lines} +/- lines, grouped by module. "
        f"Use this as a manifest of what changed across the diff. If a "
        f"failing test plausibly imports or extends a file shown here that "
        f"is **not** in its per-failure hunks above, treat it as a candidate "
        f"culprit and classify NEEDS_REVIEW (not FALSE_POSITIVE) — note the "
        f"candidate path in `specific_change`._",
        "",
    ]
    for mod in sorted(by_module, key=lambda m: -sum(lc for _, lc in by_module[m])):
        files = sorted(by_module[mod], key=lambda x: -x[1])
        mod_total = sum(lc for _, lc in files)
        lines.append(f"### {mod} ({mod_total} lines, {len(files)} file(s))")
        for path, lc in files:
            lines.append(f"- `{path}` ({lc})")
        lines.append("")
    lines.append("---")
    lines.append("")
    return lines


def render_commits_section(commits: list[tuple[str, str]]) -> list[str]:
    """Markdown block listing commits in this range, clustered by ticket
    (LPD-XXXXX / LPP-XXXXX / LPS-XXXXX) when present. Multi-commit clusters
    under one ticket often represent a single refactor — explicit candidate
    root causes for transitive-dep failures."""
    if not commits:
        return []
    by_ticket: dict[str, list[tuple[str, str]]] = {}
    for h, subj in commits:
        m = _LPD_RE.search(subj)
        key = m.group(1) if m else "(no ticket)"
        by_ticket.setdefault(key, []).append((h, subj))

    lines = [
        "## Commits in this range",
        "",
        f"_{len(commits)} commits between baseline and target. Multi-commit "
        f"clusters under the same ticket often represent a single refactor — "
        f"if a ticket touches a file related to a failing test (even via "
        f"imports), treat the cluster as a candidate root cause._",
        "",
    ]
    # Clusters with most commits first; "(no ticket)" last.
    def sort_key(t: str) -> tuple[int, int, str]:
        return (1 if t == "(no ticket)" else 0, -len(by_ticket[t]), t)
    for ticket in sorted(by_ticket, key=sort_key):
        cs = by_ticket[ticket]
        if len(cs) > 1:
            lines.append(f"### {ticket} ({len(cs)} commits)")
        else:
            lines.append(f"### {ticket}")
        for h, subj in cs:
            lines.append(f"- `{h}` {subj}")
        lines.append("")
    lines.append("---")
    lines.append("")
    return lines


def write_prompt(run_dir: Path, *, run_id: str, classifier: str,
                 build_a: int, build_b: int, hash_a: str, hash_b: str,
                 routine_id: int | None, build_a_name: str, build_b_name: str,
                 df_to_classify: pd.DataFrame, df_auto: pd.DataFrame,
                 df_flaky: pd.DataFrame, hunks_path: Path,
                 full_diff_path: Path, git_repo: Path) -> None:

    try:
        diff_blocks = prompt_helpers.parse_diff_blocks(hunks_path)
    except FileNotFoundError:
        diff_blocks = {}

    chrome_changes = prompt_helpers.find_ui_chrome_changes(diff_blocks)

    chrome_lines: list[str] = []
    if chrome_changes:
        chrome_lines.append("## UI chrome changes")
        chrome_lines.append("")
        chrome_lines.append(
            "Files changed in shared layout / navigation / taglib / theme "
            "paths — these can break UI tests in *other* components (the "
            "failing test's component won't show a matching hunk). Cross-"
            "reference against per-failure sections below when the error "
            "is UI-shaped (strict mode violation, element-not-found, "
            "visibility timeout, getByText not found)."
        )
        chrome_lines.append("")
        chrome_lines.append(f"_{len(chrome_changes)} shared-UI files changed. "
                            f"Sorted by change size, smallest last._")
        chrome_lines.append("")
        chrome_lines.append("| Changed lines | File |")
        chrome_lines.append("|---:|---|")
        for path, n in chrome_changes:
            chrome_lines.append(f"| {n} | `{path}` |")
        chrome_lines.append("")
        chrome_lines.append("---")
        chrome_lines.append("")

    has_chrome = bool(chrome_changes)
    body_lines: list[str] = []
    for i, (_, row) in enumerate(df_to_classify.iterrows(), start=1):
        short = prompt_helpers.shorten_test_name(str(row.get("test_case") or ""))
        component = row.get("component_name") or "Unknown"
        team      = row.get("team_name") or ""
        case_id   = row.get("testray_case_id")

        header = f"### {i}. `{short}`"
        meta   = f"**case_id:** {case_id} · **component:** {component}"
        if team:
            meta += f" ({team})"
        meta += f" · **status_b:** {row.get('status_b', 'FAILED')}"
        body_lines.append(header)
        body_lines.append(meta)

        if row.get("linked_issues") and not pd.isna(row.get("linked_issues")):
            body_lines.append(f"**jira:** {row['linked_issues']}")

        err = str(row.get("error_message") or "")[:500].replace("\n", " ")
        body_lines.append(f"**error:** {err}")
        body_lines.append("")

        blocks = prompt_helpers.find_diff_blocks(
            test_case=str(row.get("test_case") or ""),
            component_name=row.get("component_name"),
            matched_diff_files=None,
            diff_blocks=diff_blocks,
        )
        if blocks:
            for fp, hunk in blocks:
                body_lines.append(f"```diff")
                body_lines.append(hunk)
                body_lines.append("```")
                body_lines.append("")
        else:
            if has_chrome:
                body_lines.append(
                    "_No direct hunk match by path. If the error is UI-shaped "
                    "(strict mode violation, element-not-found, visibility "
                    "timeout), cross-check the **UI chrome changes** section "
                    "at the top — a shared layout or navigation file may be "
                    "the real culprit even though it's in a different "
                    "component. Consult `git_diff_full.diff` to confirm._"
                )
            else:
                body_lines.append(
                    "_No diff hunk matched by path heuristics. Before "
                    "concluding FALSE_POSITIVE, scan the **All changed files** "
                    "and **Commits in this range** sections below for "
                    "transitive candidates — this test class may import or "
                    "extend code in a different changed module. If you find "
                    "a plausible candidate you cannot fully verify from "
                    "this prompt alone, classify NEEDS_REVIEW with the "
                    "suspected file in `specific_change`._"
                )
            body_lines.append("")

        body_lines.append("---")
        body_lines.append("")

    header = PROMPT_HEADER.format(
        run_id=run_id,
        classifier=classifier,
        build_a=build_a, build_b=build_b,
        hash_a_short=hash_a[:12] if hash_a else "?",
        hash_b_short=hash_b[:12] if hash_b else "?",
        routine_id=routine_id if routine_id is not None else "unknown",
        build_a_name=build_a_name,
        build_b_name=build_b_name,
        n_to_classify=len(df_to_classify),
        n_auto=len(df_auto),
        n_flaky=len(df_flaky),
        run_dir_name=run_dir.name,
        run_dir_path=str(run_dir),
    )

    # Manifest + commits sections — give the model context for transitive
    # deps when per-failure hunk matching is empty.
    manifest = parse_full_diff_manifest(full_diff_path)
    commits  = fetch_commits_in_range(git_repo, hash_a, hash_b) if hash_a and hash_b else []
    manifest_lines = render_changed_files_section(manifest)
    commit_lines   = render_commits_section(commits)

    parts = [header]
    if chrome_lines:
        parts.append("\n".join(chrome_lines))
    if manifest_lines:
        parts.append("\n".join(manifest_lines))
    if commit_lines:
        parts.append("\n".join(commit_lines))
    parts.append(_FAILURES_HEADER)
    parts.append("\n".join(body_lines))
    (run_dir / "prompt.md").write_text("".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# Subtask mode — group regressions by Testray Subtask, write per-subtask
# bundle artifacts (diff_list_subtasks.csv + per-subtask prompt blocks).
# ---------------------------------------------------------------------------

def compute_subtask_groups(df: pd.DataFrame) -> list[dict]:
    """Group regression cases by `subtask_id`. Cases without a subtask link
    (subtask_id 0 / NaN — common when the testflow didn't cluster them or
    the build pre-dates testflow) become singleton groups so nothing is
    silently dropped.

    Returns a list of dicts, one per group:
        subtask_id      — int Testray subtask id, or None if unmapped
        case_ids        — [int, ...]
        test_cases      — [str, ...]
        components      — [str, ...] unique
        shared_error    — most common error string across members
        all_errors      — set of distinct error strings (size > 1 means
                          the group has internal error variation; usually
                          stays 1 since Testray groups by error fingerprint)
        linked_issues   — [str, ...] unique non-empty
        size            — len(case_ids)
        all_pre_classified — bool: every member already auto-classified
        pre_classifications — set of pre_classification labels seen
        any_known_flaky — bool: at least one member is known_flaky
        all_known_flaky — bool: every member is known_flaky
        status_b_breakdown — Counter of status_b across members
    """
    if df.empty or "subtask_id" not in df.columns:
        return []

    work = df.copy()
    # Treat 0/NaN as "no subtask link" → assign each such case its own
    # synthetic group key so they don't collide.
    work["_grp_key"] = work["subtask_id"].fillna(0).astype("int64")
    next_synth = -1
    for idx in work.index:
        if int(work.at[idx, "_grp_key"]) == 0:
            work.at[idx, "_grp_key"] = next_synth
            next_synth -= 1

    groups: list[dict] = []
    for grp_key, sub in work.groupby("_grp_key", sort=False):
        # Real subtask_ids are positive Testray ids; synthetic keys are
        # negative and represent unmapped singletons (subtask_id = None).
        real_sid = int(grp_key) if grp_key > 0 else None
        errors_list = [e for e in sub["error_message"].fillna("") if e]
        err_counts = Counter(errors_list)
        shared_error = err_counts.most_common(1)[0][0] if err_counts else ""

        components = sorted({c for c in sub.get("component_name", pd.Series([])).fillna("") if c}) \
                   or sorted({c for c in sub.get("testray_component_name", pd.Series([])).fillna("") if c})
        jiras = sorted({j for j in sub["linked_issues"].fillna("") if j})

        pre_set = {p for p in sub["pre_classification"].fillna("") if p}
        all_auto  = bool(pre_set) and sub["pre_classification"].notna().all()
        flaky_col = sub["known_flaky"].fillna(False).astype(bool)

        groups.append({
            "subtask_id":          real_sid,
            "case_ids":            [int(x) for x in sub["testray_case_id"].tolist()],
            "test_cases":          [str(x) if pd.notna(x) else "" for x in sub["test_case"].tolist()],
            "components":          components,
            "shared_error":        shared_error,
            "all_errors":          set(err_counts.keys()),
            "linked_issues":       jiras,
            "size":                len(sub),
            "all_pre_classified":  all_auto,
            "pre_classifications": pre_set,
            "any_known_flaky":     bool(flaky_col.any()),
            "all_known_flaky":     bool(flaky_col.all()),
            "status_b_breakdown":  Counter(sub["status_b"].fillna("FAILED").tolist()),
        })

    # Sort: classifiable groups (not all-flaky, not all-auto) first by size desc;
    # auto and flaky-only groups at the end.
    def sort_key(g):
        skip_pri = (1 if g["all_known_flaky"] else 0,
                    1 if g["all_pre_classified"] else 0)
        return (skip_pri, -g["size"])
    groups.sort(key=sort_key)
    return groups


def write_diff_list_subtasks(run_dir: Path, groups: list[dict]) -> None:
    """One row per subtask group. Member case-ids are joined with `|` so the
    file stays CSV-readable; submit.py parses them back.

    `bucket` marks how _finalize_bundle categorized the group:
      classifiable / auto-only / flaky-only. Subtasks with mixed members
      (some classifiable + some auto + some flaky) land in `classifiable` —
      the verdict covers all member case-rows, with submit.py giving
      pre-classified members AUTO_CLASSIFIED and dropping flaky members."""
    rows = []
    for g in groups:
        if g["all_known_flaky"]:
            bucket = "flaky-only"
        elif g["all_pre_classified"]:
            bucket = "auto-only"
        else:
            bucket = "classifiable"
        rows.append({
            "subtask_id":         g["subtask_id"] if g["subtask_id"] is not None else "",
            "case_count":         g["size"],
            "bucket":             bucket,
            "member_case_ids":    "|".join(str(c) for c in g["case_ids"]),
            "member_test_cases":  "|".join(g["test_cases"]),
            "components":         "|".join(g["components"]),
            "shared_error":       g["shared_error"],
            "linked_issues":      "|".join(g["linked_issues"]),
            "any_known_flaky":    g["any_known_flaky"],
            "all_known_flaky":    g["all_known_flaky"],
            "all_pre_classified": g["all_pre_classified"],
            "pre_classifications": "|".join(sorted(g["pre_classifications"])),
            "status_b_breakdown": "|".join(f"{k}={v}" for k, v in g["status_b_breakdown"].items()),
        })
    pd.DataFrame(rows).to_csv(run_dir / "diff_list_subtasks.csv", index=False)


def write_prompt_subtask(run_dir: Path, *, run_id: str, classifier: str,
                          build_a: int, build_b: int, hash_a: str, hash_b: str,
                          routine_id: int | None, build_a_name: str, build_b_name: str,
                          groups_to_classify: list[dict],
                          groups_auto: list[dict],
                          groups_flaky: list[dict],
                          n_member_cases: int,
                          n_auto_cases: int, n_flaky_cases: int,
                          hunks_path: Path,
                          full_diff_path: Path, git_repo: Path) -> None:
    """Subtask-mode prompt. Same chrome / manifest / commits sections as
    write_prompt; per-failure body is one block per subtask group."""

    try:
        diff_blocks = prompt_helpers.parse_diff_blocks(hunks_path)
    except FileNotFoundError:
        diff_blocks = {}

    chrome_changes = prompt_helpers.find_ui_chrome_changes(diff_blocks)

    chrome_lines: list[str] = []
    if chrome_changes:
        chrome_lines.append("## UI chrome changes")
        chrome_lines.append("")
        chrome_lines.append(
            "Files changed in shared layout / navigation / taglib / theme "
            "paths — these can break UI tests in *other* components. Cross-"
            "reference against per-subtask sections below when the shared "
            "error is UI-shaped (strict mode violation, element-not-found, "
            "visibility timeout, getByText not found)."
        )
        chrome_lines.append("")
        chrome_lines.append(f"_{len(chrome_changes)} shared-UI files changed. "
                            f"Sorted by change size, smallest last._")
        chrome_lines.append("")
        chrome_lines.append("| Changed lines | File |")
        chrome_lines.append("|---:|---|")
        for path, n in chrome_changes:
            chrome_lines.append(f"| {n} | `{path}` |")
        chrome_lines.append("")
        chrome_lines.append("---")
        chrome_lines.append("")

    has_chrome = bool(chrome_changes)
    body_lines: list[str] = []

    def render_group(idx: int, g: dict, *, kind: str) -> None:
        """kind: 'classify' | 'auto' | 'flaky'."""
        sid = g["subtask_id"]
        sid_label = f"subtask_id={sid}" if sid is not None else "no subtask link (singleton)"
        members_label = f"{g['size']} case(s)"
        header = f"### {idx}. Subtask {sid_label} — {members_label}"
        body_lines.append(header)

        meta_parts = [f"**case_ids:** {', '.join(str(c) for c in g['case_ids'][:8])}"]
        if g["size"] > 8:
            meta_parts[-1] += f" (+ {g['size'] - 8} more)"
        if g["components"]:
            meta_parts.append(f"**components:** {', '.join(g['components'][:5])}")
        meta_parts.append(f"**status_b:** {dict(g['status_b_breakdown'])}")
        body_lines.append(" · ".join(meta_parts))

        if g["linked_issues"]:
            body_lines.append(f"**jira:** {', '.join(g['linked_issues'][:5])}")

        err = (g["shared_error"] or "")[:600].replace("\n", " ")
        body_lines.append(f"**shared_error:** {err}")

        if kind == "auto":
            body_lines.append(f"_All members already auto-classified upstream "
                              f"({', '.join(sorted(g['pre_classifications']))}). "
                              f"Listed for traceability — do NOT write a results.json entry for this subtask._")
            body_lines.append("")
            body_lines.append("---")
            body_lines.append("")
            return
        if kind == "flaky":
            body_lines.append(f"_All members marked known_flaky upstream and "
                              f"will be excluded from fact_triage_results. "
                              f"Listed for traceability — do NOT write a results.json entry for this subtask._")
            body_lines.append("")
            body_lines.append("---")
            body_lines.append("")
            return

        # Member list
        body_lines.append("")
        body_lines.append("**members:**")
        for cid, tc in zip(g["case_ids"][:12], g["test_cases"][:12]):
            short = prompt_helpers.shorten_test_name(str(tc or ""))
            body_lines.append(f"- [{cid}] `{short}`")
        if g["size"] > 12:
            body_lines.append(f"- _… and {g['size'] - 12} more — see diff_list_subtasks.csv_")
        body_lines.append("")

        # Hunks: union of per-member fragment matches across the group's
        # representative members. Cap to avoid bloating the prompt.
        seen_files: set[str] = set()
        union_blocks: list[tuple[str, str]] = []
        for tc in g["test_cases"][:6]:
            if not tc:
                continue
            blocks = prompt_helpers.find_diff_blocks(
                test_case=str(tc),
                component_name=(g["components"][0] if g["components"] else None),
                matched_diff_files=None,
                diff_blocks=diff_blocks,
            )
            for fp, hunk in blocks:
                if fp in seen_files:
                    continue
                seen_files.add(fp)
                union_blocks.append((fp, hunk))
                if len(union_blocks) >= 8:
                    break
            if len(union_blocks) >= 8:
                break

        if union_blocks:
            for fp, hunk in union_blocks:
                body_lines.append(f"```diff")
                body_lines.append(hunk)
                body_lines.append("```")
                body_lines.append("")
        else:
            if has_chrome:
                body_lines.append(
                    "_No direct hunk match by path for any member. If the "
                    "shared error is UI-shaped, cross-check the **UI chrome "
                    "changes** section at the top — a shared layout or "
                    "navigation file may be the real culprit even though no "
                    "member's component matches. Consult `git_diff_full.diff` "
                    "to confirm._"
                )
            else:
                body_lines.append(
                    "_No diff hunk matched by path heuristics. Before "
                    "concluding FALSE_POSITIVE, scan the **All changed files** "
                    "and **Commits in this range** sections below for "
                    "transitive candidates — member tests may import or extend "
                    "code in a different changed module. If you find a "
                    "plausible candidate you cannot fully verify from this "
                    "prompt alone, classify NEEDS_REVIEW with the suspected "
                    "file in `specific_change`._"
                )
            body_lines.append("")

        body_lines.append("---")
        body_lines.append("")

    idx = 0
    for g in groups_to_classify:
        idx += 1
        render_group(idx, g, kind="classify")

    if groups_auto:
        body_lines.append("\n## Auto-classified subtasks (do NOT write results.json entries)\n")
        for g in groups_auto:
            idx += 1
            render_group(idx, g, kind="auto")

    if groups_flaky:
        body_lines.append("\n## Flaky-only subtasks (excluded from fact_triage_results)\n")
        for g in groups_flaky:
            idx += 1
            render_group(idx, g, kind="flaky")

    header = PROMPT_HEADER_SUBTASK.format(
        run_id=run_id,
        classifier=classifier,
        build_a=build_a, build_b=build_b,
        hash_a_short=hash_a[:12] if hash_a else "?",
        hash_b_short=hash_b[:12] if hash_b else "?",
        routine_id=routine_id if routine_id is not None else "unknown",
        build_a_name=build_a_name,
        build_b_name=build_b_name,
        n_subtasks=len(groups_to_classify),
        n_member_cases=n_member_cases,
        n_auto=n_auto_cases,
        n_flaky=n_flaky_cases,
        run_dir_name=run_dir.name,
        run_dir_path=str(run_dir),
    )

    manifest = parse_full_diff_manifest(full_diff_path)
    commits  = fetch_commits_in_range(git_repo, hash_a, hash_b) if hash_a and hash_b else []
    manifest_lines = render_changed_files_section(manifest)
    commit_lines   = render_commits_section(commits)

    parts = [header]
    if chrome_lines:
        parts.append("\n".join(chrome_lines))
    if manifest_lines:
        parts.append("\n".join(manifest_lines))
    if commit_lines:
        parts.append("\n".join(commit_lines))
    parts.append(_FAILURES_HEADER)
    parts.append("\n".join(body_lines))
    (run_dir / "prompt.md").write_text("".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def validate_combo(baseline: SideSpec, target: SideSpec) -> None:
    """api-only: both sides use the Testray REST fetch, which shares case_id as
    the join key, so all pairs are valid."""
    return


def validate_mode(baseline: SideSpec, target: SideSpec, mode: str) -> None:
    """Subtask mode reads r_subtaskToCaseResults_c_subtaskId from the Testray
    caseresult object. api-only mode always fetches via the api, so this is
    always satisfied — kept as a harmless guard."""
    return


def _finalize_bundle(
    df: pd.DataFrame, run_id: str, run_dir: Path,
    classifier: str,
    baseline_source: str, target_source: str,
    build_a: int, build_b: int, hash_a: str, hash_b: str,
    routine_id: int | None, build_a_name: str, build_b_name: str,
    git_repo: Path,
    mode: str = MODE_PER_TEST,
    fetch_specs: list | None = None,
) -> Path:
    print(f"→ Step 3/6 git diff …")
    diff_path = run_dir / "git_diff_full.diff"
    diff_lines = run_git_diff(git_repo, hash_a, hash_b, diff_path,
                              fetch_specs=fetch_specs)
    print(f"   {diff_lines} lines → {_disp(diff_path)}")

    print(f"→ Step 4/6 fragments + filtered hunks …")
    fragments = derive_test_fragments(df)
    fragments_path = run_dir / "test_fragments.txt"
    fragments_path.write_text("\n".join(sorted(fragments)), encoding="utf-8")
    hunks_path = run_dir / "hunks.txt"
    if fragments:
        run_extract_hunks(diff_path, fragments_path, hunks_path)
        print(f"   {len(fragments)} fragments → {_disp(hunks_path)}")
    else:
        # Happens when neither side carries case_name (e.g. api × api): no
        # tokens to narrow the diff. Fall back to the full diff; classify
        # by reading hunks.txt directly.
        hunks_path.write_bytes(diff_path.read_bytes())
        print(f"   WARNING: no test_case fragments (both sides lack case_name). "
              f"Copying full diff → hunks.txt unfiltered.", file=sys.stderr)

    print(f"→ Step 5/6 enrich + pre-classify …")
    df = enrich_and_pre_classify(df)
    df = df.drop_duplicates(subset="testray_case_id", keep="first").reset_index(drop=True)
    df_flaky    = df[df["known_flaky"].fillna(False)].copy()
    df_nonflaky = df[~df["known_flaky"].fillna(False)].copy()
    df_auto     = df_nonflaky[df_nonflaky["pre_classification"].notna()].copy()
    df_to_cls   = df_nonflaky[df_nonflaky["pre_classification"].isna()].copy()
    print(f"   {len(df)} unique cases: "
          f"{len(df_to_cls)} to classify, {len(df_auto)} auto, "
          f"{len(df_flaky)} flaky (excluded)")

    diff_list_cols = [
        "testray_case_id", "test_case", "component_name", "team_name",
        "status_a", "status_b", "known_flaky", "linked_issues",
        "error_message", "pre_classification",
    ]
    # Carries the TriageResult FK through to submit — absent only when the
    # target side had no caseresult ids, in which case verdicts write unlinked.
    if "caseresult_id" in df.columns:
        diff_list_cols.append("caseresult_id")
    if "subtask_id" in df.columns:
        diff_list_cols.append("subtask_id")
    df[diff_list_cols].to_csv(run_dir / "diff_list.csv", index=False)

    print(f"→ Step 6/6 prompt + schema + run.yml …")

    if mode == MODE_BY_SUBTASK:
        if "subtask_id" not in df.columns:
            raise SystemExit(
                "Internal error: --by-subtask was requested but no subtask_id "
                "column reached _finalize_bundle. fetch_build_caseresults_api "
                "or compute_test_diff failed to propagate it. Re-check the "
                "target source — it must be api."
            )
        # Group on the FULL df (all regressions) so each subtask appears
        # exactly once. Then categorize each group by member composition:
        #   - all-flaky          → flaky-only bucket (traceability)
        #   - all-auto-classified → auto bucket (traceability)
        #   - has any classifiable member → classifiable bucket
        # A subtask with a mix of classifiable + auto members lands in the
        # classifiable bucket; submit.py and assemble_dataframe_subtask
        # handle the per-member differentiation.
        all_groups      = compute_subtask_groups(df)
        groups_to_cls:  list[dict] = []
        groups_auto:    list[dict] = []
        groups_flaky:   list[dict] = []
        for g in all_groups:
            if g["all_known_flaky"]:
                groups_flaky.append(g)
            elif g["all_pre_classified"]:
                groups_auto.append(g)
            else:
                groups_to_cls.append(g)
        write_diff_list_subtasks(run_dir, all_groups)
        write_results_schema_subtask(run_dir)
        write_prompt_subtask(
            run_dir,
            run_id=run_id, classifier=classifier,
            build_a=build_a, build_b=build_b,
            hash_a=hash_a, hash_b=hash_b,
            routine_id=routine_id,
            build_a_name=build_a_name, build_b_name=build_b_name,
            groups_to_classify=groups_to_cls,
            groups_auto=groups_auto,
            groups_flaky=groups_flaky,
            n_member_cases=sum(g["size"] for g in groups_to_cls),
            n_auto_cases=len(df_auto),
            n_flaky_cases=len(df_flaky),
            hunks_path=hunks_path,
            full_diff_path=diff_path,
            git_repo=git_repo,
        )
        print(f"   subtask groups: {len(groups_to_cls)} to classify "
              f"(covering {sum(g['size'] for g in groups_to_cls)} cases), "
              f"{len(groups_auto)} auto-only, {len(groups_flaky)} flaky-only")
    else:
        write_results_schema(run_dir)
        write_prompt(
            run_dir,
            run_id=run_id, classifier=classifier,
            build_a=build_a, build_b=build_b,
            hash_a=hash_a, hash_b=hash_b,
            routine_id=routine_id,
            build_a_name=build_a_name, build_b_name=build_b_name,
            df_to_classify=df_to_cls, df_auto=df_auto, df_flaky=df_flaky,
            hunks_path=hunks_path,
            full_diff_path=diff_path,
            git_repo=git_repo,
        )

    write_run_yml(
        run_dir,
        run_id=run_id,
        baseline_source=baseline_source, target_source=target_source,
        classifier=classifier,
        build_a=build_a, build_b=build_b,
        hash_a=hash_a, hash_b=hash_b,
        routine_id=routine_id,
        build_a_name=build_a_name, build_b_name=build_b_name,
        total_failures=len(df),
        auto_classified=len(df_auto),
        flaky_excluded=len(df_flaky),
        mode=mode,
    )

    rel = _disp(run_dir)
    print(f"\nRun bundle ready: {rel}")
    print(f"Next:  testray-analysis classify {rel}")
    print(f"       (or classify {rel}/prompt.md by hand in a Claude Code session)")
    print(f"Then:  testray-analysis submit {rel}")
    return run_dir


def prepare(baseline: SideSpec, target: SideSpec, classifier: str,
            mode: str = MODE_PER_TEST, out_dir: Path | None = None,
            fetch_specs: list | None = None) -> Path:
    validate_combo(baseline, target)
    validate_mode(baseline, target, mode)

    cfg         = load_config()
    # Testray connection settings: environment variables OVERRIDE config.yml, so
    # secrets can live in the shell / Jenkins credentials rather than the file
    # (mirrors ANTHROPIC_API_KEY). Also the easy way to switch base_url per run.
    tr = cfg.setdefault("testray", {})
    for _key, _env in (("base_url", "TESTRAY_BASE_URL"),
                       ("client_id", "TESTRAY_CLIENT_ID"),
                       ("client_secret", "TESTRAY_CLIENT_SECRET")):
        if os.environ.get(_env):
            tr[_key] = os.environ[_env]
    git_repo    = Path(cfg["git"]["repo_path"]).expanduser()

    ts      = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    run_id  = f"r_{ts}_{baseline.build_id}_{target.build_id}"
    runs_root = (out_dir or (Path.cwd() / DEFAULT_RUNS_DIRNAME)).expanduser().resolve()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"→ Step 1/6 test_diff "
          f"(baseline={baseline.source} × target={target.source}) …")
    baseline_df = fetch_caseresults(baseline, cfg)
    if baseline_df.empty:
        raise SystemExit(f"No case results for baseline build {baseline.build_id} "
                         f"(source={baseline.source}).")
    target_df = fetch_caseresults(target, cfg)
    if target_df.empty:
        raise SystemExit(f"No case results for target build {target.build_id} "
                         f"(source={target.source}).")
    print(f"   baseline rows: {len(baseline_df)}  target rows: {len(target_df)}")

    df = compute_test_diff(baseline_df, target_df)
    if df.empty:
        raise SystemExit("test_diff returned 0 rows (no PASSED→FAILED after matching).")
    print(f"   {len(df)} regressions — status_b: "
          f"{df['status_b'].value_counts().to_dict()}")

    # api caseresults don't carry case names. Backfill test_case from the
    # Testray case object so the fragment matcher has something to anchor on
    # (and so prompt.md doesn't say `### N. \`\`` with no test name).
    df = enrich_api_case_names(df, cfg["testray"])

    print(f"→ Step 2/6 build metadata …")
    # Reuse the caseresults already fetched above — the git hash rides on each
    # caseresult row, so no second round-trip to Testray is needed.
    meta_a = resolve_side_metadata(baseline, cfg["testray"])
    meta_b = resolve_side_metadata(target,   cfg["testray"])

    routine_id = meta_a["routine_id"] or meta_b["routine_id"]
    if meta_a["routine_id"] and meta_b["routine_id"] \
            and meta_a["routine_id"] != meta_b["routine_id"]:
        print(f"WARNING: builds are on different routines "
              f"(A={meta_a['routine_id']}, B={meta_b['routine_id']}). "
              f"Diff may still be meaningful but test_diff will miss cases "
              f"that don't run on both.", file=sys.stderr)
    print(f"   routine_id={routine_id}  "
          f"A={meta_a['git_hash'][:12]}  B={meta_b['git_hash'][:12]}")

    return _finalize_bundle(
        df=df, run_id=run_id, run_dir=run_dir,
        classifier=classifier,
        baseline_source=baseline.source, target_source=target.source,
        build_a=baseline.build_id, build_b=target.build_id,
        hash_a=meta_a["git_hash"], hash_b=meta_b["git_hash"],
        routine_id=routine_id,
        build_a_name=meta_a["build_name"], build_b_name=meta_b["build_name"],
        git_repo=git_repo,
        mode=mode,
        fetch_specs=fetch_specs,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_side_args(ap: argparse.ArgumentParser, role: str) -> None:
    """Add --{role}-build-id / --{role}-hash / --{role}-name to the parser."""
    ap.add_argument(f"--{role}-build-id", type=int, required=True,
                    help=f"Build id for the {role} build (Testray REST).")
    ap.add_argument(f"--{role}-hash",     default=None,
                    help=f"Optional git-hash override for the {role} build "
                         f"(auto-resolved from the api response otherwise).")
    ap.add_argument(f"--{role}-name",     default=None,
                    help=f"Optional display name for the {role} build.")


def _build_spec(args: argparse.Namespace, role: str) -> SideSpec:
    hash_    = getattr(args, f"{role}_hash")
    build_id = getattr(args, f"{role}_build_id")
    name     = getattr(args, f"{role}_name")

    return SideSpec(
        role=role,
        build_id=build_id,
        hash=hash_,
        name=name,
    )


def _normalize_fetch_ref(entry: list) -> tuple[str, str]:
    """Accept `--fetch-ref <remote-or-url> <ref>` (two tokens) OR a single
    GitHub `…/tree/<branch>` browser URL, and return (remote_or_url, ref).
    (git fetch can't use the /tree/ web URL directly — we split it.)"""
    if len(entry) == 2:
        return entry[0], entry[1]
    if len(entry) == 1:
        url = entry[0].rstrip("/")
        if "/tree/" in url:
            repo, ref = url.split("/tree/", 1)
            if repo and ref:
                return repo, ref
        raise SystemExit(
            f"--fetch-ref {entry[0]!r}: pass either `<remote-or-url> <ref>` "
            f"or a single GitHub `…/tree/<branch>` URL."
        )
    raise SystemExit("--fetch-ref takes 1 (tree URL) or 2 (remote + ref) values.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Prepare a triage run bundle. Both sides (baseline, target) "
                    "load case results from the Testray REST api; the git hash is "
                    "auto-resolved from the response (--{side}-hash overrides).",
    )
    _add_side_args(ap, "baseline")
    _add_side_args(ap, "target")
    ap.add_argument("--classifier", default=DEFAULT_CLASSIFIER,
                    help=f"Provenance label (default: {DEFAULT_CLASSIFIER})")
    ap.add_argument("--by-subtask", action="store_true",
                    help="Subtask-aware mode: group regressions by Testray "
                         "Subtask (testflow algorithm), classify once per "
                         "group, fan out the verdict across member case-rows.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Directory to write the run bundle into "
                         "(default: ./runs). On Jenkins, point this at the "
                         "workspace/artifacts dir.")
    ap.add_argument("--fetch-ref", nargs="+", action="append", default=[],
                    help="Fetch a build commit not on origin (e.g. a temp "
                         "mitigation branch on a fork). Two forms: "
                         "`<remote-or-url> <ref>`, or a single GitHub "
                         "`…/tree/<branch>` URL. Repeatable.")

    args = ap.parse_args()
    fetch_specs = [_normalize_fetch_ref(e) for e in args.fetch_ref]
    baseline = _build_spec(args, "baseline")
    target   = _build_spec(args, "target")
    mode     = MODE_BY_SUBTASK if args.by_subtask else MODE_PER_TEST
    prepare(baseline, target, args.classifier, mode=mode, out_dir=args.out,
            fetch_specs=fetch_specs)


if __name__ == "__main__":
    main()
