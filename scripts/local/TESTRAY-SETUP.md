# Local Testray from scratch

Two scripts in this directory bring up a local Testray with real data:

| Script | Does |
|---|---|
| `setupTestray.sh` | starts the containers, deploys the REST API + client extensions |
| `loadTestrayData.py` | copies a project and its promoted builds from prod Testray into local |

Neither is version-controlled — they are personal setup tooling.

**Total time from nothing to loaded instance: ~25 minutes**, of which ~5 is
you clicking through the OAuth screen and the rest is waiting.

---

## Before you start

- Docker running.
- `liferay-portal` on branch **`master-testray`** from `liferay-release`
  (that branch has the
  analytics client extension; the script warns if you are elsewhere).
- `~/dev/projects/testray2` present (holds `liferay/docker-compose.yaml`).
- JDKs at `/usr/lib/jvm/zulu11` and `/usr/lib/jvm/zulu17`.

**Check the portal tree is clean first.** The script patches three Testray
source files while it builds and restores them afterwards — but it refuses to
touch a file you have already modified, and warns instead. If a previous
session left edits behind, the patches will not apply and things will break in
confusing ways:

```bash
cd ~/dev/projects/liferay-portal
git status --short -- workspaces/
```

If `TestrayStatusMetricResourceImpl.java` (or either BuildSummary JSON) shows as
modified, discard it — the script re-applies whatever is still needed:

```bash
git checkout -- workspaces/liferay-testray-workspace/modules/testray-rest-impl/src/main/java/com/liferay/testray/rest/internal/resource/v1_0/TestrayStatusMetricResourceImpl.java
```

---

## 1. Bring up the stack

```bash
cd ~/dev/projects/setupTestray
./setupTestray.sh --fresh
```

`--fresh` destroys the database first. **That wipes the OAuth application and
every row you previously loaded**, so only use it when you want to start over.
Without it, the script starts the existing containers and redeploys on top.

It runs: `docker compose up` → wait for the portal → deploy `rest-api`,
`rest-impl`, `cron`, `jira`, `custom-element`, `site-initializer`, `analytics`.
Output streams to your terminal and to `./logs/`.

Other flags: `--skip-startup` (deploys only), `--only <name>` (one component,
`--list` to see names), `--quiet` (log to file only).

### Confirm it worked

The script ends with a schema check. Make sure it does **not** warn about the
`_x` extension table, then:

```bash
for p in /o/c/routines /o/c/triageresults /o/c/buildsummaries /o/c/productversions; do
  echo "$p $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080$p)"
done
```

All four should answer. A **404** means that object does not exist — the client
extension that defines it did not install (see Troubleshooting).

---

## 2. Create the OAuth application

The one manual step. It cannot be scripted, because the credentials do not
exist until you make them.

1. <http://localhost:8080> → Control Panel → **Security → OAuth2 Administration**
2. New application, **Client Credentials** grant.
3. Grant these twelve scopes — **all of them**, or the load fails partway with a
   403:

   ```
   c_project    c_team     c_component      c_casetype   c_productversion
   c_routine    c_build    c_buildsummary   c_run        c_case
   c_caseresult c_triageresult
   ```

   Use the plain `c_<object>.everything` form. The `.read` and `.write` variants
   are accepted in configuration and then **silently dropped** — you find out at
   call time with a 403, not at deploy time.

4. Copy the client ID and secret into
   `~/dev/projects/testray-analytics/config/config.yml`:

   ```yaml
   testray:
     base_url: "http://localhost:8080"
     client_id: <local app id>
     client_secret: <local app secret>
     prod_url: https://testray.liferay.com
     prod_client_id: <prod app id>          # read-only is enough
     prod_client_secret: <prod app secret>
   ```

   The prod credentials are only for reading source data. The prod app needs
   read scopes on project, team, component, casetype, productversion, routine,
   build, buildsummary, run, case and caseresult.

### Verify the grants landed

The scope list in a UI is not proof. Ask for a token and read what it actually
carries:

```bash
cd ~/dev/projects/testray-analytics && source .venv/bin/activate
python -c "
import json,urllib.parse,urllib.request,yaml
c=yaml.safe_load(open('config/config.yml'))['testray']
d=urllib.parse.urlencode({'grant_type':'client_credentials','client_id':c['client_id'],
  'client_secret':c['client_secret']}).encode()
print(sorted(json.loads(urllib.request.urlopen(c['base_url']+'/o/oauth2/token',d).read())['scope'].split()))"
```

