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
            PROD_TESTRAY_BASE_URL (default https://testray.liferay.com).

    export PROD_TESTRAY_CLIENT_ID=...  PROD_TESTRAY_CLIENT_SECRET=...
    python3 tests/manual/mirror_prod_to_local.py <builds.json[,builds2.json...]> [limit] [routineId]
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

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


prod = Client({
    "base_url":      os.environ.get("PROD_TESTRAY_BASE_URL", "https://testray.liferay.com"),
    "client_id":     os.environ.get("PROD_TESTRAY_CLIENT_ID"),
    "client_secret": os.environ.get("PROD_TESTRAY_CLIENT_SECRET"),
})
if not prod.cfg["client_id"] or not prod.cfg["client_secret"]:
    raise SystemExit("Set PROD_TESTRAY_CLIENT_ID and PROD_TESTRAY_CLIENT_SECRET "
                     "(and optionally PROD_TESTRAY_BASE_URL) in the environment.")
local = Client(load_config()["testray"])

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

# 1. Routine.
r = prod.get(f"/o/c/routines/{ROUTINE_ID}")
local_routine = local.put_erc("routines", f"prod-routine-{ROUTINE_ID}",
    {"name": r.get("name") or f"routine-{ROUTINE_ID}",
     "autoanalyze": bool(r.get("autoanalyze"))})["id"]
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

# 3. Cases (deduped union).
print(f"\nmirroring {len(all_case_ids)} unique cases ...")
case_map = {}
case_fail = 0
for i, cid in enumerate(sorted(all_case_ids), 1):
    try:
        c = prod.get(f"/o/c/cases/{cid}")
        case_map[cid] = local.put_erc("cases", f"prod-case-{cid}",
            {"name": c.get("name") or f"case-{cid}",
             "flaky": bool(c.get("flaky"))})["id"]
    except urllib.error.HTTPError as e:
        case_fail += 1
        print(f"  case {cid} FAILED: HTTP {e.code}")
    if i % 25 == 0:
        print(f"  {i}/{len(all_case_ids)} cases")

# 4. CaseResults (all, every build).
cr_ok = cr_fail = 0
for pb in build_ids:
    if pb not in build_map:
        continue
    lb = build_map[pb]
    for it in cr_by_build[pb]:
        pcid = it.get(CASE_FK)
        body = {"errors": it.get("errors"), "r_buildToCaseResult_c_buildId": lb}
        if pcid and int(pcid) in case_map:
            body["r_caseToCaseResult_c_caseId"] = case_map[int(pcid)]
        if _key(it.get("dueStatus")):
            body["dueStatus"] = {"key": _key(it["dueStatus"])}
        try:
            local.put_erc("caseresults", f"prod-caseresult-{it['id']}", body)
            cr_ok += 1
        except urllib.error.HTTPError as e:
            cr_fail += 1
            print(f"  caseresult {it['id']} FAILED: HTTP {e.code}")
        if (cr_ok + cr_fail) % 500 == 0:
            print(f"  ... {cr_ok + cr_fail} caseresults written")
    print(f"build {pb}: caseresults written ({cr_ok} ok so far)")

print(f"\nDONE. builds: {len(build_map)}; cases: {len(case_map)} ok / {case_fail} failed; "
      f"caseresults: {cr_ok} ok / {cr_fail} failed.")
print(f"local routine {local_routine}, builds {list(build_map.values())}")
