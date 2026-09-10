---
name: run-one-triage
description: Analyse one pair of Testray builds by hand — pick the pair, gather the evidence for free, then classify only if asked. Use when someone says "why did this build fail", "triage build NNNN", "run triage on this build", or wants to test the pipeline without waiting for Jenkins. Covers the dry run that shows the cost before anything is spent.
---

# Run one triage by hand

This walks through analysing **one pair of builds**: an older build that was
fine (the *baseline*) and a newer build that failed (the *target*). See
[GLOSSARY.md](../../../docs/GLOSSARY.md) for the words.

**Money warning.** Step 4 calls a Claude model and costs money. Everything
before it, and the dry run, cost nothing. Never run step 4 unless the person
asked for a real classification.

---

## Step 0 — is the tool installed and configured?

Do this first and do not assume. A fresh clone has neither a virtualenv nor a
config file, and both are gitignored, so "it worked last week" is not evidence.

**The virtualenv:**

```bash
ls .venv/bin/testray-analysis
```

Missing? Build it. The editable install (`-e`) is what keeps `config/` and
`slack/` resolving inside this checkout:

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e .
```

**The configuration:**

```bash
ls config/config.yml; env | grep TESTRAY_
```

Settings can come from either, and the environment wins. If there is neither,
copy the template — `cp config/config.yml.example config/config.yml` — and fill
in four values:

| Setting | Value |
|---|---|
| `testray.base_url` | `https://testray.liferay.com` |
| `testray.ui_url` | `https://testray.liferay.com/web/testray` |
| `testray.client_id` / `client_secret` | an OAuth2 client-credentials application; read-only is enough |
| `git.repo_path` | a `liferay-portal` checkout — required, the analysis diffs the two builds' commits |

**Never invent credentials, and never read them out of another project's
config.** Ask the person for them, and tell them the file is gitignored so
nothing is committed.

**Then check the git side, because it fails silently:**

```bash
ls "$(grep -A1 '^git:' config/config.yml | grep repo_path | cut -d: -f2- | tr -d ' ')"
git -C <that path> remote get-url <the remote in routine_remotes>
```

`git.routine_remotes` maps a routine to a **remote name inside that checkout**,
not a URL. For Stable (79529) it must resolve to `brianchandotcom/liferay-portal`
— Stable's commits reach `liferay/liferay-portal` only later. Get this wrong and
nothing errors: every commit link in the report points at a repository that does
not have the commit.

If any of this is missing, fix it with the person before going further. A run
started on a broken config wastes their time at step 3, twenty minutes in.

---

## Step 1 — check the instance answers

```bash
.venv/bin/testray-analysis preflight
```

Read the last block:

- `Ready: rows queue in Testray and verdicts write back.` — everything works.
- `Usable, in degraded mode` — the run will work, but verdicts cannot be saved
  into Testray. You will get an HTML report only. This is normal on an instance
  where our client extension is not deployed.
- `FAIL … the Object is there, but this app has no c_triagerun scope` — stop.
  The OAuth application is missing a scope. Fix that first; see
  [JENKINS-SETUP.md](../../../JENKINS-SETUP.md) §4.

If preflight cannot mint a token, the credentials are wrong. They come from the
environment: `TESTRAY_CLIENT_ID` and `TESTRAY_CLIENT_SECRET`.

---

## Step 2 — choose the two builds

You need two build ids from the same routine.

The pair must be **comparable**: both builds must have run the same set of
tests. If the target ran a different set, the comparison is meaningless — and it
will look like a healthy build rather than an error, which is the trap here.

Two ways to pick:

**Let the scanner choose.** This is the reliable way, because the scanner picks
the baseline per failure — the most recent build that did *not* have that
failure:

```bash
.venv/bin/testray-analysis scan --once --dry-run
```

It prints the pairs it would queue, and writes nothing.

**Choose by hand.** Open the routine's build list in Testray and take the failed
build plus the build before it. Prefer two builds that are close together: the
fewer commits between them, the better the answer.

---

## Step 3 — gather the evidence (free)

```bash
./scripts/triage_pipeline.sh -b <baseline id> -t <target id> --no-classify
```

This reads both builds from Testray, works out which failures are new or
changed, groups them into clusters, and computes the git diff between the two
builds' commits. It prints one line per step and ends with the bundle path:

```
BUNDLE=/…/runs/r_20260909T181722Z_520104412_520106758
```

Look inside that directory. It is the evidence:

| File | What it holds |
|---|---|
| `diff_list_subtasks.csv` | one row per cluster: the tests, the shared error, the components |
| `hunks.txt` | the parts of the git diff that look related to the failures |
| `git_diff_full.diff` | the whole diff, as a fallback |
| `prompt.md` | exactly what would be sent to the model |
| `tickets_in_range.txt` | the tickets whose commits are in this range |

**If `hunks.txt` is the same size as `git_diff_full.diff`**, the filtering found
nothing to narrow. Check the prepare log for
`WARNING: no test_case fragments`. It means the failures are batch jobs
(`modules-compile[…]`) with no test name to match against the code, so the
whole diff would be sent. Say so — the classification will be expensive and
weak.

The pipeline needs a local `liferay-portal` checkout to compute the diff. If a
commit is missing from it, fetch first. For Stable, the commits are in
`brianchandotcom/liferay-portal`, not `liferay/liferay-portal`.

---

## Step 4 — classify (this costs money)

Always dry-run first. It prints the batch plan, the size, the estimated tokens
**and the estimated cost**, and sends nothing:

```bash
.venv/bin/testray-analysis classify <bundle> --dry-run
```

Report the plan to the person and get agreement. Then, only if they agreed:

```bash
.venv/bin/testray-analysis classify <bundle>
```

If it stops partway — a bad response, a network drop — just run it again. It
journals each finished batch to `results.partial.jsonl` and replays those
instead of paying for them twice.

**There is a $15 limit per run.** If the estimate is over it, `classify`
refuses to start and prints the figure; if measured spend crosses it mid-run,
it stops after the current batch and keeps what was already paid for. Report
the number to the person rather than raising the limit yourself. Someone who
wants to spend more sets `TRIAGE_MAX_COST_USD` deliberately.

---

## Step 5 — write the results and read them

```bash
.venv/bin/testray-analysis submit <bundle>              # local instance with the Objects
.venv/bin/testray-analysis submit <bundle> --no-write   # anywhere else, including prod
```

This validates the verdicts, renders `report.html` inside the bundle, writes the
Slack message to `slack/testray_analyzer_slack_message.txt`, and — if the
Testray Objects exist — saves the verdicts into Testray.

**Against prod, use `--no-write`.** Prod has no `TriageResult` Object, so there
is nothing to write to and nothing should try. Step 0's preflight already told
you which case you are in: `Ready` means the plain form works, `Usable, in
degraded mode` means `--no-write`.

Options worth knowing:

- `--no-write` — validate and render only. No Testray payload is built at all.
- `--dry-run` — build the payload but do not send it. For debugging the writer.
- `--report-url <url>` — put a link in each draft Jira ticket's footer.

Open `report.html` in a browser. One row per cluster, worst verdict first.

---

## Reading the answer

Lead with the clusters, not the failure count. Twenty tests failing on one
broken import is **one** problem, and reporting it as twenty misrepresents the
morning's work.

For each cluster, the useful sentence is: *this verdict, this confidence, this
candidate commit.* A candidate marked "closest in range" is **not** the cause —
it is the nearest change. Do not tell someone their commit broke the build on
that basis.

If every cluster came back `NEEDS_REVIEW`, say that plainly. The run explained
nothing, and that is a normal outcome for a routine whose failures are all
infrastructure.
