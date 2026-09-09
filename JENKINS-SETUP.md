# Running triage on Jenkins

Every 30 minutes: find Stable builds whose failures nobody has accounted for,
work out which commits caused them, write the verdicts back to Testray, and
post a summary to `#portal-failures`.

One script does the whole job — `scripts/triage_jenkins.sh`. Everything below is
either a Jenkins field or a one-off on the agent.

---

## What the job actually runs

```
Build Steps > Execute shell > Command:

export TRIAGE_REPO_PATH=/var/lib/liferay-portal
export TRIAGE_ROUTINE_REMOTES="79529=origin"
export TRIAGE_SCAN_ROUTINES=79529
export TRIAGE_QUEUE=/var/lib/triage/queue

./scripts/triage_jenkins.sh
```

The script sets the rest itself: the Testray URLs, `--engine api`, the venv
(`python3.13`, editable install), the log directory, and a lock. It prints
everything it resolved as **step 0** before doing any work, so the console names
this run's logs, bundles and Slack message up front.

Two commands run inside it, and the order matters:

| Step | Command | Cost |
|---|---|---|
| 1 | `testray-analysis scan --once` | free — REST reads and a git diff |
| 2 | `testray-analysis watch --once --classify --engine api` | **money** — one Anthropic call per batch |

`scan` is the producer: it queues a build when the build is `importStatus` DONE,
has at least one FAILED case result, and carries a failure signature Testray has
no verdict for. `watch` is the consumer, and it drains work from *either*
producer — the scanner, or someone clicking **Run Triage** in Testray.

**Running only `watch` does nothing.** With no producer the queue is always
empty and every tick prints `Nothing queued.`

---

## Jenkins configuration

| Field | Value |
|---|---|
| Git > Repository URL | the repo this file is in |
| Git > Branch Specifier | `master` |
| Build Triggers | Build periodically, `H/30 * * * *` |
| Execute shell | the block above |
| Concurrent builds | **disabled** |
| Slack Notifications | notify on **every build** |
| Slack message | `${FILE,path="slack/testray_analyzer_slack_message.txt"}` |

### Secret text bindings

| Variable | What it is |
|---|---|
| `TESTRAY_CLIENT_ID` | prod Testray OAuth2 application (client credentials) |
| `TESTRAY_CLIENT_SECRET` | its secret |
| `ANTHROPIC_API_KEY` | Anthropic API key — **note the spelling**, `ANTHROPIC`, not `ANTROPIC` |

The pipeline normally *refuses* credentials that arrive by environment, because
a stale `TESTRAY_CLIENT_ID` in a shell silently redirects half a run to the
wrong instance. `triage_jenkins.sh` sets `TRIAGE_ALLOW_ENV_CREDENTIALS=1`,
which is the "yes, these are deliberate" switch.

### Why "notify on every build" and not "every failure"

A successful triage of a red build is a **job success**. With failure-only
notification the channel would hear from this job only when the tooling itself
broke, and never see an analysis. The message is written on every run,
including one that concluded nothing — silence and success are
indistinguishable otherwise.

A tick that finds the previous tick still running exits **0** and logs
`skipped: another tick still holds …`. That is deliberate: under a 30-minute
trigger with a classify run that can take longer, overlap is normal and should
not read as a failure.

---

## One-off setup on the agent

### 1. A persistent `liferay-portal` checkout

`prepare` runs `git diff` and `git log` between the two builds' commits, so the
agent needs a real checkout — about 35 GB. It must be **outside the workspace**
and it must be the **control repo**, because Stable's commits are merged there
first and do not reach `liferay/liferay-portal` until later:

```bash
git clone https://github.com/brianchandotcom/liferay-portal /var/lib/liferay-portal
```

`TRIAGE_ROUTINE_REMOTES="79529=origin"` names the **remote inside that
checkout**, not a URL. Get this wrong and nothing errors: `github_slug()` falls
back to `liferay/liferay-portal`, and every commit link in the report and in
Slack points at a repo that does not have the commit. The script asserts the
remote resolves to something containing `brianchandotcom` and refuses to run
otherwise.

Keep it fetched — a scheduled `git -C /var/lib/liferay-portal fetch origin` is
enough. A build whose commit is missing locally can only be analysed after a
fetch.

### 2. A queue directory that outlives the workspace

```bash
mkdir -p /var/lib/triage/queue
```

