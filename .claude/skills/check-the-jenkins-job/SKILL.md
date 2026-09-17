---
name: check-the-jenkins-job
description: Diagnose the triage job on Jenkins — it failed, it posted nothing to Slack, it says "Nothing queued", no diamond appears in Testray, or the commit links point at the wrong repository. Use for any question about why the failure-triggered triage job is not behaving. Each symptom here has one known cause and one fix.
---

# Check the Jenkins triage job

The job runs `./scripts/triage_jenkins.sh` when a Stable build fails, triggered
by a hook on the Jenkins side. Its setup is
documented in [JENKINS-SETUP.md](../../../JENKINS-SETUP.md); this is for when it
is already set up and something looks wrong.

## First, always: read step 0

Every run prints what it resolved before doing any work:

```
step 0: preflight ok
  routines: 79529   engine: api   classify: true
  portal:   /var/lib/liferay-portal  (origin -> …brianchandotcom/liferay-portal.git)
  logs:     …/logs  (one file per pipeline step)
```

Then `watch` prints which queue it chose:

```
queue:    QUEUED TriageRun rows (the build-list diamond shows their state)
queue:    …/queue — marker files, because the TriageRun Object is not deployed here
```

Most questions are answered by those two blocks. Read them before running
anything.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | ran, **or** skipped because another run still holds the lock | nothing, unless skips repeat — the skipped build waits for the next Stable failure, not for half an hour |
| 1 | preflight or usage — a missing secret, a bad git remote, no portal checkout, a missing OAuth scope | read the message; it names the fix |
| 2 | a pipeline step failed | open the step's own log, named in the console |

---

## Symptom: `Nothing queued.` every run

Two different causes. Tell them apart:

```bash
.venv/bin/testray-analysis scan --once --dry-run
```

- It prints pairs it *would* queue → the scanner works and the drainer is
  consuming them. Nothing is wrong.
- It prints `Nothing red. No work to queue.` → no build in the window has
  failures.
- It prints clusters but `2 skipped for want of a baseline` → those failures are
  present in **every** build in the window. A permanently red test has no
  baseline to compare against, so it is deliberately not analysed. Note the
  message names `MAX_BASELINE_WALK` (40) even when `--window` is smaller, so a
  smaller window can report "no baseline within 40 builds" having looked at
  far fewer. Re-check with `--window 40` before believing it.
- It prints `already analysed <routine>-<baseline>-<target>` → the pair has a
  finished TriageRun. This is the normal steady state on a routine whose
  failures are all known: the job runs, finds nothing new, and spends nothing.
  The run reports `Jobs: 0 queued, N already analysed`, and on Stable it also
  writes a `🔁 Still failing` message naming each recurring signature and the
  build whose run explained it — so this state *is* readable from Slack. No
  triage report is produced for such a build, on purpose: re-analysing would
  re-pay a full classify for an identical answer.

If the build step only calls `watch`, that is the cause: `watch` consumes work,
it does not create it. The build step must call `scan` first. `triage_jenkins.sh`
does both.

- It prints `Routine …: build … is PENDING/INPROGRESS — waiting …` or
  `newest build on file is …, already DONE — watching for a newer build to
  appear …`, then `still waiting: newest is … (…) after …s` a few times, then
  `gave up after 3600s` → this is the build that fired the hook, and Testray
  had not finished importing it (or had not even created its row yet — see
  below) before the wait timeout. Not a bug: `--wait-for-import` (a flag on
  `triage_jenkins.sh` itself, which the failure-triggered build step should
  pass — see JENKINS-SETUP.md) polls for exactly this, and a give-up just
  means the build waits for the next Stable failure's `--catch-up`, same as
  before that flag existed. If this repeats every run, Testray's import lag
  is longer than `TRIAGE_IMPORT_WAIT_TIMEOUT` (default 3600s = 1 hour) —
  raise it in the job's environment rather than treating it as broken.
