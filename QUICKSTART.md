# Quickstart — analyze a failing build and read the report

For anyone who wants to analyze a build and does not
want to startup a local, empty Testray instance. 
No Docker, no client extensions, no local instance.

**About 15 minutes**, most of it a `pip install`.

You read data from the real Testray over its API, the analysis runs on your
machine, and the product is an HTML report you open in a browser.

> **Supporting the release-master job, or need the Testray screens** — the
> Triage column, the diamond, the report page inside Testray? That needs a local
> instance: [QUICKSTART-LOCAL-TESTRAY.md](QUICKSTART-LOCAL-TESTRAY.md).

New to the words — routine, cluster, verdict, baseline? Read
[docs/GLOSSARY.md](docs/GLOSSARY.md) first. It is short.

---

## The short version, if you have Claude Code

Clone the repo, open Claude Code in it, and ask:

> triage build 520106758

It follows the [`run-one-triage`](.claude/skills/run-one-triage/SKILL.md) skill,
which starts by checking the things everything else depends on: the virtualenv,
the config file, your Testray credentials, and whether the portal checkout's git
remote actually carries the routine's commits. It sets up what is missing and
asks you for anything only you can supply — credentials, mainly.

Then it runs preflight, picks a comparable pair, gathers the evidence for free,
and shows you the cost **before** spending anything. It is told not to classify
unless you asked it to.

Everything below is what it does on your behalf. Read it when you want to do
this by hand, or to understand what it did — not before.

---

## What you need

| What | Notes |
|---|---|
| This repo | `github.com/liferay/liferay-testray-analytics` |
| Python 3.13 | earlier 3.11+ works too |
| Testray API credentials | **read-only is enough**. An OAuth2 client-credentials application on `testray.liferay.com` |
| A `liferay-portal` checkout | **the one heavy item — about 35 GB.** Unavoidable: the analysis works by reading the git commits between the two builds, so it needs the actual repository |
| A way to call Claude | either the `claude` CLI you already sign in to, or an Anthropic API key |

The portal checkout must be the repository the routine's commits actually land
in. For **Stable** that is `brianchandotcom/liferay-portal` — its commits reach
`liferay/liferay-portal` only later, so a checkout of the latter will not
contain them.

---

## Step 1 — install

```bash
git clone git@github.com:liferay/liferay-testray-analytics.git
cd liferay-testray-analytics
python3.13 -m venv .venv
.venv/bin/pip install -e .
```

The `-e` matters: it keeps `config/` and `slack/` resolving inside this
checkout.

---

## Step 2 — configure

```bash
cp config/config.yml.example config/config.yml
```

Edit four values. Everything else in that file has a working default:

```yaml
testray:
  base_url: "https://testray.liferay.com"
  ui_url: "https://testray.liferay.com/web/testray"
  client_id: "<your OAuth2 application>"
  client_secret: "<its secret>"

git:
  repo_path: /home/you/dev/projects/liferay-portal
  routine_remotes:
    79529: brianchandotcom        # the git REMOTE NAME in that checkout, not a URL
```

`config/config.yml` is gitignored — secrets never get committed. If you prefer
environment variables, every setting has one; the table is at the top of
`config.yml.example`.

**`routine_remotes` is the setting people get wrong.** It maps a routine to a
remote *name inside your checkout*. Get it wrong and nothing errors — every
commit link in the report just points at a repository that does not have the
commit. Check yours:

```bash
git -C /path/to/liferay-portal remote get-url origin   # must be brianchandotcom for Stable
```

**Then confirm the credentials work:**

```bash
.venv/bin/testray-analysis preflight
```

Expect this, and it is fine:

```
ok    token minted, 16 scope(s)
warn  /o/c/triageresults → 404, the analytics client extension is not deployed …
Usable, in degraded mode: … verdicts stay in the local report.
```

Prod might not yet have the client extension, so verdicts cannot be written back
there. For this path that changes nothing: the report is the product.

---

## Step 3 — pick two builds

Open the routine in Testray and take the build that failed, plus the build
before it. Both ids are in the URL.

