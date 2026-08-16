"""Mirror a routine + a set of builds (ALL their caseresults) from a PROD
Testray into the LOCAL instance.

Build ids are read from one or more saved /o/c/builds response JSONs (the
`items` array), unioned + deduped, then capped at N (default 10). Save those
yourself off the prod instance, e.g.

    curl -H "Authorization: Bearer $TOKEN" \
      "https://testray.liferay.com/o/c/builds?filter=r_routineToBuilds_c_routineId%20eq%20'79529'&pageSize=50" \
      > builds.json

Everything is keyed by a stable externalReferenceCode so FKs are remapped
prod-id -> local-id and reruns upsert (idempotent). Cases are deduped across
builds and mirrored once each. gitHash comes from each build's detail GET.

    Routine -> Build[] -> CaseResults (all)   (build + case FK remapped)
    Cases (union across builds, deduped)

Component / team / subtask FKs are skipped (enrichment-only).

Creds:
  LOCAL  <- config/config.yml `testray` block.
  PROD   <- env: PROD_TESTRAY_CLIENT_ID, PROD_TESTRAY_CLIENT_SECRET,
            PROD_TESTRAY_BASE_URL (default https://testray.liferay.com);
            falls back to `testray.prod_client_id` / `prod_client_secret` /
            `prod_url` in config.yml when the env vars are unset.

    export PROD_TESTRAY_CLIENT_ID=...  PROD_TESTRAY_CLIENT_SECRET=...
    python3 tests/manual/mirror_prod_to_local.py <builds.json[,builds2.json...]> [limit] [routineId]

Scale: an acceptance build carries ~15k caseresults, so a two-build mirror is
~45k writes. Two things keep that tractable — cases are read from prod in
batches of BATCH_SIZE via `filter=id in (...)` instead of one GET each, and
local writes run through a WORKERS-wide thread pool (a single local PUT is
~64ms, so serial writes alone would be ~50 minutes).
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from testray_analytics.analysis.prepare import (
    load_config, _testray_oauth_token, _testray_fetch_paginated,
)

USAGE = ("usage: python3 tests/manual/mirror_prod_to_local.py "
         "<builds.json[,builds2.json...]> [limit] [routineId]")

if len(sys.argv) < 2:
    raise SystemExit(USAGE)

BUILDS_FILES = [os.path.expanduser(p) for p in sys.argv[1].split(",")]
LIMIT      = int(sys.argv[2]) if len(sys.argv) > 2 else 10
ROUTINE_ID = int(sys.argv[3]) if len(sys.argv) > 3 else 79529

CASE_FK = "r_caseToCaseResult_c_caseId"

# Local writes are independent ERC-keyed upserts, so they parallelize cleanly.
WORKERS = 8
# Case ids per `filter=id in (...)` page when reading cases back from prod.
BATCH_SIZE = 100


def run_parallel(fn, items, label):
    """Map fn over items across WORKERS threads. Returns (ok, failures).
    A failure never aborts the rest — this mirrors tens of thousands of rows
    and one bad id shouldn't cost the whole run."""
    total = len(items)
    state = {"done": 0, "ok": 0}
    failures = []
    lock = threading.Lock()
    step = max(1, total // 20)

    def _one(item):
        try:
            fn(item)
            ok, err = True, None
        except Exception as e:                      # noqa: BLE001 - reported below
            ok, err = False, e
        with lock:
            state["done"] += 1
            if ok:
                state["ok"] += 1
            else:
                failures.append((item, err))
            if state["done"] % step == 0 or state["done"] == total:
                print(f"  [{label}] {state['done']}/{total} "
                      f"({len(failures)} failed)", flush=True)

    if total:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(_one, items))

    # Concurrent PUTs occasionally lose a race in Liferay and come back
    # "400 The service parameter was not provided by this object" — the same
    # row writes fine on its own. Retry the stragglers serially before
    # reporting them as real failures.
    if failures:
        retry, failures = failures, []
        print(f"  [{label}] retrying {len(retry)} failed write(s) serially")
        for item, _ in retry:
            try:
                fn(item)
                state["ok"] += 1
            except Exception as e:                  # noqa: BLE001 - reported below
                failures.append((item, e))
    return state["ok"], failures


def _key(v):
    return v.get("key") if isinstance(v, dict) else v