- If the console shows none of these lines at all, check whether the build
  step is actually passing `--wait-for-import` to `triage_jenkins.sh` — it is
  off by default (step 0's `wait_for_import: false/true` says which), on
  purpose, so a manual `--no-classify`/`--check` run does not sit through the
  wait for a build that was never coming.
- The "already DONE — watching for a newer build" message on its own, every
  single run, is normal and expected — it is not evidence of a stuck build.
  The hook fires the instant Stable fails, which is routinely *before*
  Testray has even created the Build row for it (measured live 2026-09-15: a
  ~4-minute gap between the hook firing and the row existing at all). Until
  that row appears, the newest build Testray can report IS the previous one,
  already DONE — `await_import()` deliberately does not treat that as "ready"
  by itself; it waits for a build with a *different* id to show up DONE.

## Symptom: `! poll failed: HTTP Error 404: Not Found`

An old version of `watch` looking for `TriageRun` rows on an instance where our
client extension is not deployed. Current versions probe first and fall back to
marker files. Update the checkout.

The same 404 also appears — correctly — from `preflight`, where it is a warning,
not an error:

```bash
.venv/bin/testray-analysis preflight
```

## Symptom: no coloured diamond appears in the Testray build list

The diamond is drawn from `TriageRun` rows, so it needs the client extension
deployed **and** the OAuth application allowed to use it. Run `preflight` and
read which of the two it is:

- `404` — the extension is not deployed on that instance. Everything else still
  works; verdicts stay in the HTML report.
- `403` — the extension **is** deployed and this application has no
  `c_triagerun` scope. This is the important one: the queue cannot tell 403 from
  404, so it quietly uses marker files and refuses every write while looking
  like an undeployed extension.

Fix for 403: add `c_triageresult.everything`, `c_triagerun.everything` and
`c_triageroutinesetting.everything` to the OAuth2 application, and give the user
it acts as the **Testray Administrator** role. Use the plain `.everything` form
— `.read` and `.write` are accepted when saving and then dropped, so you only
find out at call time.

## Symptom: Slack got nothing, but the job succeeded

The message is written on **every Stable** tick — including one that analysed
nothing, where `scan` writes the `🔁 Still failing` block itself. A tick that
only analysed another routine writes no message at all and logs
`Slack: not written — routine N is not Stable`; that is expected, not a fault.
Otherwise, if Slack is silent the posting side is the problem, not the
analysis:

1. Is the notification set to post on **every build**? If it is set to "every
   failure", it will never post — a successful triage of a red build is a job
   *success*.
2. Is the message body `${FILE,path="slack/testray_analyzer_slack_message.txt"}`?
   The plugin cannot read a file without that token macro.
3. Does the file exist in the workspace after a run? The console names its path
   in step 0.

**Related symptom: Slack showed one pair when the tick analysed several.** The
file is cleared once per tick and appended to per pair, so one post covers them
all. If only the last appears, the checkout predates that fix — every `submit`
used to overwrite the same path while Jenkins posted it once. A post ending in
`… and N more pair(s) analysed this tick` is not that bug: it is the
`MAX_POST_CHARS` guard, which stops the post before Slack refuses it at 40,000
characters.

## Symptom: every commit link points at `liferay/liferay-portal`

The links should point at the repository that actually holds the routine's
commits — `brianchandotcom/liferay-portal` for Stable. When the tool cannot read
the git remote, it falls back to `liferay/liferay-portal`, where those commits do
not exist yet, so every link 404s.

```bash
git -C "$TRIAGE_REPO_PATH" remote get-url origin
```

It must contain `brianchandotcom`. `TRIAGE_ROUTINE_REMOTES="79529=origin"` names
a **remote inside that checkout**, not a URL. The script asserts this and
refuses to run when it is wrong, so seeing this symptom means an older version
or a bypassed check.

## Symptom: the same failure is analysed again and again

Each analysis costs money, so this matters. Two records stop a repeat, and they
answer different questions:

- a **TriageResult** row means *this signature has a verdict*
- a **DONE TriageRun** row means *this build pair has been looked at*

The second one exists because the first is not enough. A pair whose clusters
are all dropped by the write policy — never-ran, pre-existing, flaky,
auto-classified — never gains a TriageResult at all, so the verdict store
cannot see it, every scan calls its signatures new, and the pipeline re-prepares
and re-pays on every trigger. Stable pair `522890829 -> 522894597` was
classified twice in 100 minutes that way, writing one row each time.

Check what scan thinks has been done:

```bash
.venv/bin/testray-analysis scan --once --dry-run
```

`Runs on file: N pair(s) already analysed` is the count, and a skipped pair
prints `already analysed <routine>-<baseline>-<target>`. If that count is zero
on an instance that has been running for a while, the TriageRun Object is not
readable — check `preflight`, because a 403 there looks identical to "nothing
recorded yet".

**On a file-queue instance** (no Testray Objects) the record is the marker
directory instead:

```bash
ls "$TRIAGE_QUEUE/done" | wc -l
```

That count must grow and survive between builds. If it keeps resetting to zero,
`TRIAGE_QUEUE` is inside the Jenkins workspace and the workspace is being wiped.
Move it somewhere persistent, for example `/var/lib/triage/queue`. Note the
release-master job checks out both repos fresh on every run, so `done/` cannot
survive there — that agent depends on the TriageRun rows, not on markers.

To deliberately re-analyse a pair after a prompt or rubric change, pass
`--force` to `scan`. It costs a full classify: the bundle is prepared fresh and
nothing is reused. Never add it to the job to get a run through.

## Symptom: two runs at the same time

They cannot happen. The script takes a lock; the second run logs
`skipped: another tick still holds …` and exits 0. If you see two runs doing
work, `flock` is missing on the agent — the script warns about that at the top
of its output. Disable concurrent builds on the job as well.

## Symptom: it costs more than expected

Check three things, in this order:

1. **Was `--classify` on for a run nobody needed?** `scan` is free; only
   `watch --classify` spends.
2. **Is the same pair being re-analysed?** See the previous symptom.
3. **Is `hunks.txt` as large as the full diff?** Then the prompt carries the
   entire diff. It happens when the failures are batch jobs with no test name to
   match against the code. Look for `WARNING: no test_case fragments` in the
   prepare log.

---

## Things that are normal, and not bugs

- **A run that explains nothing.** The Slack message says `no verdicts were
  produced … Needs a human.` The job still succeeds.
- **A skipped run.** Overlap with a longer previous run.
- **A permanently red test being ignored.** No baseline exists, so no comparison
  is possible.
- **Verdicts that are all `FALSE_POSITIVE`.** Common when a whole build failed
  for infrastructure reasons. The message uses 🔍 rather than 🚨 for this.
