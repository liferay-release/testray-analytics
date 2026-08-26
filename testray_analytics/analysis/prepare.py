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
import collections
import concurrent.futures
import http.client
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
from . import error_signature
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

# Classification granularity (ARCHITECTURE.md §7). Orthogonal to the §8
# *selection* mode (build-vs-build / routine-history / suite-vs-pr).
#
#   by-cluster  DEFAULT. Group failures by error signature (§5) BEFORE the
#               classifier runs, classify once per signature, and fan the
#               verdict out to every member CaseResult — the per-CaseResult
#               grain (decision #6) is preserved. One consistent verdict per
#               root cause, and far fewer units: measured 449 -> 169 on the
#               2026.q1.11 -> q1.12-lts pair.
#   per-test    One prompt section per failure. Highest fidelity and the
#               richest training labels, but it re-answers the same question
#               once per member of a cluster — a 102-member signature group
#               costs 102 classifications to reach one answer. Never selected
#               automatically; kept for the §7 A/B and as a fallback when a
#               signature is suspected of over-grouping.
#   by-subtask  Group by Testray Subtask instead of our signature. Requires
#               an `api` target source (the link lives on the caseresult
#               object, r_subtaskToCaseResults_c_subtaskId).
#
# Only the error signature can group pre-send: the full `clusterKey` includes
# `culprit_file`, which is an LLM *output*, so it does not exist yet.
MODE_PER_TEST    = "per-test"
MODE_BY_SUBTASK  = "by-subtask"
MODE_BY_CLUSTER  = "by-cluster"
MODES            = (MODE_BY_CLUSTER, MODE_PER_TEST, MODE_BY_SUBTASK)
DEFAULT_MODE     = MODE_BY_CLUSTER
# Modes that classify a group once and fan the verdict out to its members.
GROUPED_MODES    = (MODE_BY_CLUSTER, MODE_BY_SUBTASK)

# Testray components whose rows are infrastructure rather than tests. Kept in
# the data, excluded from classification. Overridable via the triage config's
# `excluded_components`.
DEFAULT_EXCLUDED_COMPONENTS = ("Batch",)

# A CI batch/shard row names a batch axis and shard, not a test:
#   functional-tomcat-hypersonic20-jdk21_zulu/1/3
#   empty-osgi-core-dir-postgresql163/0/0
# This is the PRIMARY signal, because the shape is what makes a row
# unattributable — there is no test, so no code can be blamed. The component
# list is the safety net: the same shape appears under `Batch`, `OSGI` and
# `Smoke`, so keying on the component label alone misses some.
# Anchored and whitespace-free on purpose: Poshi names are `File#Command` and
# Playwright names contain spaces ("… .spec.ts > does a thing"), so neither can
# match by accident.
_BATCH_SHARD_RE = re.compile(r"^[^\s]+/\d+/\d+$")

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

# Which TESTRAY_* variables overrode config.yml on the last load_config().
# Reported by testray_target() so an override is visible in the run output
# rather than inferred from a 401 twenty minutes later.
_ENV_OVERRIDES: list[str] = []


def load_config() -> dict:
    """Read config.yml, then let TESTRAY_* environment variables override the
    Testray connection settings (secrets can live in the shell or in Jenkins
    credentials rather than the file — decision #5).

    The override lives here, not in prepare(), so that EVERY command resolves
    the same way. When only prepare() applied it, a stale TESTRAY_CLIENT_ID in
    a shell pointed the read half at one instance while submit kept writing to
    whatever config.yml said — silently, since nothing printed the target."""
    global _ENV_OVERRIDES
    with open(find_config_file()) as f:
        cfg = yaml.safe_load(f)

    tr = cfg.setdefault("testray", {})
    _ENV_OVERRIDES = []
    for _key, _env in (("base_url", "TESTRAY_BASE_URL"),
                       ("client_id", "TESTRAY_CLIENT_ID"),
                       ("client_secret", "TESTRAY_CLIENT_SECRET")):
        if os.environ.get(_env):
            tr[_key] = os.environ[_env]
            _ENV_OVERRIDES.append(_env)
    return cfg


def testray_target(cfg: dict) -> str:
    """One line naming the instance this command will talk to, and whether the
    environment redirected it. Print it early — it is the difference between
    diagnosing a mismatch in five seconds and in an evening."""
    line = (cfg.get("testray") or {}).get("base_url", "<no base_url>")
    if _ENV_OVERRIDES:
        line += f"   [overridden by {', '.join(sorted(_ENV_OVERRIDES))}]"
    return line


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
    # Retried: editing the OAuth app's scopes in DXP invalidates its tokens for
    # a moment, and an unretried mint turns that blip into a dead run — which is
    # expensive when it happens partway through a 30k-row fetch.
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"{base}/o/oauth2/token", data=data)
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
            token = body.get("access_token")
            if not token:
                raise SystemExit(
                    f"OAuth2 token response had no access_token: {body}")
            return token
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (401, 403, 429) and e.code < 500:
                break                       # a real config problem, not a blip
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        except urllib.error.URLError as e:
            last = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise SystemExit(
        f"OAuth2 token request to {base} failed after 3 attempts: {last}. "
        f"Check testray.client_id / client_secret, and that the app's scopes "
        f"aren't mid-edit in DXP."
    )


def fetch_paginated(endpoint: str, params: dict, token: str, base_url: str,
                    **kwargs) -> list[dict]:
    """Public alias for the paginator. USE THIS from scripts and notebooks.

    It forces `sort=id:asc`, and that is not optional: without an explicit sort
    the server pages inconsistently and silently returns some rows twice while
    skipping others. A hand-rolled pull of two builds returned 17,497 rows
    containing only 11,496 distinct ids and produced a confident, wrong
    conclusion. Row counts look right while the contents are wrong, which is
    the worst failure shape available — so the helper is exported rather than
    left for each new consumer to rediscover.
    """
    return _testray_fetch_paginated(endpoint, params, token, base_url, **kwargs)


