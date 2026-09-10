#!/usr/bin/env python3
"""
loadTestrayData.py — part 2 of the local Testray setup.

Copies a project's structure and its most recent promoted builds from prod
Testray into the local instance, so there is something real to work against.

    project -> teams -> components -> case types -> routines
            -> promoted builds -> cases -> case results

Everything is keyed by a stable externalReferenceCode (`prod-<type>-<id>`), so
prod ids remap to local ids and reruns upsert rather than duplicate. Local ids
are assigned by Liferay and will not match prod's — the ERC is the join.

Credentials come from testray-analytics/config/config.yml:

    testray:
      base_url:           http://localhost:8080
      client_id:          <local>          # or local_client_id
      client_secret:      <local>          # or local_client_secret
      prod_url:           https://testray.liferay.com
      prod_client_id:     <prod, read-only is enough>
      prod_client_secret: <prod>

    ./loadTestrayData.py                    # project 473116959, 5 promoted builds
    ./loadTestrayData.py --build-limit 2    # fewer builds (each is ~6.5k results)
    ./loadTestrayData.py --skip-caseresults # structure only, seconds not minutes
    ./loadTestrayData.py --project 473116959
"""

import argparse
import json
import os
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CONFIG = os.environ.get("TRIAGE_CONFIG") or str(
    pathlib.Path(__file__).resolve().parents[2] / "config" / "config.yml")
DEFAULT_PROJECT = 473116959
DEFAULT_BUILD_LIMIT = 5

# Unpromoted builds are noise: transient CI runs that were never blessed as a
# reference point. The one exception is the stable-CI routine below, where
# nothing is ever promoted, so "promoted only" would return zero. Any other
# (project, routine) must stick to promoted builds — add a pair here, with a
# reason, if that ever stops being true.
ALL_BUILDS_ALLOWED = {
    (35392, 79529),   # Liferay Portal 7.4 / ci:test:stable — 0 promoted by design
    # TEMPORARY (2026-09-04, Nikki). EE Development Acceptance DOES promote
    # builds — 30 all time — but none since 2026-08-24, and the triage work
    # needs recent ones to test against. This is the case the guard exists to
    # refuse, so it is opened deliberately and narrowly rather than by relaxing
    # the rule: unpromoted builds here ARE transient CI runs, and anything
    # mirrored under this entry is a working copy, not a reference point.
    # Remove once 590307 has promoted builds in the window of interest.
    (35392, 590307),  # EE Development Acceptance — TEMPORARY, see above
    # ci:test:cms, the CMS PoC routine. 0 promoted builds all time, same shape
    # as stable: promotion is not part of this routine's workflow, so
    # "promoted only" returns nothing. Permanent, unlike the 590307 entry.
    (35392, 336020509),  # Liferay Portal 7.4 / ci:test:cms — 0 promoted by design
}

WORKERS = 8          # local writes are independent ERC upserts
BATCH = 100          # ids per `filter=id in (...)` page
PAGE = 500


# --- config -----------------------------------------------------------------

def load_config(path):
    try:
        import yaml
    except ImportError:
        sys.exit("pyyaml missing — run with the testray-analytics venv python:\n"
                 "  .venv/bin/python " + sys.argv[0])
    with open(path) as f:
        cfg = yaml.safe_load(f)["testray"]
    # `local_*` if present, else the plain keys the triage pipeline already uses.
    local = {
        "base_url": cfg["base_url"],
        "client_id": cfg.get("local_client_id") or cfg.get("client_id"),
        "client_secret": cfg.get("local_client_secret") or cfg.get("client_secret"),
    }
    prod = {
        # http:// on the prod host redirects, and urllib will not carry a POST
        # body across that redirect.
        "base_url": (cfg.get("prod_url") or "https://testray.liferay.com").replace(
            "http://", "https://"),
        "client_id": os.environ.get("PROD_TESTRAY_CLIENT_ID") or cfg.get("prod_client_id"),
        "client_secret": os.environ.get("PROD_TESTRAY_CLIENT_SECRET") or cfg.get("prod_client_secret"),
    }
    for name, c in (("local", local), ("prod", prod)):
        if not c["client_id"] or not c["client_secret"]:
            sys.exit(f"missing {name} credentials in {path}")
    return local, prod