Only used while the analytics client extension is **not** deployed to prod (see
below). In that mode this directory holds the only record that a build pair was
already analysed — Testray has no verdict store to derive it from — so a wiped
workspace would make every tick re-queue and re-pay for the same analysis, 48
times a day. Once the CX is deployed, the queue moves into Testray and this
directory goes unused.

### 3. Python 3.13

The script builds its own venv on first run (`python3.13 -m venv .venv`,
editable install). Override the interpreter with `TRIAGE_PYTHON_BIN`.

### 4. Testray permissions

The prod OAuth2 application needs, on top of the read scopes it already has:

```
c_triageresult.everything
c_triagerun.everything
c_triageroutinesetting.everything
```

Use the plain `.everything` form. The `.read` / `.write` variants are accepted
in the application's configuration and then **silently dropped**, so you find
out at call time with a 403 rather than at save time.

The user that application acts as also needs the **Testray Administrator**
role: `ADD_OBJECT_ENTRY` on those three Objects is granted to that role alone.

---

## Check it before it spends anything

```bash
./scripts/triage_jenkins.sh --check
```

Read-only, no model usage. It verifies the secrets, the portal checkout and its
remote, then mints a token and reads the three triage endpoints. What you are
reading is the last block:

```
Ready: rows queue in Testray and verdicts write back.
```
Everything is in place.

```
warn  /o/c/triageruns → 404, the analytics client extension is not deployed …
Usable, in degraded mode: the queue will use marker files and verdicts stay
in the local report.
```
The job will run and produce reports, but there will be no build-list diamond
and no verdicts stored in Testray. This is expected until the CX is deployed.

```
FAIL  /o/c/triageruns → HTTP 403: the Object is there, but this app has no
      c_triagerun scope.
```
The one worth checking for. **A 403 and a 404 are indistinguishable to the
queue**, which treats any error as "not deployed" — so a missing scope silently
degrades the run to marker files and refuses every write, while looking exactly
like an undeployed extension. Fix the scopes and the role, per §4 above.

Once the CX is deployed, add `--require-objects` to the build step so a broken
deploy fails the job instead of quietly degrading.

### First real run

Run it once with classification off — it exercises the whole path for free:

```bash
./scripts/triage_jenkins.sh --no-classify
```

Then check which queue engaged, in `watch`'s own banner:

```
queue:    QUEUED TriageRun rows (the build-list diamond shows their state)
queue:    /var/lib/triage/queue — marker files, because the TriageRun Object
          is not deployed here
```

---

## Where the output goes

| What | Where |
|---|---|
| Per-step logs | `logs/log_<timestamp>_step_<n>_<name>.txt` in the workspace |
| Run bundles | `runs/r_<timestamp>_<baseline>_<target>/` — prompt, diff, hunks, verdicts |
| The report | `report.html` inside the bundle |
| Slack message | `slack/testray_analyzer_slack_message.txt`, rewritten every run |
| Verdicts | `TriageResult` rows in Testray, once the CX is deployed |

Archiving `logs/*.txt` and `runs/**` as build artifacts is worth doing: they are
the whole record of what a run concluded and why.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ran, or skipped because another tick holds the lock |
| 1 | usage, or preflight — a missing secret, a bad remote, no portal checkout, a scope gap |
| 2 | a pipeline step failed. The failing step's own log is named in the console |

---

## When something looks wrong

**`Nothing queued.` on every tick.** The scanner is not running, or it found
nothing unexplained. Both are worth telling apart: run
`.venv/bin/testray-analysis scan --once --dry-run`, which reports what it
*would* queue and writes nothing.

**`! poll failed: HTTP Error 404: Not Found`.** An older `watch` looking for
`TriageRun` rows on an instance without the CX. Current versions probe first
and fall back to marker files, saying so in the banner.

**A build is red and the run explained nothing.** Expected on a routine whose
failures are all infrastructure, and the Slack message says so outright
(`no verdicts were produced … Needs a human.`). A chronic failure — red in
every build in the window — is skipped for want of a baseline, which is what
stops a permanently-broken test from being re-analysed forever.

**Every commit link points at `liferay/liferay-portal`.** The remote assertion
was bypassed, or `TRIAGE_ROUTINE_REMOTES` names a remote that does not exist in
the checkout. Confirm with
`git -C "$TRIAGE_REPO_PATH" remote get-url origin`.
