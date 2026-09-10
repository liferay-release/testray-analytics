# Quickstart — a local Testray to develop and support against

> **Do you need this one?** Only if you need the Testray **screens** — the
> Triage column in the build list, the coloured diamond, the report page inside
> Testray — or you are supporting the release-master job and want somewhere safe
> to reproduce a problem.
>
> If you only want to **analyse a failing build and read the report**, you do
> not need any of this. Use [QUICKSTART.md](QUICKSTART.md) instead — 15 minutes,
> no Docker.

This gets you from nothing to a working local Testray with real data, the triage
screens deployed, and one analysis you ran yourself.

**About 45 minutes**, most of it waiting. You need Docker, about 40 GB of disk,
and a Liferay portal checkout.

New to the words used here — routine, cluster, verdict, baseline? Read
[docs/GLOSSARY.md](docs/GLOSSARY.md) first. It is short.

---

## Step 0 — collect the pieces

| What | Where from |
|---|---|
| This repo | `github.com/liferay/liferay-testray-analytics` |
| `setupTestray.sh`, `deployCx.sh`, `loadTestrayData.py` | **in this repo**, under `scripts/local/`. `scripts/local/TESTRAY-SETUP.md` is the long-form reference behind this guide |
| `liferay-portal` from **`liferay-release`**, on branch **`master-testray`** | that branch carries the triage client extensions |
| `testray2` | `github.com/dxpcloud/testray2` — provides `liferay/docker-compose.yaml` and the bundle the containers deploy into |
| Prod Testray OAuth credentials | read-only is enough; used to copy data down |

Also needed: Docker running, and JDKs at `/usr/lib/jvm/zulu11` and
`/usr/lib/jvm/zulu17`.

Put `liferay-portal`, `testray2` and this repo **side by side in one
directory**. The scripts derive every path from their own location on that
assumption — nothing is hardcoded to a home directory — and each one can be
pointed elsewhere:

| Variable | Default |
|---|---|
| `TESTRAY_WORKSPACE_DIR` | the directory holding this repo |
| `TESTRAY_PORTAL_DIR` | `<workspace>/liferay-portal` |
| `TESTRAY2_DIR` | `<workspace>/testray2` |
| `TESTRAY_JAVA11` / `TESTRAY_JAVA17` | `/usr/lib/jvm/zulu11`, `/usr/lib/jvm/zulu17` |

**Check the portal checkout is clean before you start:**

```bash
cd ~/dev/projects/liferay-portal
git status --short -- workspaces/
```

It must print nothing. `setupTestray.sh` patches three Testray source files
while it builds and restores them afterwards, but it refuses to touch a file you
have already modified — and then the patches do not apply and things break in
confusing ways.

---

## Step 1 — start Testray (~20 minutes)

```bash
./scripts/local/setupTestray.sh --fresh
```

`--fresh` destroys the database first. That wipes the OAuth application and
every row you loaded before, so use it the first time and then avoid it.

It brings up the containers and deploys the REST API, the cron and jira
extensions, the Testray custom element, the site initializer, and our analytics
site initializer.

**Check it worked.** The script ends with a schema check — it must not warn
about the `_x` table. Then:

```bash
for p in /o/c/routines /o/c/triageresults /o/c/buildsummaries /o/c/productversions; do
  echo "$p $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080$p)"
done
```

All four must answer. A **404** means that object does not exist, so the client
extension that defines it did not install. `TESTRAY-SETUP.md` has the recovery
steps — usually `touch` the artifact so Liferay notices it.

---

## Step 2 — create the OAuth application (~5 minutes, by hand)

This is the one step that cannot be scripted: the credentials do not exist until
you make them.

1. Open <http://localhost:8080> → Control Panel → **Security → OAuth2
   Administration**.
2. New application, **Client Credentials** grant.
3. Grant **all twelve** of these scopes, or the data load fails partway with a
   403:

   ```
   c_project    c_team     c_component      c_casetype   c_productversion
   c_routine    c_build    c_buildsummary   c_run        c_case
   c_caseresult c_triageresult
   ```

   Use the plain `c_<object>.everything` form. `.read` and `.write` are accepted
   when you save and then **silently dropped** — you find out at call time with
   a 403, not at save time.

4. Copy the client id and secret into `config/config.yml` in this repo (copy
   `config/config.yml.example` first). That file is gitignored; secrets never
   get committed.

**Check the scopes actually landed.** The list on the screen is not proof:

```bash
.venv/bin/testray-analysis preflight
```

It prints how many scopes the token really carries and whether each triage
endpoint answers.

---

## Step 3 — install this tool

```bash
cd ~/dev/projects/testray-analytics
python3.13 -m venv .venv
.venv/bin/pip install -e .
```

Editable (`-e`) matters: it keeps `config/` and `slack/` resolving inside this
checkout.

Point `config/config.yml` at your local instance and your portal checkout:

```yaml
testray:
  base_url: "http://localhost:8080"
  ui_url: "http://localhost:8080/web/liferay-testray"
  client_id: "<from step 2>"
  client_secret: "<from step 2>"
git:
  repo_path: /home/you/dev/projects/liferay-portal
  routine_remotes:
    115520: brianchandotcom        # the local Stable twin; the routineId might change after importing data
```

