# CLAUDE.md — testray-analytics

Read this before answering questions about this repo or running anything in it.

## What this tool does

Testray records the result of every test in every build. When a build is red,
someone has to work out *why*. This tool does that first pass automatically:

1. It compares two builds of the same routine — an older one that was fine, and
   the newer one that failed.
2. It groups the failures that share the same error into one **cluster**.
3. It reads the git commits between the two builds and asks a Claude model which
   commit could have caused each cluster.
4. It writes the answer back into Testray, renders an HTML report, and writes a
   Slack message.

It never edits product code and never opens a pull request. It explains; people
fix.

**The Release Tools team owns and runs this.** It is scheduled on
**release-master**, not by a central CI group, so "who do I ask about the job"
and "who do I ask about the tool" have the same answer. Where the docs say CI,
they mean the system that produced the test builds Testray recorded — never the
owner of this pipeline.

**Plain-language vocabulary is in [docs/GLOSSARY.md](docs/GLOSSARY.md).** If a
reader is new to this tool, start there rather than in ARCHITECTURE.md.

## The commands

One entry point — `.venv/bin/testray-analysis <command>` — and two scripts that
run those commands in the right order. **Money is spent in exactly one place:
`classify`.** Everything else is free; the steps above it only decide whether
that one gets reached.

`scripts/triage_jenkins.sh` — the entry point on **release-master**, run
every 30 minutes:

| Command | What it does |
|---|---|
| `preflight` | checks the credentials, the OAuth scopes and whether the Testray Objects answer |
| `scan` | queues builds that have failures nobody has explained yet |
| `watch` | if anything is queued, runs the pipeline below on each queued build pair. Without `--classify` it stops after `prepare`, so it spends nothing |

`scripts/triage_pipeline.sh` — one build pair, start to finish. `watch` calls
this, and a person can call it directly:

| Command | What it does |
|---|---|
| `prepare` | reads two builds, groups the failures into clusters, computes the git diff between the two commits |
| `classify` | asks a Claude model for a verdict per cluster; this can be via Claude Code subscription or Anthropic API — **the step that costs money** |
| `submit` | validates the verdicts, renders the HTML report, writes the verdicts back to Testray, writes the Slack message for Stable failures|

Those two scripts are the single definition of their sequences. Jenkins, `watch`
and humans all call them, so never re-implement the order somewhere else.

## Rules that cost money or time when broken

**Do not run `classify` or `watch --classify` yourself unless you were asked
to.** Each call bills the Anthropic API. When someone says "try it" or "test
it", use `--dry-run` (classify) or `--no-classify` (watch): both do everything
except the model call, and report what the call would have cost.

This is about *you*, working interactively. On release-master the job classifies
unattended on every tick — that is the whole point of it — and what protects the
bill there is not restraint but the cap below.

**One run cannot spend more than $15.** `classify` estimates the cost before it
sends anything and refuses to start if the projection is over the limit, naming
the figure and telling the reader to fork the repo and run it locally if they
want to spend more. It also stops between batches if measured spend crosses the
limit, keeping the verdicts already paid for. Raise it deliberately with
`TRIAGE_MAX_COST_USD=<n>`; the release-master job can lower it the same way.
Never raise it to get a run through without saying so.

**Always run `preflight` first against an instance you have not used before.** It
tells the difference between "the client extension is not deployed" (404, fine,
degraded mode) and "this OAuth app is missing a scope" (403, a misconfiguration
that looks identical to the queue and silently refuses every write).

**Secrets never go in a file.** `config/config.yml` and `config/config.prod.yml`
are gitignored. Credentials belong in the environment:
`TESTRAY_CLIENT_ID`, `TESTRAY_CLIENT_SECRET`, `ANTHROPIC_API_KEY`. If someone
asks where to put a key, point them at the environment, never at a config file.

**A config file is optional.** Every setting a release-master run needs has an
environment variable; the table is at the top of `config/config.yml.example`.
The environment always wins over the file.

**Routine ids mean different things and must not be mixed up.**

| Id | Routine | Repo its commits live in |
|---|---|---|
| 79529 | Stable (`ci:test:stable`) — the control repo, every commit merges here first | `brianchandotcom/liferay-portal` |
| 590307 | Acceptance | `liferay/liferay-portal` |
| 336020509 | ci:test:cms | `liferay/liferay-portal` |
| 82964 | EE Package Tester (7.4 Release tester) | `liferay/liferay-portal` |

`git.routine_remotes` maps a routine id to a **git remote name inside the local
checkout** — not a URL. If it is wrong, nothing errors: every commit link falls
back to `liferay/liferay-portal`, where Stable's commits do not exist yet.

## Running the tests

```bash
.venv/bin/python -m pytest -q --ignore=tests/test_prompt_size.py --ignore=tests/test_resume.py
```

Expect `216 passed, 11 skipped`. The two ignored files are specifications for
work that is not finished — they fail at import, on purpose, and are not
committed. Do not "fix" them by changing the source to match unless that is the
task you were given.

## Where things are

| Path | What |
|---|---|
| `JENKINS-SETUP.md` | the release-master job: fields, agent setup, troubleshooting |
| `docs/GLOSSARY.md` | plain-language terms |
| `ARCHITECTURE.md` | the full design and the reasoning behind every decision. Long. Search it; do not read it end to end |
| `tests/TESTING.md` | how the tool is validated |
| `testray_analytics/analysis/` | the pipeline |
| `scripts/` | the two orchestration scripts |
| `runs/` | run bundles — prompt, diff, hunks, verdicts, report. Gitignored |
| `logs/` | one file per pipeline step. Gitignored |
| `slack/` | the generated Slack message. Gitignored |
| `.claude/skills/` | step-by-step guides for the common tasks |

## House style for this repo

Comments explain **why**, not what. Most comments in this codebase name a real
failure that happened — a wrong verdict, a silent 403, a doubled bill — because
that is what stops the next person from undoing the fix. Match that when you add
code: if a line looks odd and the reason is not obvious, say what breaks without
it.