---

## 3. Load the data

```bash
cd ~/dev/projects/setupTestray
../testray-analytics/.venv/bin/python loadTestrayData.py
```

Defaults to project **473116959** ("Liferay Portal 2026 Q2") and its **5 most
recent promoted builds** — about 32,000 case results, roughly 10 minutes.

Loads in dependency order: project → teams → components → case types → product
versions → routines → builds → build summaries → runs → cases → case results.
Everything is keyed by `prod-<type>-<id>` external reference codes, so **it is
safe to re-run** — rows upsert, they do not duplicate. Local ids will not match
prod's; the ERC is the join.

Options: `--build-limit N`, `--skip-caseresults` (structure only, ~1 minute),
`--project <id>`.

It finishes by comparing local counts against what it fetched and prints `DONE`
or `DONE WITH GAPS`. Anything other than `DONE` means rows are missing.

---

## 4. Rebuild a client extension

```bash
cd ~/dev/projects/setupTestray
TESTRAY_EXPECT_BRANCH=master-testray ./deployCx.sh liferay-testray-analytics-custom-element
```

Builds the CX from the liferay-portal workspace, repackages the zip, drops it in
`bundles/deploy/`, and polls until the container is **serving** the new
`index.js`. Run it with no arguments to list the available extensions.

Three things it exists to handle, each of which fails quietly:

- **The zip names the stylesheet by content hash** (`cssURLs=index.<hash>.css`).
  Any style change re-hashes the file, so copying `static/` alone leaves Liferay
  requesting a stylesheet that is gone and the view renders unstyled. The script
  regenerates the config from the deployed zip.
- **`osgi/client-extensions/<name>.zip` keeps its old mtime** after a redeploy,
  so it proves nothing. The only reliable check is diffing the served asset
  against the build, which is what the script polls for.
- **Branch.** `liferay-testray-custom-element` exists on every branch, so
  building it from one without the triage work deploys a bundle with the hook
  missing — build succeeds, deploy succeeds, and the Triage column just goes
  blank. Pass `TESTRAY_EXPECT_BRANCH` and it refuses instead.

If the build fails with `tsc: not found`, `node_modules` is gone (a `git clean
-fdx` will do it). It is a **yarn workspace**, so reinstall at the workspace
root, not in the extension:

```bash
cd ~/dev/projects/liferay-portal/workspaces/liferay-testray-workspace
yarn install --frozen-lockfile
```

Do not reach for `npx tsc` to typecheck — there is no local `tsc`, so npx
installs an unrelated package, prints "This is not the tsc command you are
looking for", and exits **0** without checking anything. `npm run build` is the
only trustworthy signal.

---

## Troubleshooting

Every one of these has actually happened.

**An object endpoint 404s** (e.g. `/o/c/triageresults`) — the client extension
never installed. Liferay watches the artifact's modification time, and gradle
*preserves the source timestamp* when copying, so an unchanged build drops in a
file that still looks old and is ignored. Silent: a site initializer simply
never runs. The script now touches every artifact after deploy. To fix by hand:

```bash
touch ~/dev/projects/testray2/liferay/bundles/osgi/client-extensions/<name>.zip
```

**`/o/c/triageresults` 404s but the Testray objects are fine** — the analytics
extension initialized before the `TESTRAY` object folder existed, and aborted:

```bash
docker logs testray-liferay 2>&1 | grep "No ObjectFolder exists"
```

Our TriageResult definition lives in that folder, which the *Testray* site
initializer creates and takes ~15s to do. The script now waits for the folder
before deploying analytics. To recover without a rebuild, just re-trigger it:

```bash
touch ~/dev/projects/testray2/liferay/bundles/osgi/client-extensions/liferay-testray-analytics-site-initializer.zip
```

**403 on one object type, others fine** — a missing scope. Print the token's
scope claim (above); do not trust the UI.

**A UI page is blank, console shows `Cannot destructure property 'items' of
'undefined'`** — an API call returned 500 and the frontend does not handle it.
Find the real error:

