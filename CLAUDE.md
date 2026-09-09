# CLAUDE.md — testray-analytics

Read this before answering questions about this repo or running anything in it.

## What this tool does

Testray records the result of every test in every CI build. When a build is red,
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

**Plain-language vocabulary is in [docs/GLOSSARY.md](docs/GLOSSARY.md).** If a
reader is new to this tool, start there rather than in ARCHITECTURE.md.

## The five commands

All of them are subcommands of one entry point, `testray-analysis`, installed in
the repo's virtualenv at `.venv/bin/testray-analysis`.

| Command | What it does | Spends money |
|---|---|---|
| `prepare` | reads two builds, computes the diff, writes a run bundle | no |
| `classify` | asks the model for a verdict per cluster | **yes** |
| `submit` | validates, renders the report, writes verdicts to Testray | no |
| `scan` | queues builds that have unexplained failures | no |
| `watch` | drains the queue by running prepare → classify → submit | **yes, with `--classify`** |
| `preflight` | checks credentials, OAuth scopes and the Testray Objects | no |

`scripts/triage_pipeline.sh` runs prepare → classify → submit in order. It is the
single definition of that sequence — Jenkins, `watch` and humans all call it, so
never re-implement the order somewhere else.

`scripts/triage_jenkins.sh` is the whole CI job: `scan` then `watch`, with
preflight checks and a lock.

## Rules that cost money or time when broken

**Never run `classify` or `watch --classify` without being asked to.** Each call
bills the Anthropic API. When someone asks for a test run, use `--dry-run`
(classify) or `--no-classify` (watch), both of which do everything except the
model call and report what the call *would* have been.

**Always run `preflight` first against an instance you have not used before.** It
tells the difference between "the client extension is not deployed" (404, fine,
degraded mode) and "this OAuth app is missing a scope" (403, a misconfiguration
that looks identical to the queue and silently refuses every write).

**Secrets never go in a file.** `config/config.yml` and `config/config.prod.yml`
are gitignored. Credentials belong in the environment:
`TESTRAY_CLIENT_ID`, `TESTRAY_CLIENT_SECRET`, `ANTHROPIC_API_KEY`. If someone
asks where to put a key, point them at the environment, never at a config file.

**A config file is optional.** Every setting a CI run needs has an environment
variable; the table is at the top of `config/config.yml.example`. The
environment always wins over the file.

**Routine ids mean different things and must not be mixed up.**

| Id | Routine | Repo its commits live in |
|---|---|---|
| 79529 | Stable (`ci:test:stable`) — the control repo, every commit merges here first | `brianchandotcom/liferay-portal` |
| 590307 | Acceptance | `liferay/liferay-portal` |
| 82964 | Release | `liferay/liferay-portal-ee` |

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
| `JENKINS-SETUP.md` | the CI job: fields, agent setup, troubleshooting |
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