# How many times a page fetch is attempted before giving up (1 try + retries).
_FETCH_RETRIES = 4


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
        # Retry transient network faults. A 40k-row build is ~84 sequential
        # pages over several minutes, and prod drops a connection often enough
        # that one RemoteDisconnected used to throw away the whole fetch. Only
        # connection-level faults are retried: an HTTPError is an answer from
        # the server and retrying it just asks the same bad question again.
        data = None
        for attempt in range(_FETCH_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise SystemExit("Testray API 401 — token expired. Re-run.")
                raise
            except (urllib.error.URLError, http.client.HTTPException,
                    ConnectionError, TimeoutError) as e:
                if attempt == _FETCH_RETRIES - 1:
                    raise
                wait = 2 ** attempt
                print(f"   [retry] page {page}: {type(e).__name__}, "
                      f"retrying in {wait}s "
                      f"({attempt + 1}/{_FETCH_RETRIES - 1})",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
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


def fetch_team_metadata(team_ids: list[int], cfg: dict) -> dict[int, str]:
    """Resolve {team_id: name} via /o/c/teams/{id}.

    Deliberately mirrors fetch_component_metadata, including the 404-skip: a
    caseresult can reference a team that no longer exists, and one dangling id
    must not fail a whole run.
    """
    if not team_ids:
        return {}
    token = _testray_oauth_token(cfg)
    base = cfg["base_url"].rstrip("/")
    total = len(team_ids)
    step = max(1, total // 10)
    print(f"   [team metadata] fetching {total} team(s) …",
          file=sys.stderr, flush=True)
    out: dict[int, str] = {}
    for i, tid in enumerate(team_ids, start=1):
        url = f"{base}/o/c/teams/{tid}"

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
            out[int(tid)] = name
        if i % step == 0 or i == total:
            print(f"   [team metadata] {i}/{total}", file=sys.stderr, flush=True)
        time.sleep(0.05)
    return out


def _is_blank(v) -> bool:
    """Blank means: None, NaN, empty, or the literal string 'nan'.

    That last case is not paranoia — a CSV round-trip turns NaN into the
    four-character string "nan", and without this the Component column
    re-renders as "nan" forever after the first save.
    """
    if v is None:
        return True
    if isinstance(v, float) and pd.isna(v):
        return True
    t = str(v).strip()
    return t == "" or t.lower() == "nan"


def resolve_component_team_names(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Fill `testray_component_name` / `team_name` from the component/team FKs.

    One lookup per DISTINCT id, not per row — a 17k-row build has a handful of
    components, and per-row lookups would be thousands of sequential requests.

    Never overwrites a name that is already set: a csv/tar side may carry real
    names, and `enrich_api_caseresults` may have filled them already.
    """
    if df is None or df.empty:
        return df

    for id_col, name_col, fetch in (
        ("component_id", "testray_component_name", fetch_component_metadata),
        ("team_id",      "team_name",              fetch_team_metadata),
    ):
        if id_col not in df.columns:
            continue
        if name_col not in df.columns:
            df[name_col] = None

        blank = df[name_col].map(_is_blank)
        ids = pd.to_numeric(df.loc[blank, id_col], errors="coerce").fillna(0)
        # 0 is the "no link" sentinel — skip it rather than fetching /0.
        wanted = sorted({int(x) for x in ids if int(x) != 0})
        if not wanted:
            continue
        names = fetch(wanted, cfg)
        if not names:
            continue

        # Cast to object first. prepare seeds these columns as [None]*n, which
        # pandas types float64; assigning strings into that raises
        # "Invalid value for dtype 'float64'" on pandas >= 2.2. This is why the
        # bug survived unit tests that used string columns but failed live.
        df[name_col] = df[name_col].astype(object)
        mapped = pd.to_numeric(df[id_col], errors="coerce").fillna(0).astype("int64").map(names)
        df.loc[blank & mapped.notna(), name_col] = mapped[blank & mapped.notna()]
    return df


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
        # The FKs were already in the requested field list but never read out,
        # so component/team rendered blank on every api-sourced row (§15).
        # 0 is the "no link" sentinel the resolver filters on — not NaN, so the
        # column stays integer and `== 0` is a safe test.
        "component_id":   [it.get("r_componentToCaseResult_c_componentId") or 0
                           for it in items],
        "team_id":        [it.get("r_teamToCaseResult_c_teamId") or 0
                           for it in items],
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
        "name":       (str(body.get("name")).strip()
                       if body.get("name") else None),
        "git_hash":   str(h).strip() if h and str(h).strip() else None,
        "routine_id": int(rid) if rid else None,
        "project_id": fetch_routine_project(int(rid), cfg) if rid else None,
    }


def fetch_routine_project(routine_id: int, cfg: dict) -> int | None:
    """Project id for a routine, or None.

    Needed only so the report can build Testray deep-links: a case-result URL
    is /project/{p}/routines/{r}/build/{b}/case-result/{cr}, and the project is
    the one segment no other response carries. The relationship is named
    `routineToProjects` (routine -> project), so the FK lives on Routine and
    reads `r_routineToProjects_c_projectId` — not the reverse spelling.

    Failure is non-fatal: without it the report renders test names as plain
    text, which is strictly better than emitting a half-built link.
    """
    base = cfg["base_url"].rstrip("/")
    url = f"{base}/o/c/routines/{routine_id}"

    def _do_request(tok):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    try:
        body = _do_request(_testray_oauth_token(cfg))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            try:
                body = _do_request(_testray_oauth_token(cfg))
            except urllib.error.HTTPError:
                return None
        else:
            return None
    except OSError:
        return None
    pid = body.get("r_routineToProjects_c_projectId")
    return int(pid) if pid else None


def testray_ui_url(cfg: dict) -> str | None:
    """Base URL of the Testray *UI*, used only for report deep-links.

    Kept separate from `base_url` (the REST root) and deliberately NOT derived
    from it: the UI sits under a site friendly URL that differs per instance
    — `/web/testray` on prod, `/web/liferay-testray` on a locally initialized
    one — so deriving it would emit links that 404, which reads as a Testray
    bug rather than as missing configuration. Unset means the report renders
    test names as plain text.
    """
    ui = str(cfg.get("ui_url") or "").strip()
    return ui.rstrip("/") or None


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


# --- §12 transition matrix --------------------------------------------------
# Baseline status → target status, with the FAILED→FAILED case decided by the
# error signature rather than the transition. Decision #13: surfacing only
# PASSED→FAILED silently undercounts regressions whenever the baseline already
# had failures — which, on a real acceptance build, is most of the time.

TRANSITION_NEW          = "new"            # PASSED → FAILED/BLOCKED/UNTESTED
TRANSITION_CHANGED      = "changed"        # FAILED → FAILED, signature differs
TRANSITION_SAME_FAILURE = "same_failure"   # FAILED → FAILED, same signature
TRANSITION_FIXED        = "fixed"          # FAILED → PASSED
TRANSITION_AWARENESS    = "awareness"      # FAILED → BLOCKED
TRANSITION_BLOCKED      = "blocked"        # BLOCKED → FAILED
TRANSITION_TESTFIX      = "testfix"        # TESTFIX(status) → FAILED
TRANSITION_NO_BASELINE  = "no_baseline"    # UNTESTED → FAILED
TRANSITION_OTHER        = "other"

# Which transitions are triage candidates. The rest are counted and reported
# but never sent to the classifier.
#
# `no_baseline` is included deliberately. The baseline having no result does
# not mean there is nothing to look at — the commonest reason a test has no
# baseline is that it is NEW, and a brand-new test that fails is exactly the
# thing triage exists to catch. Dropping it hid those failures completely.
# They carry no baseline signal to compare against, so they are not sent to
# the classifier either; enrich_and_pre_classify pre-classifies them and
# submit._auto_label surfaces them as NEEDS_REVIEW for a human.
TRIAGE_TRANSITIONS = frozenset({
    TRANSITION_NEW, TRANSITION_CHANGED, TRANSITION_BLOCKED, TRANSITION_TESTFIX,
    TRANSITION_NO_BASELINE,
})

_FAILING = ("FAILED", "BLOCKED", "UNTESTED")

# pre_classify buckets that mean "infrastructure", not "product regression".
# Two different env failures are still one env problem, so a move between them
# is not a changed failure. NO_ERROR is included: no text on either side cannot
# evidence a change.
_ENV_CATEGORIES = frozenset({
    "BUILD_FAILURE", "ENV_CHROME", "ENV_DEPENDENCY", "ENV_DATE", "ENV_SETUP",
    "NO_ERROR",
})

# Pre-classifications meaning "this side produced no result at all", as opposed
# to "the environment broke the test". Mirrors submit._DID_NOT_RUN; kept here
# because compute_test_diff must not import from submit.
_BASELINE_RAN_NOTHING = frozenset({"BUILD_FAILURE", "NO_ERROR", "BATCH_FAILURE"})

# The pre_classification such a row gets. Not a new verdict: submit maps it
# onto NEEDS_REVIEW, so the verdict vocabulary, the Testray picklist and
# util/verdict.ts are all untouched.
NO_BASELINE_PRE = "NO_BASELINE"


def classify_transition(status_a, status_b, error_a, error_b) -> str:
    """Label one baseline→target pair per the §12 matrix.

    `FAILED→BLOCKED` is awareness, not triage: blocked is usually infra, and
    letting it through dilutes the BUG-hunting view. `FAILED→PASSED` is a
    "what got fixed" signal for a future Insights view. `UNTESTED→FAILED` has
    no baseline health signal at all, so it cannot be called new.
    """
    a = (status_a or "").upper()
    b = (status_b or "").upper()

    if b == "PASSED":
        return TRANSITION_FIXED if a == "FAILED" else TRANSITION_OTHER
    if a == "PASSED" and b in _FAILING:
        return TRANSITION_NEW
    if a == "FAILED":
        if b == "FAILED":
            return (TRANSITION_CHANGED
                    if error_signature.signatures_differ(error_a, error_b)
                    else TRANSITION_SAME_FAILURE)
        if b in ("BLOCKED", "UNTESTED"):
            return TRANSITION_AWARENESS
    if a == "BLOCKED" and b in _FAILING:
        return TRANSITION_BLOCKED
    if a == "TESTFIX" and b in _FAILING:
        return TRANSITION_TESTFIX
    if a == "UNTESTED" and b in _FAILING:
        return TRANSITION_NO_BASELINE
    return TRANSITION_OTHER


def baseline_signature_prevalence(baseline: pd.DataFrame) -> dict[str, int]:
    """How often each normalized error signature already occurs among the
    BASELINE build's own failures.

    The pipeline judges a case on its own status pair and never asks whether
    the error it now shows is one the baseline was already producing dozens of
    times on other tests. That question is what separates a new failure mode
    from standing suite noise, and — unlike the transition label — it works
    identically on `new` and `changed` rows.

    Counts every non-passing baseline row, not just the ones that joined: a
    signature's prevalence is a property of the build, not of the triage set.
    """
    if baseline is None or baseline.empty or "errors" not in baseline.columns:
        return {}
    status = baseline.get("status")
    counts: dict[str, int] = {}
    for i, err in enumerate(baseline["errors"]):
        if status is not None:
            st = str(status.iloc[i] or "").upper()
            if st in ("PASSED", ""):
                continue
        sig = error_signature.normalize(err)
        if sig:
            counts[sig] = counts.get(sig, 0) + 1
    return counts


def attach_baseline_prevalence(df: pd.DataFrame, prevalence: dict[str, int]
                               ) -> pd.DataFrame:
    """Add `baseline_signature_count` — 0 means the baseline never produced
    this error, which is the strongest single signal that it is genuinely new.
    """
    if df is None or df.empty:
        return df
    df = df.copy()
    df["baseline_signature_count"] = [
        prevalence.get(error_signature.normalize(e), 0)
        for e in df.get("error_message", [""] * len(df))
    ]
    return df


# ---------------------------------------------------------------------------
# Per-case history — "has this test been failing for a while?"
# ---------------------------------------------------------------------------

# OFF by default (depth 0). Tried on prod routine 107946124 on 2026-08-24 and
# reverted the same day: it correctly caught chronically-broken tests, but the
# run it produced was harder to work through than the one without it, so the
# default is opt-in until that is understood. `--history-depth 10` turns it on.
# Note the collapse of TEST_FIX in that run was NOT this feature — the case
# that regressed had history_fail_streak=0 and was never touched by the filter.
HISTORY_DEPTH_DEFAULT   = 0
HISTORY_STREAK_DEFAULT  = 5
HISTORY_WORKERS_DEFAULT = 8

# The pre_classification a chronic row gets. Not a new verdict: submit maps it
# onto FALSE_POSITIVE, so the verdict vocabulary, the Testray picklist and
# util/verdict.ts are all untouched.
PRE_EXISTING = "PRE_EXISTING"


def fetch_case_history(case_ids, cfg: dict, *,
                       depth: int = HISTORY_DEPTH_DEFAULT,
                       workers: int = HISTORY_WORKERS_DEFAULT,
                       exclude_caseresult_ids: set | None = None,
                       ) -> dict[int, list[dict]]:
    """Recent case results per case, most recent first, excluding this run's own.

    One request per case (`sort=id:desc`, pageSize=depth+1). Measured at ~0.6s
    each against prod, so this is threaded — the only concurrent fetch in the
    module. Failures are swallowed per case: history is corroborating evidence,
    and losing it must degrade a verdict, never abort a run.

    Deliberately NOT filtered to the target's routine. The same case runs in
    several routines, and restricting to one is what makes a chronically broken
    test look new: the case this was built for had only 3 results in its own
    routine but had failed in 20 consecutive builds across two.
    """
    ids = [int(c) for c in dict.fromkeys(case_ids) if pd.notna(c)]
    if not ids:
        return {}
    exclude = exclude_caseresult_ids or set()
    token = _testray_oauth_token(cfg)
    base = cfg["base_url"].rstrip("/")

    def _one(cid: int) -> tuple[int, list[dict]]:
        q = urllib.parse.urlencode({
            "filter":   f"r_caseToCaseResult_c_caseId eq '{cid}'",
            "sort":     "id:desc",
            "page":     1,
            "pageSize": depth + 1,
            "fields":   "id,dueStatus,errors",
        })
        req = urllib.request.Request(
            f"{base}/o/c/caseresults?{q}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                items = json.loads(resp.read()).get("items", [])
        except Exception:
            return cid, []
        out = [{"status": (it.get("dueStatus") or {}).get("key") or "",
                "errors": it.get("errors") or ""}
               for it in items if it.get("id") not in exclude]
        return cid, out[:depth]

    history: dict[int, list[dict]] = {}
    print(f"   [history] {len(ids)} case(s), last {depth}, {workers} workers …",
          file=sys.stderr, flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for cid, rows in pool.map(_one, ids):
            history[cid] = rows
    return history


def _nonpass_streak(rows: list[dict]) -> int:
    """Consecutive most-recent results that did not PASS.

    Not a signature-matched streak. A chronically broken test rarely fails
    identically every time — the case this was built for reported "2 Failed
    tests" for twenty builds and "3 Failed tests" on the twenty-first, which
    resets a signature streak to zero while the test never once passed. The
    question is "when did this last pass", and that is what a reader means by
    "it has been failing for a while".
    """
    n = 0
    for r in rows:
        if str(r.get("status") or "").upper() == "PASSED":
            break
        n += 1
    return n


def attach_history(df: pd.DataFrame, history: dict[int, list[dict]], *,
                   streak_threshold: int = HISTORY_STREAK_DEFAULT,
                   ) -> pd.DataFrame:
    """Add `history_depth` / `history_fail_streak` and pre-classify chronics.

    A row whose test has not passed in `streak_threshold` consecutive prior
    builds is not a regression this diff introduced, whatever the baseline
    build happened to record. It gets `pre_classification=PRE_EXISTING`, which
    keeps it in the report with its streak on the record but takes it out of
    the classify prompt — so it costs no model spend and cannot land in
    NEEDS_REVIEW. Rows already pre-classified (env/infra) keep their label.
    """
    if df is None or df.empty or not history:
        return df
    df = df.copy()
    rows = [history.get(int(c), []) if pd.notna(c) else []
            for c in df.get("testray_case_id", [])]
    df["history_depth"]       = [len(r) for r in rows]
    df["history_fail_streak"] = [_nonpass_streak(r) for r in rows]

    if "pre_classification" not in df.columns:
        df["pre_classification"] = None
    chronic = ((df["history_fail_streak"] >= streak_threshold)
               & df["pre_classification"].isna())
    df.loc[chronic, "pre_classification"] = PRE_EXISTING
    return df


def compute_test_diff(baseline: pd.DataFrame, target: pd.DataFrame,
                      *, matrix_out: dict | None = None,
                      coverage_out: dict | None = None):
    """Inner-join baseline and target and keep the §12 triage candidates —
    new failures *and changed* ones, not only PASSED→FAILED.

    Returns `(df, counts)`: the triage rows, and a Counter of every transition
    seen including the excluded ones, so a run can report "12 new, 3 changed,
    847 same failure, 40 fixed" instead of silently dropping the remainder.

    Join key: `case_id` if the target has one (db, api targets); otherwise
    `(case_name, component_name)` (csv targets). A final dropna on
    `testray_case_id` discards rows with no persistable id.

    The join is an INNER one, so a case the baseline ran and the target did
    not is silently absent from every count this returns. That is a coverage
    regression, not a triage row — a suite that stopped running half its tests
    reads as a healthy build here. `coverage_out` is where it surfaces: an
    out-parameter (like `matrix_out`) carrying the anti-join both ways, so the
    report can state the ran-both percentage instead of implying 100%."""
    if baseline.empty or target.empty:
        return pd.DataFrame(), collections.Counter()

    target_has_ids   = target["case_id"].notna().any()
    baseline_has_ids = baseline["case_id"].notna().any()
    both_have_ids    = target_has_ids and baseline_has_ids
    key_cols = ["case_id"] if both_have_ids else ["case_name", "component_name"]

    b = _aggregate_baseline(baseline, key_cols=key_cols)
    t = _aggregate_target(target,     key_cols=key_cols)

    merged = b.merge(t, on=key_cols, how="inner", suffixes=("_a", "_b"))

    # Coverage BEFORE anything is filtered: what the inner join above threw
    # away. `baseline_only` is the important half — tests that ran in A and
    # not in B produce no failure, no transition and no row, so they cannot
    # be noticed downstream by any means except this.
    if coverage_out is not None:
        b_keys = set(map(tuple, b[key_cols].itertuples(index=False, name=None)))
        t_keys = set(map(tuple, t[key_cols].itertuples(index=False, name=None)))
        only_b = b_keys - t_keys
        by_status: dict[str, int] = {}
        if only_b:
            mask = [tuple(r) in only_b
                    for r in b[key_cols].itertuples(index=False, name=None)]
            for st in b.loc[mask, "status"].fillna("UNTESTED"):
                by_status[str(st)] = by_status.get(str(st), 0) + 1
        coverage_out.clear()
        coverage_out.update({
            "baseline_cases":         len(b_keys),
            "target_cases":           len(t_keys),
            "ran_both":               len(b_keys & t_keys),
            "baseline_only":          len(only_b),
            "target_only":            len(t_keys - b_keys),
            "baseline_only_by_status": by_status or None,
        })

    merged["transition"] = [
        classify_transition(sa, sb, ea, eb)
        for sa, sb, ea, eb in zip(merged["status_a"], merged["status_b"],
                                  merged["errors_a"], merged["errors_b"])
    ]
    # Env churn is not a changed failure. Two runs can fail on the same
    # infrastructure problem with textually different messages — a different
    # container id, a different missing dependency — and the signature
    # comparison alone reads that as a regression. When both sides
    # pre-classify to the SAME env/infra bucket, the reason did not really
    # change. (Inherited from the previous platform's
    # `changed_failures.is_changed`, which learned this the hard way.)
    changed_mask = merged["transition"] == TRANSITION_CHANGED
    if changed_mask.any():
        extra = prompt_helpers.load_triage_config().get("auto_classify_patterns") or {}
        for idx in merged.index[changed_mask]:
            cat_a = prompt_helpers.pre_classify(merged.at[idx, "errors_a"], extra)
            cat_b = prompt_helpers.pre_classify(merged.at[idx, "errors_b"], extra)
            if cat_a and cat_a == cat_b and cat_a in _ENV_CATEGORIES:
                merged.at[idx, "transition"] = TRANSITION_SAME_FAILURE
            elif cat_a in _BASELINE_RAN_NOTHING and cat_b not in _BASELINE_RAN_NOTHING:
                # The baseline row says the test never ran ("was not executed",
                # "build failed prior to running"). Its error text is a harness
                # notice, not a failure, so comparing signatures against it is
                # meaningless — every real target failure looks "changed", and
                # a test that has been broken for twenty builds is triaged as a
                # regression against the one build that failed to run it. The
                # honest reading is the one we already have a name for: this
                # pair carries no baseline health signal.
                merged.at[idx, "transition"] = TRANSITION_NO_BASELINE

    # Full A x B status cross-tab, taken BEFORE the triage filter — this is
    # the whole comparison (every joined case), not the 557 rows we triage.
    # An out-parameter rather than a third return value, so every existing
    # caller keeps unpacking two.
    if matrix_out is not None:
        matrix: dict[str, dict[str, int]] = {}
        for a, b in zip(merged["status_a"].fillna("UNTESTED"),
                        merged["status_b"].fillna("UNTESTED")):
            matrix.setdefault(str(a), {})
            matrix[str(a)][str(b)] = matrix[str(a)].get(str(b), 0) + 1
        matrix_out.clear()
        matrix_out.update(matrix)

    counts = collections.Counter(merged["transition"])
    diff = merged[merged["transition"].isin(TRIAGE_TRANSITIONS)].copy()

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
        "transition":             diff["transition"],
        "error_message":          diff["errors_b"],
        # §12: for a changed failure the prompt must carry BOTH errors ("was
        # failing with X, now Y") — the reasoning is about the delta, and the
        # rubric's "baseline was clean" assumption does not hold for it.
        "baseline_error_message": diff["errors_a"],
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

    # Same suffix cases as subtask_id above. Build B is what the report
    # describes, so the TARGET side's FKs win — a case can be recategorised
    # between builds, and the baseline's component would mislabel the failure.
    for col in ("component_id", "team_id"):
        if f"{col}_b" in diff.columns:
            out[col] = diff[f"{col}_b"]
        elif col in diff.columns and target_has_ids:
            out[col] = diff[col]

    out = out.dropna(subset=["testray_case_id"]).reset_index(drop=True)
    return out, counts


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
        # Prefer the operator's --{side}-name, then Testray's own build name.
        # `api:<id>` is a last resort: a title reading "api:270750 → api:270748"
        # tells a reader nothing about which release they are looking at.
        "build_name": spec.name or build.get("name") or f"api:{spec.build_id}",
        "git_hash":   git_hash,
        "routine_id": build["routine_id"],
        "project_id": build.get("project_id"),
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
        # --no-tags: a fork's tag namespace is its own, and fetching a PR head
        # from one otherwise drags every tag it has into the local repo. We only
        # ever need the commit to be reachable for `git diff`, which is also why
        # this fetches no local branch and never checks anything out — the
        # working tree the user is sitting in must not move.
        subprocess.run(["git", "-C", str(git_dir), "fetch", "--quiet",
                        "--no-tags", src, ref],
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

    # Testray's own team wins; the local component->team map is only a
    # FALLBACK for rows Testray left blank. The previous form applied the map
    # first and filled from Testray only where the map returned nothing, which
    # silently replaced real Testray teams with mapped ones.
    mapped = df["component_name"].apply(prompt_helpers.team_for_component)
    keep = ~df["team_name"].map(_is_blank)
    df["team_name"] = df["team_name"].astype(object).where(keep, mapped)

    df["pre_classification"] = df["error_message"].apply(
        lambda e: prompt_helpers.pre_classify(e, extra_pats)
    )

    # CI-batch rows are infrastructure, not tests. Their `test_case` is a batch
    # name ("functional-tomcat101-postgresql163/25/1"), so there is no test to
    # match against the diff and no code to attribute a failure to — asking the
    # classifier about them spends tokens to reach "cannot say". Testray labels
    # them with the `Batch` component, so that is the signal we key on.
    #
    # They are NOT dropped from the data: they keep their row, are counted in
    # the transition totals, and appear in the report. They are only excluded
    # from classification, via the same `pre_classification` mechanism the
    # env/infra patterns use.
    excluded = [str(c).strip().lower()
                for c in (cfg.get("excluded_components")
                          or DEFAULT_EXCLUDED_COMPONENTS)]
    is_batch = pd.Series(False, index=df.index)
    if "test_case" in df.columns:
        is_batch |= df["test_case"].map(
            lambda n: (not _is_blank(n))
            and bool(_BATCH_SHARD_RE.match(str(n).strip()))
        )
    if excluded and "component_name" in df.columns:
        is_batch |= df["component_name"].map(
            lambda c: (not _is_blank(c)) and str(c).strip().lower() in excluded
        )
    # Only where no more specific pattern already fired — an error-text match
    # says something about *why*, which is worth keeping.
    fill = is_batch & df["pre_classification"].isna()
    if fill.any():
        df.loc[fill, "pre_classification"] = "BATCH_FAILURE"

    # Rows with no baseline result. They stay in triage (see
    # TRIAGE_TRANSITIONS) because a failing NEW test has no baseline by
    # definition and must not vanish, but there is nothing to diff a signature
    # against, so they go to a human rather than to the classifier. Same
    # `pre_classification` mechanism as the env/infra and batch rows; again
    # only where no more specific pattern already fired.
    if "transition" in df.columns:
        no_base = ((df["transition"] == TRANSITION_NO_BASELINE)
                   & df["pre_classification"].isna())
        if no_base.any():
            df.loc[no_base, "pre_classification"] = NO_BASELINE_PRE

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
                "required": ["case_ids", "classification",
                             "confidence", "reason"],
                # A result must identify its group. by-cluster emits group_id;
                # by-subtask bundles written before group_id existed emit
                # subtask_id, so either satisfies the schema and old bundles
                # keep validating.
                "anyOf": [{"required": ["group_id"]},
                          {"required": ["subtask_id"]}],
                "additionalProperties": False,
                "properties": {
                    "group_id":        {"type": ["integer", "null"]},
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
                  flaky_excluded: int, mode: str = DEFAULT_MODE,
                  project_id: int | None = None,
                  testray_url: str | None = None,
                  transition_counts: dict | None = None,
                  baseline_rows: int | None = None,
                  target_rows: int | None = None,
                  status_matrix: dict | None = None,
                  coverage: dict | None = None) -> None:
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
        # Recorded for the report's Testray deep-links only; nothing in the
        # pipeline branches on either value.
        "project_id":          project_id,
        "testray_url":         testray_url,
        "build_a_name":        build_a_name,
        "build_b_name":        build_b_name,
        # `total_failures` is the TRIAGE set (new + changed + blocked), not
        # every test and not every failure. The other three make that legible
        # instead of leaving a bare 557 to be misread as "tests run".
        "baseline_rows":       baseline_rows,
        "target_rows":         target_rows,
        "transition_counts":   dict(transition_counts or {}) or None,
        "status_matrix":       dict(status_matrix or {}) or None,
        # The anti-join the status_matrix cannot show: cases one build ran
        # and the other did not. Read by the report's Compared pill.
        "coverage":            dict(coverage or {}) or None,
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

Write `results.json` in this directory, validating against `results.schema.json`. **One entry per group** (not per case):

```json
{{
  "run_id": "{run_id}",
  "classifier": "{classifier}",
  "results": [
    {{
      "group_id": 12,
      "case_ids": [65638, 65644, 65650],
      "classification": "FALSE_POSITIVE",
      "confidence": "high",
      "reason": "Classic Poshi ElementNotFoundPoshiRunnerException — selector/timing flake. No hunks touch the cookies-banner selectors involved."
    }},
    {{
      "group_id": 47,
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

**`group_id` is required and must match the group's heading exactly** — it is how the verdict is fanned out to every member case result. The member list shown under each heading is truncated for readability, so `case_ids` is a convenience only: the canonical membership is resolved from `group_id`. Never invent or renumber a `group_id`.

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



_TICKET_RE = re.compile(r"^([A-Z][A-Z0-9]+-\d+)\b")


def collect_tickets_in_range(git_repo: Path, hash_a: str, hash_b: str
                             ) -> list[str]:
    """Unique ticket keys across `A..B`, in first-seen (newest-first) order.

    Mirrors `liferay-portal/tickets-in-release.sh`: take each commit subject's
    leading token and dedupe. Two deliberate differences from that script:

    * range is `A..B`, not `A...B`. The three-dot form is a symmetric
      difference; we want exactly the commits whose changes the diff analysed,
      or the ticket list would not describe the diff.
    * the key is matched with a regex rather than `awk '{print $1}'`, so
      subjects that do not start with a ticket (reverts, merges, "Bump …")
      are skipped instead of contributing junk keys.
    """
    seen: list[str] = []
    for _short, subject in fetch_commits_in_range(git_repo, hash_a, hash_b):
        m = _TICKET_RE.match(subject.strip())
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def commits_touching_file(git_repo: Path, hash_a: str, hash_b: str,
                          path: str, limit: int = 4) -> list[tuple[str, str, str]]:
    """Commits in `A..B` that touched `path` → [(short_hash, ticket, subject)].

    This is what turns a bare `culprit_file` into something actionable: the
    file alone says where, the ticket and commit say who changed it and why.
    Returns [] when the path does not resolve — an LLM-named path may not
    match the repo exactly, and a wrong guess must degrade to "no commits"
    rather than to a wrong attribution.
    """
    path = (path or "").strip()
    if not path or not (git_repo / ".git").is_dir():
        return []
    try:
        out = subprocess.run(
            ["git", "-C", str(git_repo), "log", "--no-merges",
             f"--max-count={limit}", "--pretty=format:%h\t%s",
             f"{hash_a}..{hash_b}", "--", path],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    rows = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        short, subject = line.split("\t", 1)
        m = _TICKET_RE.match(subject.strip())
        rows.append((short, m.group(1) if m else "", subject.strip()))
    return rows


def write_tickets_in_range(run_dir: Path, tickets: list[str], *,
                           hash_a: str, hash_b: str) -> Path:
    """Write the ticket list plus ready-to-paste JQL.

    This is the context a human needs when a wide build pair comes back mostly
    NEEDS_REVIEW with no culprit file: the classifier could not attribute the
    failure, but the reviewer can still see every ticket in the range.
    """
    keys = ",".join(tickets)
    lines = [
        f"# Unique tickets in {hash_a[:12]}..{hash_b[:12]}  ({len(tickets)})",
        "",
        *tickets,
        "",
        "# ---- JQL " + "-" * 60,
        "",
        f"key in ({keys}) and project = LPD and type = bug",
        "",
        "# Security vulnerabilities only",
        "",
        f'key in ({keys}) and project = LPD and type = bug and '
        f'(component in ("security vulnerability","Security Vulnerability") '
        f'OR "Cross Cutting Properties[Checkboxes]" in ("Security Vulnerability"))',
        "",
    ]
    out = run_dir / "tickets_in_range.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def describe_git_range(git_repo: Path, hash_a: str, hash_b: str) -> str:
    """One line naming the checkout, its branch, and whether both ends resolve.

    Worth printing because a silent mis-resolution looks identical to a real
    empty diff: the checkout was on `master` while the pair was a 2026.q1
    release pair, and nothing in the output said so.
    """
    def _git(*args) -> str:
        try:
            return subprocess.run(["git", "-C", str(git_repo), *args],
                                  capture_output=True, text=True,
                                  check=True, timeout=30).stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            return ""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "?"
    missing = [h[:12] for h in (hash_a, hash_b)
               if not _git("rev-parse", "--verify", "--quiet", f"{h}^{{commit}}")]
    note = f"  !! NOT in this checkout: {', '.join(missing)}" if missing else ""
    return f"repo {_disp(git_repo)} (branch {branch}){note}"

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
                 project_id: int | None = None, testray_url: str | None = None,
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

def compute_subtask_groups(df: pd.DataFrame,
                           mode: str = MODE_BY_SUBTASK) -> list[dict]:
    """Group regression cases for a grouped mode.

    `by-subtask` groups on Testray's `subtask_id`; `by-cluster` groups on our
    own error signature (§5). Both produce the same group shape, so the prompt
    writer, the group CSV and submit's fan-out are shared — the only thing that
    changes is the key. Cases with no key (subtask_id 0/NaN, or a blank error
    in cluster mode) become singleton groups so nothing is silently dropped.

    Returns a list of dicts, one per group:
        group_id        — 1-based int, stable within this run; what results.json
                          references and what submit fans out on
        signature       — normalized error signature (cluster mode) or ""
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
    if df.empty:
        return []
    if mode == MODE_BY_SUBTASK and "subtask_id" not in df.columns:
        return []

    work = df.copy()
    if mode == MODE_BY_CLUSTER:
        # Key on the normalized error signature. A blank signature means we
        # have no error text to group on, so those stay singletons rather than
        # collapsing into one giant "no error" cluster — that would fan a
        # single verdict across unrelated failures, which is the exact risk §7
        # names for this mode.
        work["_sig"] = [error_signature.normalize(e) or ""
                        for e in work["error_message"]]
        sig_key: dict[str, int] = {}
        keys, next_synth = [], -1
        for sig in work["_sig"]:
            if not sig:
                keys.append(next_synth)
                next_synth -= 1
                continue
            if sig not in sig_key:
                sig_key[sig] = len(sig_key) + 1
            keys.append(sig_key[sig])
        work["_grp_key"] = keys
    else:
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
            # Stable within the run and always positive: this is what
            # results.json references and what submit fans out on. In subtask
            # mode it IS the Testray subtask id (so existing bundles keep
            # working); in cluster mode it is a 1-based index, and unkeyed
            # singletons get their own id rather than sharing one.
            "group_id":            int(grp_key) if grp_key > 0 else abs(int(grp_key)) + 1_000_000,
            "signature":           (sub["_sig"].iloc[0] if "_sig" in sub.columns else ""),
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
            # How often this group's error signature already occurred among
            # the BASELINE's own failures. min() across members: if ANY member
            # is new, the group is new. Diagnostic only — it rides in
            # diff_list_subtasks.csv and is written to TriageResult, but is
            # rendered by neither report (the Baseline column was dropped from
            # both on 2026-08-26). It is deliberately kept OUT of the prompt
            # too: feeding it to the classifier (with a NOVEL /
            # rare / CHRONIC rubric) is the prime suspect for the 2026-08-24
            # run where TEST_FIX collapsed 639 rows -> 0. The rubric was
            # reverted 2026-08-26 without ever being committed, so git has no
            # copy of it; re-testing it means writing it again, deliberately.
            "baseline_signature_count": (
                int(sub["baseline_signature_count"].min())
                if "baseline_signature_count" in sub.columns
                and sub["baseline_signature_count"].notna().any() else None),
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
            "group_id":           g.get("group_id", ""),
            "signature":          g.get("signature", ""),
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
            "baseline_signature_count": (g.get("baseline_signature_count")
                                         if g.get("baseline_signature_count") is not None else ""),
        })
    pd.DataFrame(rows).to_csv(run_dir / "diff_list_subtasks.csv", index=False)


def write_prompt_subtask(run_dir: Path, *, run_id: str, classifier: str,
                          build_a: int, build_b: int, hash_a: str, hash_b: str,
                          routine_id: int | None, build_a_name: str, build_b_name: str,
                 project_id: int | None = None, testray_url: str | None = None,
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
        gid = g.get("group_id")
        sid = g["subtask_id"]
        members_label = f"{g['size']} case(s)"
        if g.get("signature"):
            # Cluster mode: the group IS the error signature, so name it —
            # it is the evidence for why these cases are one unit.
            header = f"### {idx}. Group {gid} — {members_label} sharing one error signature"
        elif sid is not None:
            header = f"### {idx}. Group {gid} (Testray subtask_id={sid}) — {members_label}"
        else:
            header = f"### {idx}. Group {gid} — {members_label}, no subtask link (singleton)"
        body_lines.append(header)

        meta_parts = [f"**group_id:** {gid}"]
        if g.get("signature"):
            meta_parts.append(f"**signature:** `{g['signature'][:120]}`")
        meta_parts.append(f"**case_ids:** {', '.join(str(c) for c in g['case_ids'][:8])}")
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
    mode: str = DEFAULT_MODE,
    fetch_specs: list | None = None,
    project_id: int | None = None,
    testray_url: str | None = None,
    transition_counts: dict | None = None,
    baseline_rows: int | None = None,
    target_rows: int | None = None,
    status_matrix: dict | None = None,
    coverage: dict | None = None,
    testray_cfg: dict | None = None,
    history_depth: int = HISTORY_DEPTH_DEFAULT,
    history_streak: int = HISTORY_STREAK_DEFAULT,
) -> Path:
    print(f"→ Step 3/6 git diff …")
    diff_path = run_dir / "git_diff_full.diff"
    # State the provenance before the diff runs. An empty or partial diff is
    # indistinguishable from a real "nothing changed" unless the range and the
    # checkout are on the record.
    print(f"   {describe_git_range(git_repo, hash_a, hash_b)}")
    print(f"   range {hash_a[:12]}..{hash_b[:12]}")
    diff_lines = run_git_diff(git_repo, hash_a, hash_b, diff_path,
                              fetch_specs=fetch_specs)
    n_files = 0
    try:
        n_files = sum(1 for ln in diff_path.open(encoding="utf-8", errors="replace")
                      if ln.startswith("diff --git"))
    except OSError:
        pass
    print(f"   {diff_lines} lines, {n_files} file(s) after exclusions "
          f"→ {_disp(diff_path)}")
    if diff_lines == 0:
        print("   WARNING: the diff is EMPTY. Either the two builds ran the "
              "same commit, or the range is wrong — no hunk can be matched "
              "and no culprit_file can be attributed.", file=sys.stderr)

    tickets = collect_tickets_in_range(git_repo, hash_a, hash_b)
    if tickets:
        tickets_path = write_tickets_in_range(run_dir, tickets,
                                              hash_a=hash_a, hash_b=hash_b)
        print(f"   {len(tickets)} unique ticket(s) in range "
              f"→ {_disp(tickets_path)}")

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

    # Per-case history. The baseline is ONE build: when it did not run a test,
    # or ran it on a bad day, a test broken for months reads as a regression.
    # History is the only thing that can say "this has not passed in N builds".
    if history_depth and testray_cfg:
        before = int(df["pre_classification"].notna().sum())
        hist = fetch_case_history(
            df["testray_case_id"], testray_cfg,
            depth=history_depth,
            exclude_caseresult_ids=set(
                int(c) for c in df.get("caseresult_id", []) if pd.notna(c)),
        )
        df = attach_history(df, hist, streak_threshold=history_streak)
        n_chronic = int((df["pre_classification"] == PRE_EXISTING).sum())
        got = sum(1 for v in hist.values() if v)
        print(f"   history: {got}/{len(hist)} case(s) resolved — "
              f"{n_chronic} have not passed in {history_streak}+ consecutive "
              f"builds → {PRE_EXISTING} (excluded from classification)")
        if got < len(hist):
            print(f"   WARNING: history unavailable for {len(hist) - got} case(s); "
                  f"they stay in the classify set.", file=sys.stderr)

    df_flaky    = df[df["known_flaky"].fillna(False)].copy()
    df_nonflaky = df[~df["known_flaky"].fillna(False)].copy()
    df_auto     = df_nonflaky[df_nonflaky["pre_classification"].notna()].copy()
    df_to_cls   = df_nonflaky[df_nonflaky["pre_classification"].isna()].copy()
    print(f"   {len(df)} unique cases: "
          f"{len(df_to_cls)} to classify, {len(df_auto)} auto, "
          f"{len(df_flaky)} flaky (excluded)")

    diff_list_cols = [
        "testray_case_id", "test_case", "component_name", "team_name",
        "status_a", "status_b", "transition", "known_flaky", "linked_issues",
        "error_message", "baseline_error_message", "pre_classification",
    ]
    # Carries the TriageResult FK through to submit — absent only when the
    # target side had no caseresult ids, in which case verdicts write unlinked.
    if "caseresult_id" in df.columns:
        diff_list_cols.append("caseresult_id")
    if "subtask_id" in df.columns:
        diff_list_cols.append("subtask_id")
    if "baseline_signature_count" in df.columns:
        diff_list_cols.append("baseline_signature_count")
    for col in ("history_depth", "history_fail_streak"):
        if col in df.columns:
            diff_list_cols.append(col)
    df[diff_list_cols].to_csv(run_dir / "diff_list.csv", index=False)

    print(f"→ Step 6/6 prompt + schema + run.yml …")

    if mode in GROUPED_MODES:
        if mode == MODE_BY_SUBTASK and "subtask_id" not in df.columns:
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
        all_groups      = compute_subtask_groups(df, mode=mode)
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
        label = "clusters" if mode == MODE_BY_CLUSTER else "subtask groups"
        print(f"   {label}: {len(groups_to_cls)} to classify "
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
        project_id=project_id,
        testray_url=testray_url,
        transition_counts=transition_counts,
        baseline_rows=baseline_rows,
        target_rows=target_rows,
        status_matrix=status_matrix,
        coverage=coverage,
    )

    rel = _disp(run_dir)
    print(f"\nRun bundle ready: {rel}")
    print(f"Next:  testray-analysis classify {rel}")
    print(f"       (or classify {rel}/prompt.md by hand in a Claude Code session)")
    print(f"Then:  testray-analysis submit {rel}")
    return run_dir


def prepare(baseline: SideSpec, target: SideSpec, classifier: str,
            mode: str = DEFAULT_MODE, out_dir: Path | None = None,
            fetch_specs: list | None = None,
            history_depth: int = HISTORY_DEPTH_DEFAULT,
            history_streak: int = HISTORY_STREAK_DEFAULT) -> Path:
    validate_combo(baseline, target)
    validate_mode(baseline, target, mode)

    cfg         = load_config()   # applies the TESTRAY_* env overrides
    print(f"Testray:    {testray_target(cfg)}")
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

    status_matrix: dict = {}
    coverage: dict = {}
    df, transitions = compute_test_diff(baseline_df, target_df,
                                        matrix_out=status_matrix,
                                        coverage_out=coverage)
    if df.empty:
        raise SystemExit(
            "test_diff returned 0 triage rows. Transitions seen: "
            f"{dict(transitions) or 'none (no cases matched across the pair)'}")
    # Report the excluded buckets too — "847 same_failure" is the number that
    # tells you the baseline was already broken, and it used to be invisible.
    print(f"   {len(df)} triage rows — " +
          ", ".join(f"{n} {name}" for name, n in sorted(transitions.items(),
                                                        key=lambda kv: -kv[1])))

    # The join is an inner join on case id, so the transition total IS how many
    # cases were comparable. When that is a small share of the target build the
    # two builds ran different test sets, and every verdict below describes that
    # sliver rather than the build. Say it here as well as in the report: a run
    # triggered from the UI is read in this log first.
    joined = sum(transitions.values())
    if joined and len(target_df) and joined < len(target_df) * 0.5:
        print(f"   WARNING: only {joined} of {len(target_df)} target case "
              f"results ({100.0 * joined / len(target_df):.1f}%) ran on BOTH "
              f"builds. The suite was probably re-selected between them; "
              f"verdicts will cover only that overlap, not the build.",
              file=sys.stderr)

    # The other direction, which the check above cannot see: the target ran
    # FEWER cases than the baseline. Overlap-vs-target stays near 100% while
    # half the suite quietly stops running, so this needs its own test.
    only_b = coverage.get("baseline_only") or 0
    b_cases = coverage.get("baseline_cases") or 0
    if b_cases and only_b > b_cases * 0.1:
        print(f"   WARNING: {only_b} of {b_cases} baseline cases "
              f"({100.0 * only_b / b_cases:.1f}%) did NOT run on the target "
              f"build. Those tests produce no failure and no row here — a "
              f"drop in coverage, not a clean build.", file=sys.stderr)

    # api caseresults don't carry case names. Backfill test_case from the
    # Testray case object so the fragment matcher has something to anchor on
    # (and so prompt.md doesn't say `### N. \`\`` with no test name).
    df = enrich_api_case_names(df, cfg["testray"])

    # How often each failure's error signature already occurred in the BASELINE
    # build. 0 = the baseline never produced this error. Reuses the case
    # results already fetched above, so this costs no extra I/O.
    prevalence = baseline_signature_prevalence(baseline_df)
    df = attach_baseline_prevalence(df, prevalence)
    if prevalence:
        novel = int((df["baseline_signature_count"] == 0).sum())
        chronic = int((df["baseline_signature_count"] >= 5).sum())
        print(f"   baseline signatures: {len(prevalence)} distinct across its own "
              f"failures — {novel}/{len(df)} triage rows are novel, "
              f"{chronic} already chronic (5+ occurrences)")

    # Resolve component/team from the FKs the diff carried through. Must run
    # BEFORE enrich_and_pre_classify, which renames testray_component_name to
    # component_name and applies the local component->team map on top.
    df = resolve_component_team_names(df, cfg["testray"])
    if "testray_component_name" in df.columns:
        n_comp = int((~df["testray_component_name"].map(_is_blank)).sum())
        n_team = int((~df["team_name"].map(_is_blank)).sum()) \
            if "team_name" in df.columns else 0
        print(f"   resolved component on {n_comp}/{len(df)} row(s), "
              f"team on {n_team}/{len(df)}")

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

    project_id = meta_a["project_id"] or meta_b["project_id"]

    # A PR build's commit is on a fork, so origin will not have it. Derive the
    # fetch from the build name rather than making the caller supply it — but
    # only when they have not, since an explicit --fetch-ref is a deliberate
    # override and must win.
    if not fetch_specs:
        derived = [
            spec for spec in (pr_fetch_spec(meta_a.get("name"), cfg),
                              pr_fetch_spec(meta_b.get("name"), cfg))
            if spec
        ]
        # dict.fromkeys: both sides of a PR-vs-PR pair can name the same ref.
        fetch_specs = list(dict.fromkeys(derived))
        for src, ref in fetch_specs:
            print(f"   pull request detected — will fetch {ref} from {src}")

    return _finalize_bundle(
        df=df, run_id=run_id, run_dir=run_dir,
        classifier=classifier,
        baseline_source=baseline.source, target_source=target.source,
        build_a=baseline.build_id, build_b=target.build_id,
        hash_a=meta_a["git_hash"], hash_b=meta_b["git_hash"],
        routine_id=routine_id,
        build_a_name=meta_a["build_name"], build_b_name=meta_b["build_name"],
        project_id=project_id,
        testray_url=testray_ui_url(cfg["testray"]),
        transition_counts=dict(transitions),
        baseline_rows=len(baseline_df),
        target_rows=len(target_df),
        status_matrix=status_matrix,
        coverage=coverage,
        testray_cfg=cfg["testray"],
        history_depth=history_depth,
        history_streak=history_streak,
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


# Testray names a pull-request build
#   [master] ci:test:object - shuyangzhou > shuyangzhou - PR#12591 - 2026-08-19[…]
# i.e. `[branch] job - <sender> > <receiver> - PR#<number> - <timestamp>`.
# Verified against every PR build in the mirror. Nothing else on the Build object
# references the pull request at all — `description` carries only a Jenkins
# report link and the portal branch/SHA — so the name is the sole source.
_PR_BUILD_RE = re.compile(
    r"-\s*(?P<sender>\S+)\s*>\s*(?P<receiver>\S+)\s*-\s*PR#(?P<number>\d+)\b"
)

# The PR head lives on the repo the pull request was opened *against*, which is
# the receiver — `pull/<n>/head` is a ref on the target repo, not the sender's.
# The repo is always liferay-portal; only the owner varies.
PR_REMOTE_TEMPLATE = "git@github.com:{owner}/liferay-portal.git"


def pr_fetch_spec(build_name, cfg: dict | None = None) -> tuple[str, str] | None:
    """`(remote, ref)` to fetch a PR build's commit, or None if not a PR build.

    A PR build's commit is on a fork, so it is not on origin and `git diff`
    cannot see it — the run fails with "NOT in this checkout". This is the same
    fetch a human would do by hand (`git fetch <fork> pull/<n>/head`), derived
    from the build name so a UI-triggered PR run needs no extra input.

    Override the remote via `git.pr_remote_template` in config.yml, which takes
    `{owner}`; the default assumes GitHub over SSH.
    """
    match = _PR_BUILD_RE.search(str(build_name or ""))
    if not match:
        return None
    template = str(((cfg or {}).get("git") or {}).get("pr_remote_template")
                   or PR_REMOTE_TEMPLATE)
    return (template.format(owner=match.group("receiver")),
            f"pull/{match.group('number')}/head")


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
    ap.add_argument("--mode", choices=MODES, default=DEFAULT_MODE,
                    help="Classification granularity (ARCHITECTURE §7). "
                         "by-cluster (default) groups failures by error "
                         "signature before classifying and fans one verdict "
                         "out to each member; per-test classifies every "
                         "failure separately; by-subtask groups on Testray's "
                         "Subtask instead of our signature.")
    ap.add_argument("--by-subtask", action="store_true",
                    help="Deprecated alias for --mode by-subtask.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Directory to write the run bundle into "
                         "(default: ./runs). On Jenkins, point this at the "
                         "workspace/artifacts dir.")
    ap.add_argument("--history-depth", type=int, default=HISTORY_DEPTH_DEFAULT,
                    help="How many prior case results to read per test when "
                         "deciding whether a failure is pre-existing. 0 "
                         "disables the history fetch entirely "
                         f"(default {HISTORY_DEPTH_DEFAULT}).")
    ap.add_argument("--history-streak", type=int, default=HISTORY_STREAK_DEFAULT,
                    help="Consecutive builds without a PASS before a failure "
                         "is treated as pre-existing rather than new "
                         f"(default {HISTORY_STREAK_DEFAULT}).")
    ap.add_argument("--fetch-ref", nargs="+", action="append", default=[],
                    help="Fetch a build commit not on origin (e.g. a temp "
                         "mitigation branch on a fork). Two forms: "
                         "`<remote-or-url> <ref>`, or a single GitHub "
                         "`…/tree/<branch>` URL. Repeatable.")

    args = ap.parse_args()
    fetch_specs = [_normalize_fetch_ref(e) for e in args.fetch_ref]
    baseline = _build_spec(args, "baseline")
    target   = _build_spec(args, "target")
    # --by-subtask is kept as an alias so existing invocations keep working;
    # an explicit --mode always wins.
    mode     = MODE_BY_SUBTASK if args.by_subtask else args.mode
    prepare(baseline, target, args.classifier, mode=mode, out_dir=args.out,
            history_depth=args.history_depth, history_streak=args.history_streak,
            fetch_specs=fetch_specs)


if __name__ == "__main__":
    main()