> **One checkout, two jobs — and they are not the same repository.** The
> client extensions are built from `master-testray` on
> `liferay-release/liferay-portal`. The *diff* is computed against whichever
> repository the routine's commits actually landed in — for Stable that is
> `brianchandotcom/liferay-portal`, because the data you loaded in step 4 was
> copied down from prod.
>
> One clone can serve both: add both as remotes and let `routine_remotes` name
> the right one per routine. What must not happen is `routine_remotes` pointing
> at a remote that does not carry the commits — nothing errors, the commit
> links just go to a repository that does not have them.

---

## Step 4 — copy real data down from prod (~10 minutes)

A fresh Testray has no builds, and there is nothing to triage without them.

```bash
.venv/bin/python scripts/local/loadTestrayData.py
```

It copies one project and its five most recent promoted builds — about 32,000
case results. Everything is keyed by `prod-<type>-<id>` reference codes, so it
is **safe to re-run**: rows update rather than duplicate.

It must finish with `DONE`. `DONE WITH GAPS` means rows are missing.

Useful flags: `--build-limit N`, `--skip-caseresults` (structure only, ~1
minute), `--project <id>`.

> **Local ids are not prod ids.** The local twin of Stable is routine
> **115520** in project **114660**, not 79529/35392. Use the local numbers when
> you are working against localhost.

---

## Step 5 — deploy the triage screens

`setupTestray.sh` deploys the analytics **site initializer** (the Objects), but
**not** the triage user interface. That is a separate extension:

```bash
./scripts/local/deployCx.sh liferay-testray-analytics-custom-element
```

`master-testray` is now the branch `setupTestray.sh` and `deployCx.sh` expect by
default, so you only pass `TESTRAY_EXPECT_BRANCH` to override it.

`TESTRAY_EXPECT_BRANCH` is not optional in spirit: `liferay-testray-custom-element`
exists on every branch, and building it from one without the triage work
produces a bundle where the Triage column is simply blank — the build succeeds,
the deploy succeeds, and nothing tells you.

> The branch that matters is `master-testray` on
> `liferay-release/liferay-portal`. Both scripts default to it now.

The script also handles two things that fail silently: the stylesheet is named
by content hash, so any style change leaves Liferay asking for a file that is
gone; and the deployed zip keeps its old timestamp, so it proves nothing. It
polls the served asset until it matches the build.

If the build fails with `tsc: not found`, `node_modules` is gone. It is a yarn
workspace, so reinstall at the workspace root:

```bash
cd ~/dev/projects/liferay-portal/workspaces/liferay-testray-workspace
yarn install --frozen-lockfile
```

---

## Step 6 — run one triage

Pick two builds of the same routine from the Testray build list: an older one
and a newer one that failed. Then, free:

```bash
cd ~/dev/projects/testray-analytics
./scripts/triage_pipeline.sh -b <older build id> -t <newer build id> --no-classify
```

That reads both builds, groups the failures, computes the git diff, and writes a
run bundle under `runs/`. Look inside it — that directory is the evidence.

To get verdicts you have to call the model, and that costs money:

```bash
.venv/bin/testray-analysis classify runs/r_<id> --dry-run   # free: shows the cost
.venv/bin/testray-analysis classify runs/r_<id>             # spends
.venv/bin/testray-analysis submit   runs/r_<id>
```

A run is capped at **$15**; `classify` refuses to start above that and says so.
A local Stable-sized run is about **$0.27**.

Open `report.html` in the bundle, and the Triage page in Testray at
`http://localhost:8080/web/liferay-testray/triage?buildId=<newer build id>`.

The [`run-one-triage`](.claude/skills/run-one-triage/SKILL.md) skill walks
through this in more detail; Claude Code will follow it if you ask it to triage
a build.

---

## Step 7 — try the unattended path

What release-master runs every 30 minutes, against your local instance:

```bash
.venv/bin/testray-analysis scan  --once --routine 115520 --dry-run
.venv/bin/testray-analysis watch --once            # no --classify: free
```

`scan` queues builds whose failures have no verdict yet; `watch` drains the
queue. Running only `watch` does nothing, because nothing queued anything.

---

## When it goes wrong

Most of these have happened to somebody. `scripts/local/TESTRAY-SETUP.md`
carries the full list; these are the ones you are most likely to hit.

| Symptom | Cause |
|---|---|
| An `/o/c/…` endpoint 404s | the client extension did not install. `touch` its zip in `testray2/liferay/bundles/osgi/client-extensions/` |
| `/o/c/triageresults` 404s but Testray objects are fine | the analytics extension initialised before the `TESTRAY` object folder existed. Re-trigger it the same way |
| 403 on one object type only | a missing scope. Print the token's scopes with `preflight`; do not trust the UI |
| A page is blank, console says `Cannot destructure property 'items'` | an API call returned 500. `docker logs --since 10m testray-liferay 2>&1 \| grep -iE "PSQLException\|does not exist"` |
| `column bx.cpuusetime_ does not exist` | the rest-impl patch was skipped because the file was already modified. Discard your edits and redeploy |
| The build list is empty | build summaries lost their build FK. `TESTRAY-SETUP.md` has the SQL check; re-run the loader |
| `prepare` 401s while everything else works | stale `TESTRAY_*` variables in your shell override `config.yml`. `env \| grep TESTRAY_` should be empty |
| The Triage column is blank | the CX was built from a branch without the triage work. Rebuild from `master-testray` |
