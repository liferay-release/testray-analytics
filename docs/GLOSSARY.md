# Glossary

The words this tool uses, in plain language. Read this once and the rest of the
documentation becomes much shorter.

---

## Testray words

**Routine**
A repeating group of CI builds. `ci:test:stable` is one routine. Each routine has
a number. Ours: **79529** is Stable, **590307** is Acceptance, **82964** is
Release.

**Build**
One run of a routine. It contains many test results. A build has a git commit —
the state of the code it tested.

**Case result**
The result of one test in one build: passed, failed, blocked, or not run. A
Stable build has a few hundred of them; a Release build has a few thousand.

**Import status**
Testray does not receive results the moment CI finishes. It imports them. The
tool only looks at builds whose import status is `DONE`, because a build still
importing has an incomplete list of failures.

**Object**
Liferay's name for a custom database table with a REST endpoint. This tool adds
three: `TriageResult`, `TriageRun` and `TriageRoutineSetting`. They only exist on
an instance where our client extension is deployed. On an instance without it,
`/o/c/triageresults` answers 404 and the tool works in a reduced mode.

**Client extension (CX)**
Code deployed alongside Testray that adds the triage screens and those three
Objects, without changing Testray itself.

---

## Words this tool invented

**Baseline and target**
The two builds being compared. The **baseline** is the older build. The
**target** is the newer build that failed. Everything the tool says is about the
difference between them.

**Signature**
A failure's error text with the changing parts removed — timestamps, ids, line
numbers. Two failures with the same signature are the same failure. This is what
lets the tool say "this is not new, we already explained it".

**Cluster**
All the failures in one build that share a signature. A cluster is the unit of
work: one cluster gets one verdict, because one cause broke all of them. Twenty
tests failing on the same broken import are **one** cluster, not twenty
problems.

**Verdict**
The tool's answer for one cluster.

| Verdict | Meaning |
|---|---|
| `BUG` | a product change caused this. Someone should fix the product |
| `POSSIBLE_BUG` | probably a product change, but the evidence is not conclusive |
| `TEST_FIX` | the test is wrong, not the product |
| `NEEDS_REVIEW` | the tool could not decide. A person must look |
| `FALSE_POSITIVE` | not a real failure — infrastructure, environment, or noise |

**Not attributable**
How a low-confidence `NEEDS_REVIEW` is displayed. It means "nobody could point
at a cause", which is different from "153 things for you to review".

**Confidence**
How sure the model is: high, medium or low. Shown as a coloured circle in Slack
— 🟢 high, 🟡 medium, 🔴 low, ⚪ nobody attributed it.

**Candidate**
A commit the model thinks might be the cause. A candidate marked
`explains: false` is the closest change in the range, **not** the cause — the
report and the Slack message label it differently on purpose, because naming
someone's commit as the cause of a broken build is a serious claim.

**Run bundle**
A directory under `runs/` holding everything one comparison produced: the list of
failures, the git diff, the relevant parts of the diff (`hunks.txt`), the prompt
sent to the model, the verdicts, and the HTML report. If you need to know why the
tool said something, the bundle is the evidence.

**Hunks**
The parts of the git diff that look related to the failures. The prompt is built
from these rather than from the whole diff, because a whole diff can be much
larger than the model can read.

---

## Pipeline words

**Prepare, classify, submit**
The three steps. `prepare` gathers evidence (free). `classify` asks the model
(**this is what costs money**). `submit` validates the answers, writes them to
Testray and renders the report (free).

**Producer and consumer**
Two things create work; one thing does it.

- Producers: `scan` (finds builds with unexplained failures) and the **Run
  Triage** button in Testray.
- Consumer: `watch`, sometimes called the drainer. It takes queued work and runs
  the three steps on it.

Running only the consumer does nothing, because nothing is queued.

**Queue**
Where waiting work sits. Two possible places, chosen automatically:

- **`TriageRun` rows** in Testray, when the client extension is deployed. This is
  better: Testray shows the state as a coloured diamond in the build list.
- **Marker files** in a directory, when it is not. Testray cannot see these, so a
  queued or failed run is invisible outside the logs.

**Tick**
One pass of the CI job: scan once, drain once. Jenkins runs a tick every 30
minutes.

**Dry run**
A run that does everything except spend money. `classify --dry-run` reports what
it would have sent, and its cost, without sending it.

---

## Money words

**The two-term cost**
A run costs a fixed amount plus an amount per cluster. This is why the tool works
hard to avoid re-analysing anything: a failure that already has a verdict is
skipped, and a build pair that has already been analysed is skipped.

**Engine**
Which service answers the classification.

- `api` — the Anthropic API. Bills per token. This is what CI uses.
- `claude-code` — the `claude` command-line tool, using a subscription instead of
  an API key. Needs the CLI installed and logged in.
