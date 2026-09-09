# testray-analytics

Additive analytics module for Testray. Reads Testray over REST, analyzes test
results, and writes results back for a client extension to render — without
patching core Testray.

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full design, decisions, and the
local dev/test loop.

## Modules

- **`analysis/`** — regression triage. Classifies new *or changed* failures
  between two builds (`BUG / POSSIBLE_BUG / TEST_FIX / NEEDS_REVIEW /
  FALSE_POSITIVE`), clusters them, and writes `TriageResult` verdicts back to
  Testray. *(current milestone)*
- `insights/` — dashboards / metrics / scoring. *(future)*
- `custom_suite/` — test triggering / custom suites. *(future)*

## Setup

```bash
pip install -e .
cp config/config.yml.example config/config.yml   # fill in Testray base_url + OAuth
export ANTHROPIC_API_KEY=...
```

The tool is **REST-only** (no database) and needs a local `liferay-portal`
checkout (`git.repo_path` in config) to compute the diff between build hashes.

## Usage — analysis (build-vs-build)

```bash
testray-analysis prepare  --baseline-build-id <A> --target-build-id <B>
testray-analysis classify runs/r_<id>
testray-analysis submit   runs/r_<id>
```

`prepare` reads caseresults for both builds over REST, computes the
new/changed-failure diff + relevant hunks, and writes a run bundle. `classify`
sends the bundle to the Anthropic API. `submit` validates the results and writes
`TriageResult` rows to Testray. See ARCHITECTURE.md §14 for the local loop.

## Usage — unattended (scan + watch)

```bash
testray-analysis scan  --once            # queue what is unexplained, spend nothing
testray-analysis watch --classify        # drain the queue through the pipeline
```

`scan` queues a build when it is `importStatus` DONE, has at least one FAILED
case result, and carries a failure signature Testray has no verdict for.
Routines opt in via `triage.scan.routines` in config; cadence belongs to the
caller (cron, Jenkins, `--interval`, `TRIAGE_SCAN_INTERVAL`). `watch` drains
both producers — the scanner and *Run Triage* in the Testray UI — and
`--classify` is opt-in because it is the step that costs money. See
ARCHITECTURE.md §9 "Auto-queue" for the queue backends and what an instance
without the analytics Objects cannot do.

> Dev status: foundational extraction (LPD-95842). The headless Testray write
> sink lands in LPD-95843; until then `submit` writes the exact batch payload
> locally for inspection.