# --- client -----------------------------------------------------------------

class Client:
    """Testray REST client: re-mints its token on a 401 and retries 5xx."""

    def __init__(self, cfg, label):
        self.cfg = cfg
        self.label = label
        self.base = cfg["base_url"].rstrip("/")
        self.token = self._mint()

    def _mint(self):
        data = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.cfg["client_id"],
            "client_secret": self.cfg["client_secret"],
        }).encode()
        for attempt in range(3):
            try:
                req = urllib.request.Request(f"{self.base}/o/oauth2/token", data=data)
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())["access_token"]
            except Exception as e:                                  # noqa: BLE001
                if attempt == 2:
                    sys.exit(f"{self.label}: token mint failed: {e}")
                time.sleep(2 * (attempt + 1))

    def _raw(self, method, path, body=None, timeout=90):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.base}{path}", data=data,
                                     method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return json.loads(raw) if raw else {}

    def request(self, method, path, body=None):
        last = None
        for attempt in range(3):
            try:
                return self._raw(method, path, body)
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 401:
                    self.token = self._mint()
                    continue
                if e.code < 500:
                    raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
            time.sleep(1.0 * (attempt + 1))
        raise last

    def get(self, path, **params):
        if params:
            path += "?" + urllib.parse.urlencode(params)
        return self.request("GET", path)

    def paginate(self, endpoint, **params):
        """Walk every page. Sorted by id so paging is stable — without an
        explicit sort the server may repeat rows on one page and skip others."""
        params.setdefault("sort", "id:asc")
        params["pageSize"] = PAGE
        out, page = [], 1
        while True:
            params["page"] = page
            data = self.get(endpoint, **params)
            out.extend(data.get("items", []))
            if page >= data.get("lastPage", 1):
                return out
            page += 1

    def upsert(self, plural, erc, body):
        path = (f"/o/c/{plural}/by-external-reference-code/"
                f"{urllib.parse.quote(erc, safe='')}")
        return self.request("PUT", path, body)


# --- helpers ----------------------------------------------------------------

def key_of(v):
    return v.get("key") if isinstance(v, dict) else v


def run_parallel(fn, items, label):
    """Map fn over items across WORKERS threads, then retry failures serially.
    Concurrent PUTs occasionally lose a race in Liferay and come back
    '400 The service parameter was not provided by this object'; the same row
    writes fine on its own."""
    total = len(items)
    if not total:
        return 0, []
    state = {"done": 0, "ok": 0}
    failures = []
    lock = threading.Lock()
    step = max(1, total // 10)

    def one(item):
        try:
            fn(item)
            ok, err = True, None
        except Exception as e:                                      # noqa: BLE001
            ok, err = False, e
        with lock:
            state["done"] += 1
            if ok:
                state["ok"] += 1
            else:
                failures.append((item, err))
            if state["done"] % step == 0 or state["done"] == total:
                print(f"    [{label}] {state['done']}/{total} "
                      f"({len(failures)} failed)", flush=True)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(one, items))

    if failures:
        retry, failures = failures, []
        print(f"    [{label}] retrying {len(retry)} serially", flush=True)
        for item, _ in retry:
            try:
                fn(item)
                state["ok"] += 1
            except Exception as e:                                  # noqa: BLE001
                failures.append((item, e))
    return state["ok"], failures