The two builds must have run the **same set of tests**. If they did not, the
comparison is meaningless — and it looks like a healthy build rather than an
error, which is the trap.

Prefer builds close together. The fewer commits between them, the better the
answer, because there are fewer candidate causes.

---

## Step 4 — gather the evidence (free)

```bash
./scripts/triage_pipeline.sh -b <older build id> -t <newer build id> --no-classify
```

This reads both builds over the API, works out which failures are new or
changed, groups them into clusters, and computes the git diff between the two
builds' commits. It ends with the bundle path:

```
BUNDLE=/…/runs/r_20260909T181722Z_520104412_520106758
```

Nothing has been sent to a model yet, and nothing has cost anything.

If it fails saying a commit is missing, fetch it — a build can only be analysed
once its commit is in your checkout:

```bash
git -C /path/to/liferay-portal fetch origin
```

---

## Step 5 — get verdicts (this costs money)

You can look at the plan first. This sends nothing and costs nothing:

```bash
.venv/bin/testray-analysis classify runs/r_<id> --dry-run
```

It prints how many clusters, how many calls, how much prompt, and the estimated
cost. Then pick an engine:

```bash
# Your Claude subscription, through the CLI you already sign in to.
.venv/bin/testray-analysis classify runs/r_<id> --engine claude-code

# Or the Anthropic API, billed per token. Needs ANTHROPIC_API_KEY.
.venv/bin/testray-analysis classify runs/r_<id> --engine api
```

`claude-code` is usually the right choice here: it uses the Claude Code
subscription rather than an API key, and it deliberately ignores
`ANTHROPIC_API_KEY` so a stale key cannot silently bill you.

For scale: a **Stable** run is 1–2 clusters and costs about **$0.27** on the
API. A **Release** run is a different animal — one measured at **$200**, because
its shared prompt is 1.87 MB and gets re-sent on every call. `--engine api` is
capped at **$15 per run** and refuses to start above that; `--dry-run` tells you
before you commit to anything.

If it stops partway, run it again. Each finished batch is journalled and
replayed rather than re-bought.

---

## Step 6 — read the report

```bash
.venv/bin/testray-analysis submit runs/r_<id> --dry-run
```

`--dry-run` renders the report and builds the Testray payload, but sends
nothing: **nothing on this path writes to Testray.** If you do not even want the
payload file, use `--no-write` instead — it stops after validating and
rendering. Either is safe; neither touches prod.

You get:

| File | What it is |
|---|---|
| `runs/r_<id>/report.html` | **open this** — one row per cluster, worst verdict first |
| `slack/testray_analyzer_slack_message.txt` | the same findings as a Slack post |
| `runs/r_<id>/` | the evidence: prompt, diff, hunks, verdicts |

Reading it well:

- **Count clusters, not failures.** Twenty tests failing on one broken import is
  one problem. Reporting it as twenty misrepresents the work.
- A candidate marked **"closest in range"** is *not* the cause — it is the
  nearest change. Do not tell someone their commit broke the build on that
  basis.
- **All `NEEDS_REVIEW` is a real answer.** It means the run explained nothing,
  which is normal when the failures are infrastructure.

---

## When it goes wrong

| Symptom | Cause |
|---|---|
| `preflight` cannot mint a token | wrong `client_id` / `client_secret`, or stale `TESTRAY_*` variables in your shell overriding the file. `env \| grep TESTRAY_` should be empty |
| `warn … 404` on the `/o/c/triage*` endpoints | expected on prod. The client extension is not deployed there; the report still works |
| `prepare` fails on a missing commit | fetch the routine's remote in your portal checkout |
| Every commit link points at `liferay/liferay-portal` | `routine_remotes` names a remote that does not exist in your checkout, so the tool fell back to a default |
| `hunks.txt` is the same size as `git_diff_full.diff` | the failures are batch jobs with no test name to match against code, so nothing could be narrowed. The verdicts will be weak — say so rather than trusting them |
| `classify` refuses to start | the $15 per-run cap. The message names the figure and what to do |
