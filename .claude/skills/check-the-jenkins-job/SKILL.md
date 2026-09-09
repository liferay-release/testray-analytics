---
name: check-the-jenkins-job
description: Diagnose the scheduled triage job on Jenkins — it failed, it posted nothing to Slack, it says "Nothing queued", no diamond appears in Testray, or the commit links point at the wrong repository. Use for any question about why the every-30-minutes triage job is not behaving. Each symptom here has one known cause and one fix.
---

# Check the Jenkins triage job

The job runs `./scripts/triage_jenkins.sh` every 30 minutes. Its setup is
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
| 0 | ran, **or** skipped because another run still holds the lock | nothing. A skip is normal under a 30-minute trigger |
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
  baseline to compare against, so it is deliberately not analysed.

If the build step only calls `watch`, that is the cause: `watch` consumes work,
it does not create it. The build step must call `scan` first. `triage_jenkins.sh`
does both.

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

The message is written on **every** run, including one that concluded nothing.
So if Slack is silent, the posting side is the problem, not the analysis:

1. Is the notification set to post on **every build**? If it is set to "every
   failure", it will never post — a successful triage of a red build is a job
   *success*.
2. Is the message body `${FILE,path="slack/testray_analyzer_slack_message.txt"}`?
   The plugin cannot read a file without that token macro.
3. Does the file exist in the workspace after a run? The console names its path
   in step 0.

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

Each analysis costs money, so this matters. The tool skips a failure that
already has a verdict and a build pair that has already been analysed. On an
instance **without** the Testray Objects there is no verdict store, so the only
record is the marker directory:

```bash
ls "$TRIAGE_QUEUE/done" | wc -l
```

That count must grow and survive between builds. If it keeps resetting to zero,
`TRIAGE_QUEUE` is inside the Jenkins workspace and the workspace is being
wiped. Move it somewhere persistent, for example `/var/lib/triage/queue`.

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