class Client:
    """Testray REST client that re-mints its token on a 401 (prod tokens
    expire ~10 min; long mirrors outlive them)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg["base_url"].rstrip("/")
        self.token = _testray_oauth_token(cfg)

    def _raw(self, method, path, body=None):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.base}{path}", data=data,
                                     method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def _go(self, method, path, body=None):
        """401 -> re-mint token and retry; 5xx -> retry with backoff (local
        Liferay throws transient 500s under write load). On final failure,
        print the response body and re-raise the HTTPError."""
        last = None
        for attempt in range(5):
            try:
                return self._raw(method, path, body)
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 401:
                    self.token = _testray_oauth_token(self.cfg)
                    continue
                if 500 <= e.code < 600:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break  # non-retryable 4xx
        detail = ""
        try:
            detail = last.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        print(f"  ! {method} {path} -> HTTP {last.code} {detail}", flush=True)
        raise last

    def get(self, path):
        return self._go("GET", path)

    def put_erc(self, plural, erc, body):
        path = (f"/o/c/{plural}/by-external-reference-code/"
                f"{urllib.parse.quote(erc, safe='')}")
        return self._go("PUT", path, body)


_local_cfg = load_config()["testray"]
# http:// on the prod host just redirects; urllib won't carry the POST body
# across that, so normalize before minting a token.
_prod_url = (os.environ.get("PROD_TESTRAY_BASE_URL")
             or _local_cfg.get("prod_url")
             or "https://testray.liferay.com").replace("http://", "https://")
prod = Client({
    "base_url":      _prod_url,
    "client_id":     os.environ.get("PROD_TESTRAY_CLIENT_ID")     or _local_cfg.get("prod_client_id"),
    "client_secret": os.environ.get("PROD_TESTRAY_CLIENT_SECRET") or _local_cfg.get("prod_client_secret"),
})
if not prod.cfg["client_id"] or not prod.cfg["client_secret"]:
    raise SystemExit("Set PROD_TESTRAY_CLIENT_ID and PROD_TESTRAY_CLIENT_SECRET "
                     "(and optionally PROD_TESTRAY_BASE_URL) in the environment, "
                     "or testray.prod_client_id / prod_client_secret in config.yml.")
local = Client(_local_cfg)

_records, _seen = [], set()
for f in BUILDS_FILES:
    for it in json.load(open(f))["items"]:
        if it["id"] not in _seen:
            _seen.add(it["id"])
            _records.append((it.get("dateCreated") or "", it["id"]))
_records.sort(reverse=True)                 # most recent first
build_ids = [bid for _, bid in _records][:LIMIT]
print(f"PROD  {prod.base}\nLOCAL {local.base}")
print(f"routine {ROUTINE_ID}, {len(build_ids)} most-recent builds "
      f"(of {len(_seen)} in {len(BUILDS_FILES)} file(s)): {build_ids}\n")

# 1. Project + Routine. The project FK is what makes the mirrored rows
# reachable in the Testray UI (project -> routine -> build); the CLI itself
# never needs it, since it filters caseresults by build id.
r = prod.get(f"/o/c/routines/{ROUTINE_ID}")
prod_project_id = r.get("r_routineToProjects_c_projectId")
local_project = None
if prod_project_id:
    # Project linkage is what makes the rows browsable in the Testray UI, but
    # it is not needed by the CLI — so a missing c_project scope downgrades to
    # a warning instead of killing a 45k-row mirror.
    try:
        p = prod.get(f"/o/c/projects/{prod_project_id}")
        local_project = local.put_erc("projects", f"prod-project-{prod_project_id}",
            {"name": p.get("name") or f"project-{prod_project_id}"})["id"]
        print(f"project {prod_project_id} -> local {local_project}  ({p.get('name')})")
    except urllib.error.HTTPError as e:
        print(f"project {prod_project_id} SKIPPED: HTTP {e.code} — mirroring "
              f"without project linkage (rows stay invisible in the Testray UI; "
              f"grant c_project.everything to fix)")
else:
    print("routine has no project FK — routine/cases will be UI-invisible")

routine_body = {"name": r.get("name") or f"routine-{ROUTINE_ID}",
                "autoanalyze": bool(r.get("autoanalyze"))}
if local_project:
    routine_body["r_routineToProjects_c_projectId"] = local_project
local_routine = local.put_erc("routines", f"prod-routine-{ROUTINE_ID}",
                              routine_body)["id"]
print(f"routine {ROUTINE_ID} -> local {local_routine}  ({r.get('name')})\n")

# 2. Builds + their caseresults (gitHash from the build detail GET).
build_map = {}
cr_by_build = {}
all_case_ids = set()
for pb in build_ids:
    try:
        b = prod.get(f"/o/c/builds/{pb}")
        body = {"name": b.get("name") or f"build-{pb}",
                "r_routineToBuilds_c_routineId": local_routine}
        # Builds carry their own project FK in addition to the routine's; the
        # Testray UI needs it to place the build under a project.
        if local_project:
            body["r_projectToBuilds_c_projectId"] = local_project
        if b.get("gitHash"):
            body["gitHash"] = b["gitHash"]
        if _key(b.get("dueStatus")):
            body["dueStatus"] = {"key": _key(b["dueStatus"])}
        build_map[pb] = local.put_erc("builds", f"prod-build-{pb}", body)["id"]
    except urllib.error.HTTPError as e:
        print(f"build {pb} FAILED, skipping: HTTP {e.code}", flush=True)
        continue

    rows = _testray_fetch_paginated("/o/c/caseresults",
        {"filter": f"r_buildToCaseResult_c_buildId eq '{pb}'",
         "fields": f"id,dueStatus,errors,{CASE_FK}"},
        token=prod.token, base_url=prod.base)
    cr_by_build[pb] = rows
    all_case_ids.update(int(it[CASE_FK]) for it in rows if it.get(CASE_FK))
    print(f"build {pb} -> local {build_map[pb]}  "
          f"(gitHash {(b.get('gitHash') or 'none')[:12]}, {len(rows)} caseresults)")

# 3. Cases (deduped union). Read from prod in batches, then write locally in
# parallel — one GET per case would be ~15k serial round-trips to prod.
sorted_case_ids = sorted(all_case_ids)
print(f"\nreading {len(sorted_case_ids)} unique cases from prod "
      f"in batches of {BATCH_SIZE} ...")
prod_cases = {}
for start in range(0, len(sorted_case_ids), BATCH_SIZE):
    chunk = sorted_case_ids[start:start + BATCH_SIZE]
    q = urllib.parse.urlencode({
        "filter": "id in (" + ",".join(f"'{i}'" for i in chunk) + ")",
        "fields": "id,name,flaky",
        "pageSize": BATCH_SIZE,
    })
    try:
        for c in prod.get(f"/o/c/cases?{q}").get("items", []):
            prod_cases[int(c["id"])] = c
    except urllib.error.HTTPError as e:
        print(f"  ! case batch at {start} FAILED: HTTP {e.code}")
    if (start // BATCH_SIZE) % 10 == 0:
        print(f"  read {len(prod_cases)}/{len(sorted_case_ids)}", flush=True)
missing = [i for i in sorted_case_ids if i not in prod_cases]
if missing:
    print(f"  {len(missing)} case id(s) not returned by prod — "
          f"mirrored with a placeholder name")

print(f"writing {len(sorted_case_ids)} cases locally ...")
case_map = {}
_case_lock = threading.Lock()

def _write_case(cid):
    c = prod_cases.get(cid, {})
    body = {"name": c.get("name") or f"case-{cid}", "flaky": bool(c.get("flaky"))}
    if local_project:
        body["r_projectToCases_c_projectId"] = local_project
    local_id = local.put_erc("cases", f"prod-case-{cid}", body)["id"]
    with _case_lock:
        case_map[cid] = local_id

_, case_failures = run_parallel(_write_case, sorted_case_ids, "cases")
case_fail = len(case_failures)
for cid, err in case_failures[:5]:
    print(f"  case {cid} FAILED: {err}")

# 4. CaseResults (all, every build). Every build's rows are mirrored, not just
# the failures — prepare.py recomputes the PASSED->FAILED diff from the local
# data, and pre-filtering here would quietly decide that answer for it.
cr_ok = cr_fail = 0
for pb in build_ids:
    if pb not in build_map:
        continue
    lb = build_map[pb]

    def _write_cr(it, lb=lb):
        pcid = it.get(CASE_FK)
        body = {"errors": it.get("errors"), "r_buildToCaseResult_c_buildId": lb}
        if pcid and int(pcid) in case_map:
            body["r_caseToCaseResult_c_caseId"] = case_map[int(pcid)]
        if _key(it.get("dueStatus")):
            body["dueStatus"] = {"key": _key(it["dueStatus"])}
        local.put_erc("caseresults", f"prod-caseresult-{it['id']}", body)

    ok, failures = run_parallel(_write_cr, cr_by_build[pb], f"caseresults {pb}")
    cr_ok += ok
    cr_fail += len(failures)
    for it, err in failures[:5]:
        print(f"  caseresult {it['id']} FAILED: {err}")
    print(f"build {pb}: {ok} caseresults written ({cr_ok} ok so far)")

print(f"\nDONE. builds: {len(build_map)}; cases: {len(case_map)} ok / {case_fail} failed; "
      f"caseresults: {cr_ok} ok / {cr_fail} failed.")
print(f"local routine {local_routine}, builds {list(build_map.values())}")