```bash
docker logs --since 10m testray-liferay 2>&1 | grep -iE "PSQLException|does not exist" | tail -5
```

**`column bx.cpuusetime_ does not exist`** — the routines page. Testray's SQL
reads Build fields from the extension table; locally they are on the base table.
The script patches this during `rest-impl` deploy. If you see it, the patch was
skipped because the file was already modified — discard your edits and redeploy:
`./setupTestray.sh --only rest-impl --skip-startup`.

**`column bs.caseresulttotal_ does not exist`, or the build list is empty** —
the BuildSummary object is missing pieces. Both required columns must sit on the
**base** table, which only happens if they were declared when the site was first
initialized:

```bash
docker exec testray-postgres psql -U root -d lportal -t -c \
  "select table_name||'.'||column_name from information_schema.columns
   where column_name in ('caseresulttotal_','r_buildtobuildsummary_c_buildid');"
```

Both must name `..._buildsummary`, not `..._buildsummary_x`. If either is in
`_x`, it was added after the fact and the SQL cannot see it — you need a
`--fresh` run so the site is initialized with the script's patches in place.

**Build list renders but shows nothing** — the summaries may have their build FK
empty. Writing to a relationship that does not exist yet does not error, it
silently drops the value. Check, and re-run the loader if it is zero:

```bash
# find whichever table holds the column (the prefix is your company id)
T=$(docker exec testray-postgres psql -U root -d lportal -t -c \
  "select table_name from information_schema.columns
   where column_name='r_buildtobuildsummary_c_buildid';" | tr -d ' \n')

docker exec testray-postgres psql -U root -d lportal -t -c \
  "select count(*) as rows, count(nullif(r_buildtobuildsummary_c_buildid,0)) as linked
   from $T;"
```

`linked` must equal `rows`. If it is 0, re-run `loadTestrayData.py` — the
summaries were written before the relationship existed.

**`prepare` fails with 401 while everything else works** — `prepare()` honours
`TESTRAY_BASE_URL` / `TESTRAY_CLIENT_ID` / `TESTRAY_CLIENT_SECRET` environment
variables *over* `config.yml`, and `submit` does not. Stale exports in your shell
will authenticate the read half against the wrong instance:

```bash
env | grep TESTRAY_          # should be empty
```

---

## What the scripts patch, and why

Three Testray source files are edited during the build and restored immediately
after, so `liferay-portal` is never left dirty and nothing can be committed by
accident. All three work around genuine defects:

| File | Fix |
|---|---|
| `TestrayStatusMetricResourceImpl.java` | `bx.cpuUseTime_` / `bx.importStatus_` → `b.` — the SQL hardcodes which physical table an Objects field lives in, which differs between prod and a freshly initialized instance |
| `testray-build-summary.json` | adds `caseResultTotal`, which the SQL selects but the definition never declared |
| `testray-build-to-buildSummary.json` | `[$OBJECT_DEFINITION_ID:Build]` → `...Build$]` — a missing `$` means the token never resolves and the Build↔BuildSummary relationship is silently never created |

**Do not commit any of them, and do not keep them in your working tree.** The
script re-applies all three on every run and restores the files afterwards, so
a clean `liferay-portal` is the state it wants — wipe and update that repo as
often as you like. There is nothing to preserve, no branch to maintain, and
nothing for a colleague to fetch: hand them these two scripts and this file.

The first patch is environment-specific. The last two are real upstream bugs
worth reporting against `testray-rest-impl` and `liferay-testray-site-initializer`
— every fresh Testray hits them — but they belong in their own ticket, fixed
properly, not carried on a feature branch.

Because the patches are text matches, the script asserts the *result* after
applying them: the `bx.` references must be gone, `caseResultTotal` must exist,
and the placeholder must read `Build$]`. If upstream fixes a bug themselves the
patch becomes a no-op and the assertion still passes. If upstream moves the
code, you get a warning naming the consequence rather than a silent broken
deploy. Take those warnings seriously — they mean a page will 500.

Two runbook steps are skipped deliberately: the licence key (step 2 — the
container image already has one) and the `16b8cd7` revert (step 5). That commit
is only a dependency-version bump in `testray-rest-impl/build.gradle`; the newer
versions build and run fine here, so the revert is unnecessary.