def report(label, ok, failures):
    if failures:
        print(f"  {label}: {ok} ok, {len(failures)} FAILED")
        for item, err in failures[:3]:
            ident = item.get("id") if isinstance(item, dict) else item
            print(f"      {ident}: {err}")
    else:
        print(f"  {label}: {ok} ok")


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Load prod Testray data into local.")
    ap.add_argument("--project", type=int, default=DEFAULT_PROJECT,
                    help=f"prod project id (default {DEFAULT_PROJECT})")
    ap.add_argument("--routine", type=int,
                    help="only mirror builds from this routine (default: any "
                         "routine in the project). All the project's routines "
                         "are mirrored either way, so build FKs resolve.")
    # Promoted-only is the default, so the flag is named for it. --build-limit
    # is kept as an alias: it is what earlier commands and notes used, and in
    # --all-builds mode it is the accurate name anyway.
    ap.add_argument("--promoted-build-limit", "--build-limit", type=int,
                    dest="build_limit", default=DEFAULT_BUILD_LIMIT,
                    metavar="N",
                    help="most recent promoted builds to mirror (default 5; "
                         "0 = no limit). With --all-builds it caps builds "
                         "regardless of promotion.")
    ap.add_argument("--since",
                    help="only builds created on/after this date (YYYY-MM-DD). "
                         "Combine with --build-limit 0 for 'everything since'.")
    ap.add_argument("--all-builds", action="store_true",
                    help="include unpromoted builds. Restricted to the routines "
                         "in ALL_BUILDS_ALLOWED, which have no promoted builds "
                         "by design; everywhere else, promoted only.")
    ap.add_argument("--skip-caseresults", action="store_true",
                    help="structure only — no cases or case results")
    ap.add_argument("--list-routines", action="store_true",
                    help="print the project's routines with their build counts "
                         "and exit, without writing anything")
    ap.add_argument("--config", default=CONFIG)
    args = ap.parse_args()

    # Checked before anything connects or writes.
    if args.all_builds and (args.project, args.routine) not in ALL_BUILDS_ALLOWED:
        allowed = "\n".join(f"    --project {p} --routine {r}"
                            for p, r in sorted(ALL_BUILDS_ALLOWED))
        sys.exit(
            f"--all-builds is not allowed for project {args.project}"
            f"{f' / routine {args.routine}' if args.routine else ' (no --routine given)'}.\n"
            f"Unpromoted builds are transient CI runs; only promoted builds are\n"
            f"mirrored, so the local instance holds meaningful reference points.\n\n"
            f"Allowed with --all-builds:\n{allowed}\n\n"
            f"Drop --all-builds to mirror promoted builds only. If the routine has\n"
            f"none, --list-routines will show that, and it is a signal to pick a\n"
            f"different routine rather than to widen the filter."
        )

    local_cfg, prod_cfg = load_config(args.config)
    prod = Client(prod_cfg, "prod")
    P = args.project
    started = time.time()

    # 0. Survey mode — reads prod only, so it works before the local instance
    # exists. Connect to local afterwards, once we know we need it.
    if args.list_routines:
        rts = prod.paginate("/o/c/routines",
                            filter=f"r_routineToProjects_c_projectId eq '{P}'")
        print(f"{len(rts)} routine(s) in project {P}, counting builds ...\n")

        # Two prod round-trips per routine; serial that is a minute-plus on a
        # 40-routine project, so fan them out.
        counts = {}
        clock = threading.Lock()

        def survey(r):
            base_f = f"r_routineToBuilds_c_routineId eq '{r['id']}'"
            total = prod.get("/o/c/builds", filter=base_f, pageSize=1)["totalCount"]
            promo = prod.get("/o/c/builds", filter=f"{base_f} and promoted eq true",
                             pageSize=1)["totalCount"]
            with clock:
                counts[r["id"]] = (total, promo)

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(survey, rts))

        for r in sorted(rts, key=lambda x: -counts.get(x["id"], (0, 0))[1]):
            total, promo = counts.get(r["id"], (0, 0))
            if promo:
                flag = ""
            elif (P, r["id"]) in ALL_BUILDS_ALLOWED:
                flag = "   <- use --all-builds"
            else:
                flag = "   <- nothing to mirror (no promoted builds)"
            print(f"  {r['id']:<12} {(r.get('name') or '')[:40]:<40} "
                  f"{total:>6} builds, {promo:>3} promoted{flag}")
        print("\nUse --routine <id>; add --all-builds where promoted is 0.")
        return

    local = Client(local_cfg, "local")
    print(f"PROD  {prod.base}\nLOCAL {local.base}\nproject {P}\n")

    # 1. Project ---------------------------------------------------------
    p = prod.get(f"/o/c/projects/{P}")
    project_id = local.upsert("projects", f"prod-project-{P}",
                              {"name": p.get("name") or f"project-{P}"})["id"]
    print(f"project {P} -> local {project_id}  ({p.get('name')})")

    # 2. Teams -----------------------------------------------------------
    teams = prod.paginate("/o/c/teams", filter=f"r_projectToTeams_c_projectId eq '{P}'")
    team_map = {}
    tlock = threading.Lock()

    def write_team(t):
        lid = local.upsert("teams", f"prod-team-{t['id']}", {
            "name": t.get("name") or f"team-{t['id']}",
            "r_projectToTeams_c_projectId": project_id,
        })["id"]
        with tlock:
            team_map[t["id"]] = lid

    report("teams", *run_parallel(write_team, teams, "teams"))

    # 3. Components ------------------------------------------------------
    components = prod.paginate("/o/c/components",
                               filter=f"r_projectToComponents_c_projectId eq '{P}'")
    comp_map = {}
    clock = threading.Lock()

    def write_component(c):
        body = {"name": c.get("name") or f"component-{c['id']}",
                "r_projectToComponents_c_projectId": project_id}
        t = c.get("r_teamToComponents_c_teamId")
        if t and int(t) in team_map:
            body["r_teamToComponents_c_teamId"] = team_map[int(t)]
        lid = local.upsert("components", f"prod-component-{c['id']}", body)["id"]
        with clock:
            comp_map[c["id"]] = lid

    report("components", *run_parallel(write_component, components, "components"))

    # 4. Case types (global — no project FK) ------------------------------
    casetypes = prod.paginate("/o/c/casetypes")
    ct_map = {}
    ctlock = threading.Lock()

    def write_casetype(ct):
        lid = local.upsert("casetypes", f"prod-casetype-{ct['id']}",
                           {"name": ct.get("name") or f"casetype-{ct['id']}"})["id"]
        with ctlock:
            ct_map[ct["id"]] = lid

    report("case types", *run_parallel(write_casetype, casetypes, "casetypes"))

    # 4b. Product versions — the builds-metrics query inner-joins these, so a
    # build with no product version is invisible in the UI build list.
    pversions = prod.paginate("/o/c/productversions",
                              filter=f"r_projectToProductVersions_c_projectId eq '{P}'")
    pv_map = {}
    pvlock = threading.Lock()

    def write_pv(pv):
        lid = local.upsert("productversions", f"prod-productversion-{pv['id']}", {
            "name": pv.get("name") or f"productversion-{pv['id']}",
            "r_projectToProductVersions_c_projectId": project_id,
        })["id"]
        with pvlock:
            pv_map[pv["id"]] = lid

    report("product versions", *run_parallel(write_pv, pversions, "productversions"))

    # 5. Routines ---------------------------------------------------------
    routines = prod.paginate("/o/c/routines",
                             filter=f"r_routineToProjects_c_projectId eq '{P}'")
    routine_map = {}
    for r in routines:
        body = {"name": r.get("name") or f"routine-{r['id']}",
                "autoanalyze": bool(r.get("autoanalyze")),
                "r_routineToProjects_c_projectId": project_id}
        t = r.get("r_teamToRoutines_c_teamId")
        if t and int(t) in team_map:
            body["r_teamToRoutines_c_teamId"] = team_map[int(t)]
        routine_map[r["id"]] = local.upsert("routines", f"prod-routine-{r['id']}", body)["id"]
        print(f"  routine {r['id']} -> local {routine_map[r['id']]}  ({r.get('name')})")

    # 6. Promoted builds --------------------------------------------------
    build_filter = f"r_projectToBuilds_c_projectId eq '{P}'"
    if args.routine:
        build_filter += f" and r_routineToBuilds_c_routineId eq '{args.routine}'"
    if not args.all_builds:
        build_filter += " and promoted eq true"      # note: unquoted boolean
    if args.since:
        # Dates are unquoted in Liferay's filter syntax, unlike ids.
        build_filter += f" and dateCreated ge {args.since}T00:00:00Z"
    if args.build_limit and args.build_limit > 0:
        builds = prod.get("/o/c/builds", filter=build_filter,
                          pageSize=args.build_limit,
                          sort="dateCreated:desc").get("items", [])
    else:
        builds = prod.paginate("/o/c/builds", filter=build_filter)
    print(f"  {len(builds)} build(s) matched")
    if not builds:
        print(f"  filter was: {build_filter}")
        print("  (a routine with no promoted builds needs --all-builds)")
    elif args.since and args.build_limit and len(builds) == args.build_limit:
        # "--since <date>" reads as "everything since", but the limit still
        # applies and silently truncates. Say so rather than quietly mirroring
        # the newest N.
        print(f"  NOTE: capped at --promoted-build-limit {args.build_limit}. "
              f"There may be more since {args.since};")
        print(f"        add --promoted-build-limit 0 for every match.")
    build_map = {}
    for b in builds:
        body = {
            "name": b.get("name") or f"build-{b['id']}",
            # Copy prod's value. Hardcoding True here marked every mirrored
            # build promoted, including --all-builds CI runs that prod never
            # promoted — which quietly falsified the one field the default
            # filter selects on.
            "promoted": bool(b.get("promoted")),
            "archived": bool(b.get("archived")),
            "template": bool(b.get("template")),
            "description": b.get("description"),
            "r_projectToBuilds_c_projectId": project_id,
        }
        if b.get("gitHash"):
            body["gitHash"] = b["gitHash"]
        if b.get("dueDate"):
            body["dueDate"] = b["dueDate"]
        for field in ("dueStatus", "importStatus"):
            if key_of(b.get(field)):
                body[field] = {"key": key_of(b[field])}
        rt = b.get("r_routineToBuilds_c_routineId")
        if rt and int(rt) in routine_map:
            body["r_routineToBuilds_c_routineId"] = routine_map[int(rt)]
        pv = b.get("r_productVersionToBuilds_c_productVersionId")
        if pv and int(pv) in pv_map:
            body["r_productVersionToBuilds_c_productVersionId"] = pv_map[int(pv)]
        build_map[b["id"]] = local.upsert("builds", f"prod-build-{b['id']}", body)["id"]
        print(f"  build {b['id']} -> local {build_map[b['id']]}  "
              f"({(b.get('name') or '')[:56]})")

    # 6a. Build summaries — per build, per team. The UI's build list joins
    # these for its status columns, so without them the page renders empty.
    # Note caseResultTotal only exists if the site-initializer's BuildSummary
    # definition includes it; a stock definition omits the field and the
    # write silently drops it.
    summaries = []
    for pb, lb in build_map.items():
        rows = prod.paginate("/o/c/buildsummaries",
                             filter=f"r_buildToBuildSummary_c_buildId eq '{pb}'")
        summaries.extend((s, lb) for s in rows)
        print(f"  build {pb}: {len(rows)} build summaries")

    COUNTERS = ("caseResultBlocked", "caseResultDidNotRun", "caseResultFailed",
                "caseResultInProgress", "caseResultIncomplete", "caseResultPassed",
                "caseResultTestFix", "caseResultUntested", "caseResultTotal")

    def write_summary(pair):
        s, lb = pair
        body = {"r_buildToBuildSummary_c_buildId": lb}
        for f in COUNTERS:
            if s.get(f) is not None:
                body[f] = s[f]
        t = s.get("r_teamToBuildSummary_c_teamId")
        if t and int(t) in team_map:
            body["r_teamToBuildSummary_c_teamId"] = team_map[int(t)]
        local.upsert("buildsummaries", f"prod-buildsummary-{s['id']}", body)

    report("build summaries", *run_parallel(write_summary, summaries, "summaries"))

    # 6b. Runs — Testray navigates build -> run -> case result, so without
    # these the UI shows a build with no results even though the rows exist.
    # One run per environment combination (Tomcat/Chrome/DB/JDK/...).
    run_map = {}
    rlock = threading.Lock()
    all_runs = []
    for pb, lb in build_map.items():
        rows = prod.paginate("/o/c/runs", filter=f"r_buildToRuns_c_buildId eq '{pb}'")
        all_runs.extend((r, lb) for r in rows)
        print(f"  build {pb}: {len(rows)} runs")

    def write_run(pair):
        r, lb = pair
        body = {
            "name": r.get("name") or f"run-{r['id']}",
            "number": r.get("number") or 0,
            "environmentHash": r.get("environmentHash"),
            "externalReferenceType": r.get("externalReferenceType") or 0,
            "jenkinsJobKey": r.get("jenkinsJobKey") or 0,
            "r_buildToRuns_c_buildId": lb,
        }
        lid = local.upsert("runs", f"prod-run-{r['id']}", body)["id"]
        with rlock:
            run_map[r["id"]] = lid

    report("runs", *run_parallel(write_run, all_runs, "runs"))

    if args.skip_caseresults:
        print(f"\n--skip-caseresults set. Done in {time.time() - started:.0f}s.")
        return

    # 7. Case results (fetched first, so we know which cases we need) -----
    cr_by_build = {}
    case_ids = set()
    for pb in build_map:
        rows = prod.paginate("/o/c/caseresults",
                             filter=f"r_buildToCaseResult_c_buildId eq '{pb}'",
                             fields="id,dueStatus,errors,comment,duration,warnings,"
                                    "startDate,closedDate,"
                                    "r_caseToCaseResult_c_caseId,"
                                    "r_componentToCaseResult_c_componentId,"
                                    "r_teamToCaseResult_c_teamId,"
                                    "r_runToCaseResult_c_runId")
        cr_by_build[pb] = rows
        case_ids.update(int(r["r_caseToCaseResult_c_caseId"])
                        for r in rows if r.get("r_caseToCaseResult_c_caseId"))
        print(f"  build {pb}: {len(rows)} caseresults fetched")

    # 8. Cases — read in batches; one GET each would be thousands of trips
    ids = sorted(case_ids)
    print(f"\nreading {len(ids)} cases from prod in batches of {BATCH} ...")
    prod_cases = {}
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        flt = "id in (" + ",".join(f"'{x}'" for x in chunk) + ")"
        for c in prod.get("/o/c/cases", filter=flt, pageSize=BATCH,
                          fields="id,name,priority,r_caseTypeToCases_c_caseTypeId,"
                                 "r_componentToCases_c_componentId").get("items", []):
            prod_cases[int(c["id"])] = c

    case_map = {}
    calock = threading.Lock()

    def write_case(cid):
        c = prod_cases.get(cid, {})
        body = {"name": c.get("name") or f"case-{cid}",
                "r_projectToCases_c_projectId": project_id}
        ct = c.get("r_caseTypeToCases_c_caseTypeId")
        if ct and int(ct) in ct_map:
            body["r_caseTypeToCases_c_caseTypeId"] = ct_map[int(ct)]
        cp = c.get("r_componentToCases_c_componentId")
        if cp and int(cp) in comp_map:
            body["r_componentToCases_c_componentId"] = comp_map[int(cp)]
        lid = local.upsert("cases", f"prod-case-{cid}", body)["id"]
        with calock:
            case_map[cid] = lid

    report("cases", *run_parallel(write_case, ids, "cases"))

    # 9. Case results -----------------------------------------------------
    for pb, rows in cr_by_build.items():
        lb = build_map[pb]

        def write_cr(it, lb=lb):
            body = {"errors": it.get("errors"),
                    "r_buildToCaseResult_c_buildId": lb}
            if key_of(it.get("dueStatus")):
                body["dueStatus"] = {"key": key_of(it["dueStatus"])}
            for field in ("comment", "duration", "warnings", "startDate", "closedDate"):
                if it.get(field) not in (None, ""):
                    body[field] = it[field]
            for src, mapping in (
                ("r_caseToCaseResult_c_caseId", case_map),
                ("r_componentToCaseResult_c_componentId", comp_map),
                ("r_teamToCaseResult_c_teamId", team_map),
                ("r_runToCaseResult_c_runId", run_map),
            ):
                v = it.get(src)
                if v and int(v) in mapping:
                    body[src] = mapping[int(v)]
            local.upsert("caseresults", f"prod-caseresult-{it['id']}", body)

        report(f"caseresults build {pb}",
               *run_parallel(write_cr, rows, f"cr {pb}"))

    # 10. Verify ----------------------------------------------------------
    print("\nverifying against prod counts:")
    ok = True
    for pb, lb in build_map.items():
        got = local.get("/o/c/caseresults",
                        filter=f"r_buildToCaseResult_c_buildId eq '{lb}'",
                        pageSize=1)["totalCount"]
        want = len(cr_by_build[pb])
        ok &= got == want
        print(f"  build {pb} -> local {lb}: {got}/{want} "
              f"{'OK' if got == want else 'SHORT'}")

    print(f"\n{'DONE' if ok else 'DONE WITH GAPS'} in {time.time() - started:.0f}s. "
          f"Local project {project_id}, routines {list(routine_map.values())}, "
          f"builds {list(build_map.values())}")


if __name__ == "__main__":
    main()
