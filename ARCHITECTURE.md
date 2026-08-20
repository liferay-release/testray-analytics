# testray-analytics — Architecture

**Epic:**  [LPD-95835](https://liferay.atlassian.net/browse/LPD-95835).

**Last updated:** 2026-07-08

---

## 1. What this is

`testray-analytics` is a **new, additive analytics module for Testray**. It is the natural next step from Testray as a Test Management tool displaying raw test results. The Analytics module will provide script-, LLM-, and AI-assisted analysis to help teams make informed decisions during development and release. This repo houses the scripts to

1. run on **Jenkins** (or locally),
2. read Testray build/case data over **REST**,
3. **classify** PASSED→FAILED regressions (`BUG / POSSIBLE_BUG / TEST_FIX /
   NEEDS_REVIEW / FALSE_POSITIVE`), and
4. write the verdicts **back into Testray over REST** as a Liferay Object that a
   **client extension** renders inline.

The repo is named `testray-analytics` deliberately to prepare for a
future **Insights** module (dashboards / risk metrics) alongside `triage/`.

### Guiding principle — additive, not invasive

We add a module **beside** Testray; we don't evolve its core. No patching Testray
Java, no changes to its Object definitions, no touching its native workflow objects
(notably **`Subtask`** — see §6). Our only touch points are to **read** Testray's
existing REST APIs and **write** our own new Objects (via our own site-initializer
CX) — keeping the feature independently deployable, removable, and conflict-free
with current Testray.

**The line we draw — structure vs. data.** We don't change Testray's *structure* —
object definitions, core Java, or the Subtask/Task workflow. We *do* write to one
piece of native *data*: the free-text `CaseResult.issues` field, via the public
REST API, exactly as a user does by hand today (§11). That write happens in the
**frontend CX (a user action)** — never in the headless Jenkins tool, which only
*reads* `issues` for dedup.

**One scoped exception:** ticket *creation* extends the existing **`etc-jira`**
client extension (§11). `etc-jira` is itself a client extension, not core Testray
Java — so extending it stays inside the "add/extend client extensions, don't touch
core" boundary.

---

## 2. End-to-end shape

```
Jenkins job (Python triage tool — runs as a CLI; container only if Jenkins needs it)
   │   OAuth2 client_credentials  (one service-account app, scoped)
   │
   ├── READ    Testray REST — caseresults for baseline build A + target build B [api source only]
   │
   ├── COMPUTE new & changed failures across the transition matrix (§12), relevant-hunk extraction, and test matching in Python
   │
   ├── REASON  Anthropic API — classify each test failure + hunk match 
   │
   └── WRITE   Testray REST — POST our TriageResult objects (batch)
                          │
                          ▼
              TriageResult Object   (our module — NOT a Testray object)
                 · linked to Testray CaseResult (FK on OUR side)
                 · carries clusterKey (our own grouping — NOT Testray Subtask)
                          │
                          ├── Frontend custom-element CX renders triage in Testray
                          │
                          └── Future 'Insights' reads /o/c/triageresults on its own
                              schedule — labeled training data for future NN work
```

Two outbound dependencies from Jenkins: **Testray REST** and **Anthropic**.
**Zero database credentials on Jenkins.** Dev and prod run identical code,
differing only by config — `base_url` + per-env credentials.

**Third input — a local `liferay-portal` checkout.** COMPUTE runs `git diff` and
`git log A..B` against a local checkout (Testray supplies *which* hashes via
`gitHashA`/`gitHashB`; the checkout supplies the code). This is the source of both
the hunks *and* `suspiciousCommits`. `liferay-portal` is multi-GB, so the Jenkins
agent keeps a **persistent checkout with the release branches** and `git pull`s each
run — never a fresh clone (too slow). Checkout strategy = open-Q #3.

**Off-origin build commits.** A Testray build records only its `gitHash` — **not**
the repo/branch it ran on (verified against a real build). So a commit that isn't
reachable from `origin` — e.g. a **temp mitigation branch on a fork** (official HEAD
+ a fix commit that never lands upstream) — can't be auto-fetched; there's no
metadata to derive the source from. The CLI handles this with **`prepare --fetch-ref
<remote-or-url> <ref>`** (also accepts a GitHub `…/tree/<branch>` URL), which fetches
the commit into the checkout before diffing; if a hash still can't be resolved,
`prepare` fails with that guidance rather than a cryptic git error. The eventual
triage **UI (LPD-95844) must offer the same input** when triaging an off-origin
build — again, nothing to auto-derive. (Having CI populate repo/branch on the build
would remove the need, but that's a Testray/CI-side change, outside this module's
additive scope.)

---

## 3. Firm decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Relationship to Testray | **Additive module** — no patching Testray code/objects | Independently deployable/removable; no conflict with current Testray. |
| 2 | Read transport | **REST only (`api` source)** | Prod Testray DB is SaaS/GCP — unreachable from Jenkins. REST is the stable, access-available contract. |
| 3 | Write transport | **REST (headless batch)** to our own Object | A raw DB INSERT would hand-populate Liferay system columns/FKs against a platform-owned schema; headless does it correctly and is prod-reachable. |
| 4 | Input sources | **`api` only** — drop `db` / `csv` / `tar` | Those were laptop conveniences pre-live-Testray. Once local Testray is up, `api` against `localhost` covers dev too. Big cleanup. |
| 5 | Dev vs prod parity | **Same code; per-env `base_url` + credentials** | The code never forks (no DB path — decision #4): local Testray DXP (backed by `testray_working_db`) in dev, SaaS REST in prod. Only *config* differs — `base_url`, OAuth2 `client_id`/`secret`, `ANTHROPIC_API_KEY` — all injected via `config.yml`/env, never in code. |
| 6 | Verdict grain | **Per `CaseResult`** | The rubric produces a per-failure `culprit_file`; that per-failure label is the training-data grain. |
| 7 | Verdict storage | **Our own `TriageResult` Object**, linked to `CaseResult`; FK lives on TriageResult | Testray's `CaseResult` definition is untouched. Auto-gets a `/o/c/triageresults` REST endpoint the CX reads. |
| 8 | Clustering | **Own `clusterKey`** (deterministic) on `TriageResult` | Do NOT reuse Testray's `Subtask`. Keeps us additive and avoids colliding with Testray's human-curated split/merge workflow (see §6). |
| 9 | Multiplicity | Multiple `TriageResult` per `CaseResult`, discriminated by `classifier` | Lets `api:…`, `agent:…`, and `human` verdicts coexist for comparison / override. Maps the old key `(build_id_b, case_id, classifier)`. |
| 10 | Tool interface | **CLI-first** (not Docker-first) | The previous Release Analytics Platform Dockerfile was legacy; it was never actually run as a container. Docker only re-appears for Jenkins if needed. |
| 11 | Canonical store | **Testray** (our Object) is the single sink | Future Insights module becomes a *consumer* (pulls for training data), not the owner. No dual-write from Jenkins. |
| 12 | Jira ticket write-back | Write the created ticket to **native `CaseResult.issues`** (module takes the user's role); create via **extended `etc-jira`** | Testray DB stays source of truth; manual + automated tickets unify in one field; dedup and future NN training read one place. See §11. |
| 13 | Transition detection | Surface **new *or changed* failures** across a transition matrix — not only PASSED→FAILED | A baseline with pre-existing failures makes PASSED→FAILED silently undercount regressions. See §12. |
| 14 | `TESTFIX` vs `TEST_FIX` | Never conflate Testray's `status=TESTFIX` (native, user-set) with our `TEST_FIX` **verdict** | Same words, different concepts: status is an input signal; verdict is our output. |
| 15 | Override / validate AI | A human override = a new `human`-classifier `TriageResult` row (LPD-95851); the AI row is preserved | Static `report.html` couldn't capture this; the Object + REST write path can. Keep AI vs. human separate for measurement. See §12. |
| 16 | Ticket creation is routine-gated | **No "Create ticket" for Stable** (and any gate the policy excludes); the CX enforces it | Quality-strategy policy — Stable protects fix velocity with no ticketing. See §11. |

---

## 4. What is NOT changing (inherited from RAP triage, verbatim)

- The **classification layer** — Anthropic API, batching, prompt caching,
  structured output, the rubric. API mode stays **pure-prompt** (no tool-use /
  agent loops) and **one-shot**.
- The **taxonomy** — `BUG / POSSIBLE_BUG / NEEDS_REVIEW / TEST_FIX /
  FALSE_POSITIVE`, with `culprit_file` semantics intact.
- The **reasoning** — transitive-dep tracing, test-fix ≠ bug, and baseline validity
  (the baseline must be a valid reference build; the exact rule is
  routine-dependent — see §8).

> **One thing that *does* change, despite this header — `NEEDS_REVIEW` discipline.**
>
> In the interactive/Claude-Code era, `NEEDS_REVIEW` was a *last resort* after
> exhausting a ~5-tool-call investigation budget, **not** a default. The production
> path here is **pure-prompt, one-shot** (no tool use), so that budget mechanism does
> not exist — which means `NEEDS_REVIEW` could quietly become an easy out. What keeps
> it honest instead is the **confidence-gated rubric** 
> * BUG = high confidence +
> verified culprit; 
> * POSSIBLE_BUG = a single medium candidate; 
> * NEEDS_REVIEW = 2+
> candidates / transitive / low confidence). 
> 
> The plan: **monitor the `NEEDS_REVIEW`
> rate** — the `POSSIBLE_BUG` and `TEST_FIX` tiers already absorb cases that used to
> land here, and **tune the rubric after analyzing human overrides**. Deliberately
> **no second classification pass** (extra tokens for little gain). See §13 (resolved).

## 5. What gets cut in the extraction

These pieces existed only because the previous Release Analytics Platform ran
locally, against whatever data access happened to be on hand. Moving to live Testray
changes the approach — the tool reads and writes everything over REST — so the items
below fall away naturally as part of the move, not as a redesign.

- **Sources:** `db` reader (`testray_analytical`), `csv` parser, `tar` parser, and
  the per-side source-switching (`SideSpec.source`, `--{side}-source/-csv/-hash`).
- **SQL:** `test_diff.sql`, `git_hash_lookup.sql`.
- **Write:** the Postgres upsert into `fact_triage_results` / `release_analytics`.

**Kept (load-bearing — do not over-cut):** the `api` reader + its case-name
enrichment (`fetch_case_metadata`, `enrich_api_case_names`,
`fetch_component_metadata`) — api caseresults lack test names, and enrichment is
what gives the hunk-matcher an anchor. Also kept: `compute_test_diff`, git-diff +
hunk extraction, pre-classification, and the classify layer.

---

## 6. Why we do NOT reuse Testray's `Subtask`

Testray's `Subtask` Object (`subtaskToCaseResults` oneToMany, with assignee,
`dueStatus`, Jira `issues`, and split/merge lineage) is effectively a human-curated
cluster. We **rejected reusing it** as our cluster identity (`clusterKey =
subtaskId`) to stay additive (decision #1), and **own a private `clusterKey`**
instead. Why:

- Writing native `Subtask`s from Jenkins pulls the tool into Testray's
  `Task → Subtask → CaseResult` lifecycle and can **collide with manual curation**
  (a human split/merge vs. an automated re-group next run).
- It would *evolve* Testray's workflow rather than sit beside it.

Surfacing our clusters as native Subtasks later is a separate, explicit integration.

---

## 7. Clustering contract

- **Where it's computed:** in the tool (deterministic), not ad hoc. (In the previous Release Analytics Platform, clustering was done by prompting Claude Code after the fact — that mechanism is gone once this runs headless on Jenkins, so it must be encoded.)
- **How:** `clusterKey` = a stable function of `culprit_file` + a normalized error
  signature (test `name` + normalized `error` message). Rows sharing a root
  cause/signature share a `clusterKey`.
- **Normalization is versioned.** `normalize()` *will* change (§12 flags it as "only
  trustworthy once decent"), and a silent change invalidates every stored
  `clusterKey` relative to newly-computed ones — breaking re-clustering continuity
  **and** §11 dedup (a recurring failure gets re-ticketed because old and new keys no
  longer match). Resolved as open-Q #9; the three decisions are:

  1. **The version tag is embedded in the key string**, not a second field:
     `clusterKey = "v<N>:" + sha256(signature)[:16]`, e.g.
     `v1:9c1f8a3d2e4b6071`. Key and version are one value, so they cannot drift
     apart, `startswith(clusterKey,'v1:')` answers "is anything stale?" in a
     single query (the field is already `indexedAsKeyword`), and no object-field
     is added — which matters: Liferay Objects puts fields declared at object
     creation on the base table and anything added later in the `_x` extension
     table, so a new field is not free once an instance exists.
  2. **Raw signature inputs are NOT copied onto `TriageResult`.** They are
     recomputed through the `CaseResult` FK: the raw `errors` and the test name
     live on Testray's row, `culpritFile` is already ours, and the relationship
     is `deletionType: cascade` — so the inputs are reachable for exactly as
     long as the verdict exists. Avoids a second copy that can disagree with
     Testray (decision #6, Testray stays source of truth) and avoids paying
     storage for data we already point at. If backfill throughput ever becomes
     the bottleneck, add one snapshot field then — not before.
  3. **A version bump backfills eagerly.** Bumping means incrementing
     `CLUSTER_KEY_VERSION` whenever `normalize()` changes behaviour; an explicit
     `--recompute-cluster-keys` pass then rewrites every stored key to the new
     version. Lazy/at-read recomputation is rejected: it pushes version-awareness
     into every consumer, which is exactly where the §11 re-ticketing bug would
     creep back in. Volume is one row per classified failure, so an eager pass is
     cheap. Startup warns when stored keys carry a version other than the current
     one.

- **Environment is cluster metadata, never part of the identity.** A build runs
  the same case across several `Run`s (Tomcat/Chrome/MariaDB/JDK combinations);
  the same error in two environments is **one** cluster, with the environments
  recorded on its members. Folding environment into the key would multiply
  cluster counts by the environment matrix for the common case (fails
  everywhere), while a cluster whose members are all one environment is a strong
  "environment-specific" signal on its own. Note `_aggregate_target` currently
  collapses a case's runs worst-status-wins, so the failure is never missed —
  but the environment dimension has to be carried deliberately if we want it.
- **Where it's stored:** as a field on `TriageResult`.
- **Where it's rendered — clustered by default.** The frontend CX groups
  `TriageResult`s by `clusterKey` (LPD-95844); **the clustered view is the default**,
  not per-test. Per-test is a **drill-down**: a cluster expands to its member rows
  (row-expand UX borrowed from `UseCase1-DevHeavy_report.html`), each row showing
  `Verdict / Confidence / Suspicious commits / Reasoning` (the column set the sample
  reports already use). Cluster-level verdict is a **rollup**; the stored verdict
  stays per-CaseResult.
- **Reasoning is first-pass and in scope — not Insights.** Each verdict carries its
  own `reason` (a `TriageResult` field, §9; a column in every sample report). It is
  core triage output produced on the first pass, never deferred. **Insights** is a
  separate, later layer for **visualization / dashboards / scoring** — it does not
  own reasoning. (A cluster-level *narrative* summary, if we ever add one, is still
  triage output, not Insights.)

### Classification granularity — `by-cluster` (default) vs `per-test` (LPD-95844)

Selected by `prepare --mode {by-cluster, per-test, by-subtask}`. **`by-cluster` is
the default as of 2026-08-18** and `per-test` is never chosen automatically. This is
orthogonal to the §8 *selection* mode (`analysisMode`); the granularity used is
recorded in `run.yml`'s `mode`.

- **`by-cluster`** — group failures by **error signature (§5) before the classifier
  runs**, classify **once per signature**, and **fan the verdict out to every member
  `CaseResult`** (the per-CaseResult grain, decision #6, is preserved). Shares the
  whole grouped code path with `by-subtask`; only the key differs.
- **`per-test`** — one prompt section per failure → a per-failure verdict +
  `culprit_file`. Kept for the A/B below and as a fallback when a signature is
  suspected of over-grouping.
- **`by-subtask`** — group on Testray's `Subtask` instead of our signature. Requires
  an `api` target source.

**Two clusterings exist, keyed differently, and that is forced.** Pre-send grouping
can only use the **error signature**; the full `clusterKey` also contains
`culprit_file`, which is an LLM *output* and does not exist yet. Post-classification
grouping (the report and the cluster view) uses the full `clusterKey`.

#### Measured on the 2026.q1.11 → q1.12-lts pair (449 classifiable failures)

| | `per-test` | `by-cluster` |
|---|---|---|
| Units sent | 449 | **169** (2.7× fewer) |
| Batches | 4 | 3 |
| Prompt content | ~346k tokens | **~222k tokens** (~36% less) |

**The token saving is far sublinear in the unit reduction** — 2.7× fewer units buys
only ~36% fewer tokens. Two reasons, both structural: **118 of the 169 groups are
singletons** (70% of groups, 26% of failures) and save nothing at all, and the
per-failure hunk context dominates the shared header, which is the part that
compresses. Do not size a run by unit count alone.

#### Caveats

1. **Over-grouping is the real risk, and it concentrates.** One verdict *and one
   `culprit_file`* is fanned across every member. On this pair the largest group is
   **102 failures on a single signature** — 102 `CaseResult` rows resting on one
   judgement. If `normalize()` merged two genuinely different causes, one is
   mislabelled and there is nothing in the output that reveals it.
2. **`normalize()` becomes load-bearing for verdicts, not just display.** Its
   measured behaviour (v2, against Nikki's hand-merges): tuning set 14/14 recall with
   0/55 false merges; held-out set 27/38 recall with **2/105 false merges**. Read
   those two error directions differently — **under-merging costs tokens, over-merging
   costs correctness**. The ~2% false-merge rate is the number that matters here, and
   it was measured on 105 group pairs, which is a small sample.
3. **The display clustering degenerates.** Because every member gets the same verdict
   and the same `culprit_file`, all members collapse to one `clusterKey` — so
   post-classification clustering becomes 1:1 with the pre-send grouping. Under
   `per-test`, one signature can *split* into several `clusterKey`s when the model
   names different culprits for different members. **`by-cluster` saves tokens by
   never asking a question whose answer could have split the group.**
4. **Training labels weaken.** `per-test` yields one independent
   (case, verdict, `culprit_file`) observation per failure. `by-cluster` yields one
   label replicated across N rows — correlated, not independent. The rows still land
   per-`CaseResult`, so nothing downstream breaks, but the supervision for the future
   NN work is thinner than the row count suggests.
5. **`group_id` is the fan-out contract, and the prompt truncates members.** Long
   member lists are cut for readability, so the `case_ids` a model emits are
   advisory; canonical membership is resolved from `group_id` against
   `diff_list_subtasks.csv` at submit time. If a model invents or renumbers a
   `group_id`, that group's members are silently dropped. `classify`'s own coverage
   line reads from the CSV for the same reason — summing parsed sections undercounted
   449 as 321.
6. **Blank signatures stay singletons** rather than collapsing into one "no error"
   cluster, which would be over-grouping in its worst form.
7. **Auto-classification runs first and is unaffected.** On this pair it screened 108
   failures into 8 groups before either mode saw them — including a 91-case
   `BUILD_FAILURE` group. Cheap screening beats clever grouping; check
   `triage.auto_classify_patterns` before optimising granularity.

#### Gaps

- **The A/B §7 asked for has not been run.** Both bundles now exist for the same
  pair (`r_20260818T161215Z_270750_270748` per-test,
  `r_20260818T162337Z_270750_270748` by-cluster) but neither has been classified.
  The 102-member group is the natural test: if `per-test` gives all 102 members the
  same verdict, the signature did not over-group and `by-cluster` is justified on
  evidence; if it splits them, that is a `normalize()` bug caught before it fanned a
  wrong verdict 102 times.
- **No over-grouping signal is surfaced at runtime.** Nothing warns when a single
  group is large enough that one wrong verdict would be costly. A size threshold that
  flags or force-splits the largest groups is unimplemented.
- **`normalize()` needs a third labelled set.** The held-out set has been spent on
  evaluation, so any further tuning has no clean measurement left.

---

## 8. Use-case flows = `api`-sourced build-selection modes

All three cluster use cases are `api`-sourced and share the same rubric; only
*what gets compared* differs. They are **selection modes**, not new sources.

| Ticket | Mode | Inputs | Routine | Selection logic | Order |
|--------|------|------|--------|-----------------|-------|
| LPD-95845 | `build-vs-build` | baseline build, target build | Stable, EE Package Tester (Release) | The A→B pair (already works in Release Analytics Platform). **Priority** ("run 1 vs run 2"). | **First slice** |
| LPD-95846 | `routine-history` | routine id, window (e.g. 90d) | Acceptance, Team Routines  | Triage across a routine's build history, not a single pair. | Later |
| LPD-95847 | `suite-vs-pr` | PR id, baseline build | PRs |  Target = the PR's build; baseline = reference build. | Later |

> **Baseline selection for `build-vs-build` is routine-specific:**
>
> * **EE Package Tester / Release (routine 82964)** → baseline = last **promoted**
> build; 
> * **Stable (routine 79529)** → baseline = last **fully-clean** build where  (`status=FAILED` count = 0). 
> 
> Both encode one invariant — the baseline must be a valid reference. Inherited from the Release Analytics Platform triage rule
> (`.claude/skills/triage.skill` §"Baseline validity"): a baseline that itself had
> failures makes the diff silently undercount — which is also what §12's
> changed-failure detection mitigates. This undercount is worst for **Stable**, an
> *all-or-nothing* suite where consecutive failed builds accumulate, so a wrong
> baseline hurts most there.

CLI gains `--mode {build-vs-build,routine-history,suite-vs-pr}` (default
`build-vs-build`). The `TriageResult` schema carries `analysisMode` so all three
coexist and the later modes slot in with no rework.

### Per-routine workflow defaults

Each routine has a **type** that sets its baseline rule, trigger, and commit
expectations. The pipeline **infers type from the routine name** — the routine object
(`/o/c/routines/{id}`) exposes a `name` but **no formal type field**. Verified across
the canonical routines, keyword-matchable (case-insensitive substring):

- `82964` → `"EE Package Tester"` → **Release** (`package tester`)
- `79529` → `"[master] ci:test:stable"` → **Stable** (`stable`)
- `590307` → `"EE Development Acceptance (master)"` → **Acceptance** (`acceptance`)

A small **config override** (`routines:` block) covers names that don't match a
pattern. Name-inference beats a hand-maintained ID map — 20+ routines and growing,
and the name is already in the API. It's a heuristic, so the tool **logs the inferred
type** and, on an unrecognized name, falls back to a safe default (+ warning) rather
than guessing; the override corrects misfires.

Notes: routine names **often embed the official branch** (`[master]`, `(master)`) —
not universal, not needed for the diff (hash-to-hash), but it confirms these routines
run **on-origin**, so off-origin is strictly a heavy-dev/PR concern. `autoanalyze` is
uniformly `false` on these three, so it is **not** a type/trigger discriminator — and
its meaning is no longer TBD: it is Testray's own *carry-forward* flag, not an
analysis trigger (see "Why not Testray's `autoanalyze`" below). We do not read it and
we do not set it.

| Type | Baseline | Trigger | Commit locality | Tickets |
|------|----------|---------|-----------------|---------|
| **Stable** (e.g. 79529) | last fully-clean build (`FAILED`=0) | build completion | origin | **no** (#16) |
| **Acceptance** (e.g. 590307) | across history | build completion | origin | yes |
| **Release / EE Pkg** (e.g. 82964) | last **promoted** build | promotion | usually origin, **sometimes off-origin** (release temp/mitigation fixes) → fetch source | yes |
| **Heavy-dev / PR** | reference build | user-driven | fork/temp → **off-origin common** → fetch source | yes |

**A build pair is only comparable if the two ran the same case set.** The diff
joins on case id, so suite re-selection between two builds silently shrinks it
to the intersection — the run still succeeds and still reports verdicts, just
about far fewer tests than the build ran. Measured across 2024 Q1's four builds:
q1.28 and q1.29 share 5,301 case ids (100%), Q1.30 and its retest share 3,274
(100%), and **any pair spanning those two groups shares 54 (1.6%)**. So pick
within a generation: consecutive builds of one routine usually share their whole
case set, and a build and its retest always do. Note that a build against its
own retest is a *flakiness* comparison, not a regression one — same code on both
sides. §12 rule 10 is what makes a bad pair visible in the report instead of
silently producing a tiny, confident-looking verdict list.

**PR builds: the fetch source is derived from the build name.** A pull-request
build's commit lives on a fork, so origin does not have it and `git diff` fails
with "NOT in this checkout" — the off-origin case below, and the first thing a
real queued PR run hit. Nothing on the `Build` object references the pull
request: `description` carries only a Jenkins report link and
`Portal Branch/SHA`, and there is no GitHub field anywhere. The **name** is the
only source, and it is structured:

    [master] ci:test:object - shuyangzhou > shuyangzhou - PR#12591 - 2026-08-19[11:47:41]
    [branch]  job          - sender      > receiver    - PR#<n>    - timestamp

`prepare.pr_fetch_spec()` parses that and fetches `pull/<n>/head` from
`git@github.com:<receiver>/liferay-portal.git`. **The receiver, not the sender** —
`pull/<n>/head` is a ref on the repo the PR was opened *against*. Every PR build
observed so far has sender == receiver, so that distinction is untested by the
data and is pinned by a test instead. The repo is always `liferay-portal`; the
owner is the only variable, and `git.pr_remote_template` overrides the whole
remote.

This is derived rather than asked for so a UI-triggered PR run needs no extra
input, but an explicit `--fetch-ref` always wins — it is a deliberate override.
The fetch is `--no-tags` and creates no branch and no checkout: the commit only
has to be reachable for `git diff`, and the working tree the user is sitting in
must not move.

**Off-origin handling follows from the type.** Origin-only routines run fully
automated (no fetch source needed). The types that *can* be off-origin (Release edge
cases, Heavy-dev/PR) are exactly the ones where a fetch source is supplied — by a
human at the CLI (`--fetch-ref`) or, for a UI-triggered run, a UI fetch-source input
(§2). If a diff still can't be computed, the run records a **"diff unavailable —
off-origin"** status rather than crashing (degraded: error-signature/flake triage
only, no culprit attribution).

*Status:* framework only. `prepare` today takes explicit build IDs and does not yet
branch on routine type; the config `routines:` map + per-type baseline/trigger logic
land with the selection-mode work (LPD-95845+).

### Default view per context (folded-in 95843 design)

Resolves T3.txt's open "what should the default view be?" — each entry context lands
on one of the modes above, **rendered clustered by default (§7)**:

| Context | Default mode | Baseline |
|---------|--------------|----------|
| **Release** — U / Patch / Quarterly (+ LTS) | `build-vs-build` | last **promoted** build (a U release compares against the last promoted) |
| **Product Team** — a specific routine | `routine-history` | across the routine's history |
| **Acceptance** | `routine-history` | across history |
| **Heavy-dev / PR** | `suite-vs-pr` | reference build (user-driven) |

Raw build-results browse stays available as a **power-user fallback** (T3
decommissioning note: build indices are no longer the default view, but remain
reachable) — the default is triage-with-verdicts, clustered. This design was folded
into **LPD-95842**; the views that render it are **LPD-95844+**.

### Triggering a run — `autoTriage`, manual selection, and where the report lands

§8 above says *what* gets compared. This says *who starts it* and *where the answer
shows up*. Settled 2026-08-17.

**Why not Testray's `autoanalyze`.** It already exists and already means something
else. `TestrayManagerImpl` checks it during results import and, when true, looks up
the routine's previous `importStatus eq 'DONE'` build and carries that build's prior
analysis forward onto matching results; `RoutineForm.tsx` renders it as a
user-facing "Autoanalyze" toggle, so it also has live user-set data behind it.
Overloading it with "run our classifier" would give one field two meanings and make
our behaviour depend on a Testray setting users change for unrelated reasons —
the exact failure §6 avoids for `Subtask`. We add **`autoTriage`** on our own object
instead.

**Two entry paths, one route.**

| `autoTriage` | Trigger | Baseline |
|---|---|---|
| **on** | the routine type's trigger (§8: build completion, or promotion for Release) | the routine type's baseline rule (§8) |
| **off** (default) | user picks both builds and hits Triage | whatever the user picked |

The manual path deliberately mirrors Testray's existing compare-runs affordance
(select A, select B, compare) so it needs no new interaction vocabulary: select
baseline build, select target build, **Triage** in the left-hand panel. Both paths
land on the same route — `triage/:baselineBuildId/:targetBuildId` — so an automatic
run and a manual one are the same object and the same view, differing only in who
supplied the build IDs.

**The Triage index is the landing page.** The sidebar item links to the view
with no build id, which used to render "No build selected" — a dead end, since
the only other way in was the build-list diamond, and the only way to reach a
report directly was to already know a build id. With no parameters the view now
lists every run instead: project and routine selects, then one row per triaged
build carrying the verdict columns.

Those columns mirror the build list's Passed/Failed/Blocked metrics, and like
the report they are **cluster** counts with row counts as fan-out — same reason,
and the reason the counts had to move to display labels (§12). The whole page is
one paginated `/o/c/triageruns` query plus three id-batched name lookups: every
number on it is already stored on the run row, so there is deliberately no
per-run fetch of `TriageResult`. That would be an N+1 growing with every run ever
recorded, to recompute figures we already have.

The report carries a **breadcrumb**, not a single back link: `Triage / <routine>
/ <build>`. There are two ways in — the index and the build-list diamond — and
one "back" cannot serve both. Deriving it from a `from=` parameter breaks the
moment someone shares or bookmarks the URL.

**Selection happens in the build list's right-click menu.** Settled 2026-08-20,
replacing an earlier dropdown-based picker on the index page. `useBuildActions`
already offers **Select Build A** / **Select Build B** for autofill and
compare-runs, so triage adds **Select Triage Baseline** / **Select Triage
Target** to the same menu with the same toast. It is the same kind of decision,
so it should not need a second interaction to learn — and it puts the choice on
the build list, where the builds actually are.

The pair then drives the Triage column: the chosen baseline shows a `BASELINE`
chip, the chosen target shows **Run Triage**, and clicking that queues the run.
Three distinct states, because a chosen baseline has nothing to act on and a
chosen target with no baseline is an incomplete pair — offering the button on
either would queue a run against an undefined baseline.

The selection lives in **its own localStorage-backed store**
(`useTriageSelection`), not in `TestrayContext`. Autofill and compare-runs live
in that context because they *are* Testray features; triage is ours, so keeping
it out preserves the "no triage logic in Testray core" boundary above. It is
persisted rather than in-memory because the two halves are separate interactions
— pick a baseline, page, maybe reload, pick a target — and an in-memory pair
empties under any of that.

**The queue contract.** *Run Triage* writes one `TriageRun` and nothing more:
`triageRunStatus: QUEUED`, `analysisMode`, and the three FKs (target build,
baseline build, routine). Everything else on the row — counts, clusters, the
status matrix — is filled in by the pipeline. So the row is a *request*, not a
result. Its ERC is derived from the pair (`queued-<baseline>-<target>`) rather
than random, so double-clicking upserts the same row instead of queueing the
work twice.

What consumes it: **`testray-analysis watch`** (`runner.py`), added 2026-08-20.
It polls for QUEUED rows, claims one by setting it RUNNING, and runs
**`scripts/triage_pipeline.sh`** — which is the single definition of the
pipeline sequence. Jenkins runs that same script, a human can run it by hand,
and the runner runs it; none of them re-implement the order of steps or where
the logs land.

The script is modelled on liferay-docker's release scripts (`lc_time_run`):
every step's output goes to its own numbered file,
`logs/log_<ts>_step_<n>_<name>.txt`, and the console gets one line per step with
its timing. Interleaved stdout from three long-running Python processes is not
legible in a Jenkins console; this is. On failure it prints the failing step's
last 20 lines *and* the log path, so an operator does not have to go fetch a
file to see the cause. Exit codes: 0 ok, 1 usage, 2 a step failed.

It echoes `BUNDLE=<absolute path>` on stdout after prepare. That is a deliberate
contract, not a side effect: the runner reads it to learn what it just built,
which is why prepare's own "Run bundle ready" line does not need scraping out of
a log file.

    testray-analysis watch                # prepare only, then hand over
    testray-analysis watch --classify     # prepare -> classify -> submit
    testray-analysis watch --once         # drain what is queued now, then exit

**`--classify` is opt-in, and that is the point.** Classification is the step
that costs minutes and real model usage, and a click in a browser is a very
small gesture to attach that to silently. Without the flag the runner does the
slow-but-free part — REST reads, the git diff, hunk filtering — and prints the
two commands that finish the job.

State transitions, which exist so the build-list diamond never lies:

| From | Event | To |
|---|---|---|
| QUEUED | runner claims it | RUNNING |
| RUNNING | pipeline succeeds | *row deleted* — `submit` wrote the real one |
| RUNNING | pipeline throws | FAILED + `errorMessage` |
| QUEUED | user clicks Abort | ABORTED |

The queued row is **deleted** on success rather than completed, because `submit`
writes its own row keyed by the *bundle* id. Leaving both would give one build
two runs, with the column free to show either.

**Abort applies to QUEUED only.** Once the runner has claimed a row it owns that
row's state, so offering abort on RUNNING would promise a cancellation nothing
can deliver. An aborted run is set to ABORTED rather than deleted — someone
asked and someone changed their mind, and a deleted row is indistinguishable
from never having clicked. It renders as a **grey** diamond, deliberately
outside the traffic-light set: a withdrawn request is not a failure to
investigate.

> **An unknown picklist key fails the write; an unknown *field* is silently
> dropped.** These are opposite behaviours and it matters which one you are
> relying on. Writing a field the Object does not have returns 200 and drops it,
> which is what lets the writer run ahead of the schema (§9). Writing
> `triageRunStatus: {key: "ABORTED"}` before that entry exists returns
> `400 ... "Object field name \"triageRunStatus\" is not mapped to a valid list
> type entry"`. So a new run state needs the picklist entry *first* — added to
> `triage-run-statuses.list-type-entries.json` for fresh installs, and by hand
> on a live instance, because `headless-admin-list-type` returns an empty
> collection for both the service account and the session.

**The report renders in-app.** Not a hosted artifact, not a link out. The verdicts
are already in Testray as `TriageResult` rows, so the view reads them through the
`CaseResult` FK and groups by `clusterKey` (§7); shipping an HTML file alongside
would be a second copy of data we already store, and would need hosting that does
not exist locally. `report.py`'s `report.html` stays what it is today — the **local
dev preview**, for inspecting a run without DXP. It is not the product surface.

**Build-index icon.** A column in the routine's build list, between **Build Status**
and **Execution Date** (`Routine.tsx` — between the `status` column and
`testrayBuildDueDate`). Testray's `status` column already uses a **circle** for
task/testflow status; a second circle in the neighbouring column would read as the
same vocabulary, so triage state uses a **diamond**. Starting shape only — the
mapping below is what matters, and the symbol is one string to change.

| Triage state | Colour | Clickable |
|---|---|---|
| no run for this build | *renders nothing* — same as the unpromoted star | — |
| in progress | `$blockedColor` | no |
| failed | `$failedColor` | yes → failure detail |
| ready | `$passedColor` | yes → the triage view |

Use the SCSS tokens, not hex, so the column tracks Testray's palette (same reason
`report.py` copied their values rather than inventing any). The empty state is
load-bearing: it keeps the column silent on routines that never triage, which is
what lets the column ship to everyone.

**The row data cannot carry this flag.** That list is populated by
`testray-builds-metrics`, hand-written SQL in `TestrayStatusMetricResourceImpl` —
Testray core, which decision #1 puts off-limits. So the column **side-fetches** and
merges client-side, with no core change.

> The query, as shipped and verified 2026-08-17 against real rows — scoped by
> **routine**, not by the build ids on the current page:
>
> ```
> GET /o/c/triageruns?sort=startedAt:desc
>     &filter=r_routineToTriageRuns_c_routineId eq '<routineId>'
> ```
>
> This is why `TriageRun` carries a routine FK as well as a build FK: one request
> covers every page of the build list, so paging costs nothing extra and the
> request count does not scale with page size. The per-page form
> (`r_buildToTriageRuns_c_buildId in ('<id>','<id>',…)`) also works and is the
> right shape anywhere the routine is not already known. Both were verified;
> `sort=startedAt:desc` lets the client keep the newest run per build by taking
> the first one seen.
>
> **The FK values must be quoted strings**, even though the column is `bigint`.
> Unquoted (`eq 36418`, `in (36418)`) returns `400 InvalidFilterException:
> Incompatible types` — as does `buildToTriageRuns/id eq …`. There is also no
> nested `/o/c/builds/{id}/triageRuns` endpoint (404), so per-row fetching is not
> an option even as a fallback. `TriageResult` is reachable the same way via its
> ERC prefix — `startswith(externalReferenceCode,'<buildB>_')`, which the writer
> already documents — so no `TriageRun`→`TriageResult` relationship is needed.

> **The view's read path — one query, not N+1.** Verified 2026-08-19 against the
> local instance. The triage table needs eleven columns, and only some of them
> live on `TriageResult`: status-in-B, the error text and the component/team FKs
> are on the joined `CaseResult`, and the test *name* is one hop further out on
> `Case`. Three facts make that one request:
>
> * `nestedFields=caseResultToTriageResults` expands the `CaseResult` inline.
> * **`nestedFieldsDepth=2` reaches through it to `Case`**, which is the only
>   place the test name exists. Only `case` and `creator` expand at depth 2 —
>   `component` and `team` do **not**, so those two are still resolved by id.
> * `fields=<projection>` trims the row to what the view reads (402 KiB → 135
>   KiB on a 183-row run). It does **not** reach inside an expansion, so the
>   expansion is what the payload costs: 1.1 MB and ~0.4s for that same run.
>
> Two traps. The expanded object lands under a **different key depending on the
> query** — `caseResultToTriageResults` with a `fields=` projection,
> `r_caseResultToTriageResults_c_caseResult` without one — so read whichever is
> present rather than coupling the reader to the projection. And every number
> arrives as a **string**: `id`, `baselineSignatureCount` and every `…Id` field
> serialise as text, so an unguarded `row.count > 5` compares strings.
>
> **Testray persists its entire SWR cache across reloads.**
> `SWRCacheProvider` serialises every cache entry to storage on `beforeunload`
> and restores it on boot, so a write made from our side is invisible not just
> until the next revalidation but *through an F5* — the restored cache serves
> the pre-write answer. This cost real debugging time: after *Run Triage*, the
> column went blank instead of amber and stayed blank after a reload, which
> looks exactly like a write that silently failed. The row was there the whole
> time. Anything that creates or changes a `TriageRun` must
> `mutate(triageRunsKey(routineId))`; the key is exported from `useTriageRuns`
> for that reason.
>
> **A session-authenticated read without `x-csrf-token` lies about why it
> failed.** From the browser, `/o/c/builds/270748` returns **404** and
> `/o/c/builds` returns `totalCount: 0` when the header is missing — both of
> which read as "this object is not readable by this user", and neither is true.
> Send `x-csrf-token: Liferay.authToken` (the CX fetcher always does) and the
> same two requests return the build and the full collection. Testray's own
> `/o/testray-rest/v1.0/...` app behaves the same way, returning 403. Do not
> conclude anything about permissions from a bare `fetch`.
>
> **`id` follows the same quoting rule as the relationship FKs.** `id eq 282268`
> fails with `Incompatible types`; `id in ('282268')` works. There is no `eq`
> form that accepts an unquoted primary key, so even single-id lookups go
> through `in`. Batch those filters — a ~550-row run would push one `in (…)`
> clause past the container's header limit and fail as an opaque 400.
>
> Net: the view loads in **6 requests** — run, results (the expanded query),
> routine (for the project id the deep-links need, which `TriageRun` does not
> store), teams-by-id, components-by-id, and builds-by-id for the two build
> names in the title. `TriageRun` stores neither build name; it does not need
> to, because `/o/c/builds` is readable with the CSRF header above.

**CX boundary — what ships where.**

| Piece | Where it lives | Deployable on its own? |
|---|---|---|
| `TriageResult`, `TriageRun`, `TriageRoutineSetting`; OAuth app | `liferay-testray-analytics-site-initializer` | yes |
| Triage view, cluster render, baseline/target picker, `autoTriage` settings screen | **`liferay-testray-analytics-custom-element`** (new) | yes |
| 'Triage' sidebar item + build-index column | `liferay-testray-custom-element` (~40 lines) | **no** |

The last row is a source modification to Testray's own custom element, and that is
forced, not chosen. Verified 2026-08-17: the app is a single
`customElements.define(ELEMENT_ID, Testray)` with its own React root and its own
internal router, it exposes no plugin registry or extension point, and `ListView`'s
`columnsContext` is a **visibility map** over the static `tableProps.columns` literal
(`columns[key] === undefined → true`) — it can hide a column but cannot add one. A
second custom element mounts on a different DOM node and cannot reach in.

What keeps this inside the additive principle: the modification contains **no triage
logic**. It is two link-shaped additions whose render feature-detects
`/o/c/triageruns` and draws nothing when the probe fails — so on a stock instance
without our CX, the patch is inert. That preserves the deploy story: *don't apply the
hook* → Testray unchanged; *apply it and deploy our CX* → the whole feature. It is
also small enough and generic enough to offer upstream as an integration point
rather than carry as a permanent local diff.

> Rejected: making our custom element a standalone page instead, to reach literal
> zero edits. Our site-initializer provisions its **own site**
> (`siteName: Liferay Testray Analytics`), so that page sits in a different URL
> space from `/web/testray` and the flow becomes leave-Testray-and-come-back.
> Also rejected: DOM-injecting into their React tree from outside.

---

## 9. The `TriageResult` Object (integration contract)

Deployed via our own **`liferay-testray-analytics-site-initializer`** CX (the
name this shipped under; earlier drafts of this doc called it
`liferay-triage-site-initializer`). It is a *new* Object; defining it
auto-creates its backing table and a `/o/c/triageresults` REST endpoint.

- **Relationship:** `oneToMany` from Testray's `CaseResult` → our `TriageResult`.
  The FK (`r_caseResultToTriageResults_c_caseResultId` — Liferay derives the
  name from the relationship, so the plural is load-bearing) lives on **our**
  `TriageResult` table; Testray's `CaseResult` definition is not edited.
  `deletionType: cascade`.
- **Idempotency key (logical):** `(caseResult, classifier)` — re-running a
  classifier overwrites rather than duplicates. Realized as
  `externalReferenceCode = <buildB>_<caseId>_<classifier>`; see open-Q #1 for
  why the write is a PUT-by-ERC rather than the bulk batch endpoint.
- **No `buildId` field.** A build's verdicts are retrieved either by ERC prefix
  (`startswith(externalReferenceCode,'<buildB>_')`) or through the `CaseResult`
  side of the relationship.
- **OAuth scope.** The CX's headless-server app carries
  `c_triageresult.everything`. Only that plain form resolves — `.read` /
  `.write` leaves are accepted in the yaml and then **dropped silently at
  deploy**, surfacing as a 403 at call time rather than a build error. Verify
  what was actually granted by reading the token's `scope` claim.

**Fields (as deployed):**

| Field | Type | Notes |
|-------|------|-------|
| `classification` | Picklist | `BUG / POSSIBLE_BUG / NEEDS_REVIEW / TEST_FIX / FALSE_POSITIVE` |
| `confidence` | Picklist/String | `high / medium / low` |
| `culpritFile` | String | Required for `BUG`, expected for `POSSIBLE_BUG`, else null |
| `specificChange` | String | e.g. `Foo.java:42 removed null check` |
| `suspiciousCommits` | String/Clob | Candidate commits/tickets from `git log A..B` (§2). Broader evidence than `culpritFile` — present even for `NEEDS_REVIEW`, where `culpritFile` is null. Distinct grain: commit/ticket-level, not the single file. |
| `reason` | Clob | Rationale |
| `classifier` | String | `api:claude-opus-4-7`, `agent:…`, `human` |
| `clusterKey` | String (indexed) | Our grouping key (§7) |
| `analysisMode` | Picklist/String | `build-vs-build / routine-history / suite-vs-pr` (§8) |
| `gitHashA` | String | Baseline build hash |
| `gitHashB` | String | Target build hash |

> `human`-classifier rows are how override capture (LPD-95851) is stored —
> separate from the AI verdict, per the vision's "keep verdict and human feedback
> separate for measurement/fine-tuning."

> **`classifier` is model-version-pinned.** The label embeds the model
> (`api:claude-opus-4-7`), set in `config.yml` (`triage.classifier.api.model` +
> `effort`) and overridable per run with `--classifier`. Consequence for the
> idempotency key (decision #9): a **model upgrade creates new rows** per
> `CaseResult` rather than superseding the old ones — deliberate, so versions stay
> comparable for measurement. So "overwrite-on-rerun" (open-Q #1) means *same
> classifier label*, **not** same model family.

> **Ticket linkage is not stored here.** The Jira ticket lives on native
> `CaseResult.issues` (§11), read via the `CaseResult`→`TriageResult` join. This
> keeps Testray the source of truth and avoids a duplicate store.

> **Write inclusion policy — what actually gets persisted.** The writer
> (`testray_writer.py`) persists *actionable* verdicts and, by default,
> **excludes** two non-actionable,
> re-derivable classes: **high-confidence `FALSE_POSITIVE`** (a confident
> not-a-failure) and the **auto/env buckets `DID_NOT_RUN` / `ENV_FAILURE`**
> (env/infra pre-classification — never diff-analyzed, since a build failure or
> env issue has no failure-vs-diff relationship to evaluate). Both are toggleable,
> and run summaries still **count** them (the "auto-dismissed / env-noise volume"
> KPI is preserved). `DID_NOT_RUN` = `BUILD_FAILURE`/`NO_ERROR`; `ENV_FAILURE` =
> `ENV_*` — human-readable relabels of the old `AUTO_CLASSIFIED` umbrella (they're
> auto-buckets, not LLM verdicts, so they sit outside the §4 taxonomy). Keeps the
> store lean (T3 retention) and the triage view focused on code regressions.

### Companion objects — `TriageRun` and `TriageRoutineSetting`

Same CX, same rules as `TriageResult`: new Objects, no edits to Testray's
definitions, FKs on our side.

**`TriageRun`** — one row per triage run. `TriageResult` answers *what the verdict
was*; this answers *whether a run exists at all, and how it went*, which
`TriageResult` cannot: a run that failed or is still going has no results yet.

- **Fields:** target build FK, baseline build FK, routine FK, `analysisMode` (§8),
  `triageRunStatus` (`QUEUED` / `RUNNING` / `DONE` / `FAILED`), `startedAt`,
  `finishedAt`, `classifier`, `errorMessage`, and counts. It is not plain `status`
  because **`status` is a reserved Objects field name** — Liferay keeps it for
  workflow state and rejects the definition outright
  (`ObjectFieldNameException$MustNotBeReserved`). The qualified form also matches
  Testray's own convention (`testrayBuildTaskStatus`, `testrayBuildPromoted`).
- **Counts are split deliberately.** Five stable aggregates as sortable `Integer`
  columns (`totalFailures`, `totalClusters`, `totalClassified`, `totalWritten`,
  `totalExcluded`) plus a `verdictCounts` **Clob of JSON** for the per-verdict
  breakdown. One `Integer` per verdict was rejected: the §4 taxonomy has already
  churned once (`AUTO_CLASSIFIED` split into `DID_NOT_RUN` + `ENV_FAILURE`), and a
  field added after the Object exists lands in `_x`. The aggregates are the ones
  worth filtering and sorting on and they do not move; the breakdown is display
  data, so it costs nothing to keep it schemaless.
- **Routine FK is not redundant with the build FK.** `routine-history` runs (§8,
  LPD-95846) span a window rather than a build pair, so they have a routine but no
  single target build. That is also what keeps the icon column honest: it lights up
  only for runs that *have* a target build.
- **Deletion types differ per FK, on purpose.** `buildToTriageRuns` and
  `routineToTriageRuns` are `cascade` — deleting the target build or the routine
  should take its runs with it. `baselineBuildToTriageRuns` is **`disassociate`**:
  an old baseline aging out must not delete a newer run that merely referenced it.
- **ERC:** the CLI's run id (the `runs/r_<id>/` bundle name), so a local bundle and
  its Testray row share one identity.
- **Why it earns its place:** it is the icon's predicate. With it, the build-index
  column resolves in one filtered query per page; without it we would count
  `TriageResult` rows by ERC prefix — a query per row — and still could not
  distinguish *in progress* or *failed* from *nothing here*. It also gives the manual
  flow its progress state and the routine its run history.

**`TriageRoutineSetting`** — one row per routine, holding `autoTriage` (boolean,
default off), `baselineStrategy`, and `classificationMode` (`per-test` /
`by-cluster`, §7). ERC = the routine id. **An absent row means defaults**, so no
backfill is needed and an unconfigured routine behaves as `autoTriage` off with the
strategy inferred from its name (§8).

> **Declare both objects complete at creation.** Fields and relationships present
> when the Object is created land on the base table; anything added afterwards lands
> in the `_x` extension table. That split is what produced the
> `bx.cpuusetime_ does not exist` and `bs.caseresulttotal_ does not exist` failures
> in the local setup, and it stays invisible until some hand-written SQL reads the
> wrong table. Adding a field later is not free — plan the shape up front.
>
> Measured 2026-08-17, and it is good news for how these files are laid out: a
> site-initializer pass counts as "at creation" even when relationships live in
> separate files. `o_..._triageresult` carries all 11 fields *and*
> `r_caseresulttotriageresults_c_caseresultid` on the **base** table, with an empty
> `_x` (1 column). So splitting definitions and relationships across files is safe;
> what is not safe is adding to an Object that already exists in a deployed
> instance — which now includes `TriageResult`.

> **Scopes:** each new Object needs its own entry on the CX's
> `oAuthApplicationHeadlessServer` (`c_triagerun.everything`,
> `c_triageroutinesetting.everything`). Only the plain `.everything` alias resolves;
> `.read` / `.write` leaves are dropped silently at deploy with no build error and
> surface as a 403 at call time, so confirm what landed by reading the token's
> `scope` claim rather than trusting the YAML.

**Deployed and verified 2026-08-17** against the local instance. Both Objects, all
four relationships, and three new picklists (`Triage Run Statuses`,
`Triage Baseline Strategies`, `Triage Classification Modes`) land from a redeploy of
the existing CX — a site initializer **does** re-run `_addObjectDefinitions` for an
already-initialized site, so iterating on these files does not need a rebuild. All
four FK columns are on the base tables and both `_x` tables are empty.

The write path was exercised end-to-end against `/o/c/triageruns`:
`PUT .../by-external-reference-code/{erc}` created a row, the picklist was accepted
in `{"key": …}` form, `startedAt` round-tripped as UTC, **all three FKs persisted —
including both Build relationships, which is the part that could have collided** —
a second PUT updated in place with `totalCount` unchanged (idempotent, matching
open-Q #1), and the scratch row was deleted afterwards (0 rows remain). Two
authoring
constraints cost a deploy cycle each and are worth knowing before adding fields:
`status` is reserved (above), and a `DateTime` field is rejected without an explicit
`objectFieldSettings` entry `timeStorage` — we use `convertToUTC`, since a run
timestamp is an absolute moment and Testray spans timezones.

> Failures here are **fatal to the whole pass**, not per-file: the first bad field
> aborted initialization and *neither* Object was created, including the valid one.
> So a redeploy that silently changes nothing usually means an exception, not a
> no-op — check `docker logs` for `BundleSiteInitializer` before assuming the
> watcher missed the artifact.
>
> The other reason a redeploy changes nothing: **the initializer skips Objects that
> already exist.** It creates, it does not reconcile — a renamed or added field in
> the JSON is silently ignored, with no error and nothing written to `_x`. Harmless,
> but it means the JSON and the live schema can drift apart without any signal. To
> apply a change to an existing Object, delete it (Control Panel → Objects) and
> redeploy; safe while row counts are zero, a migration once they are not.

---

## 10. Build sequence (follows the tickets)

1. **LPD-95842 (foundational):** this doc (incl. default-view-per-context design,
   §8); repo scaffold; extract `apps/triage` as an api-only, CLI-first tool with the
   Postgres write removed.
2. **`TriageResult` Object (LPD-95843)** in a new
   `liferay-testray-analytics-site-initializer` CX (§9) — the contract
   everything hangs off.
3. **Headless write-sink (LPD-95843)** — replace the Postgres upsert with
   `PUT /o/c/triageresults/by-external-reference-code/{erc}` per row (open-Q
   #1); register one OAuth2 service-account app.
4. **Frontend (LPD-95844 + LPD-95848)** — three pieces, in this order:
   a. `TriageRun` + `TriageRoutineSetting` Objects in the existing
      site-initializer CX (§9), declared complete at creation.
   b. **`liferay-testray-analytics-custom-element`** (new CX) — cluster view
      grouping by `clusterKey`, baseline/target picker, `autoTriage` settings.
   c. The **hook** in `liferay-testray-custom-element` — 'Triage' sidebar item and
      the build-index diamond column, feature-detected so it is inert without (b).
   Order matters: (c) probes for (a), and is the only piece that touches Testray.
5. **Later modes & features** — `routine-history` / `suite-vs-pr`; inline + bulk
   ticket creation (LPD-95849/96615) via extended `etc-jira`, with dedup +
   write-back to native `issues` (§11); inline fix (LPD-95850); override capture
   (LPD-95851).
6. **Jenkins job** — last, once read→classify→write works against local DXP.

---

## 11. Jira / issue integration

Ticket creation (LPD-95849 inline, LPD-96615 bulk) + dedup, layered on Testray's
existing Jira plumbing. Testray-native `issues` stays the source of truth; the
module automates the manual edit a user does today.

We write the ticket ref to Testray's native **`issues`** free-text field (on
`CaseResult`) — the field users type into today — and do **not** use the richer
synced **`JiraIssue`** Object. Why `issues`: it unifies manual + automated tickets in
one place, keeps Testray canonical, needs no schema of ours, and the join
`TriageResult ↔ CaseResult.issues ↔ Jira` yields labeled outcome data for free.

### Division of labor

- **Jenkins tool (scripts):** dedup **read only** — computes `existingIssue` per
  cluster. **Never creates or writes tickets.**
- **Frontend CX:** the create action + the write-back to native `issues`. This is
  where "take the user's role" happens.
- **`etc-jira` (extended):** performs the actual Jira `POST` to create the issue —
  the one scoped exception to §1 (it's a client extension, not core Testray).

### Credentials & audit

`etc-jira` authenticates to Jira via **per-user 3-legged OAuth** — each user
authorizes their own Jira access and their token is stored per-user in Testray
(`JiraAuth.authorize(userId)` / `getOAuthJira(userId)`). The registered Atlassian
*app* holds a `client_id`/`secret`, but the token that files the ticket is the
**acting user's**, so an inline-created ticket is attributed to the real person —
clean audit, no shared service account.

**This is why headless/bulk creation is a real decision, not a scaling detail**
(open-Q #8): a Jenkins/headless path has no user session → no per-user token → it
would need a **service account or a nominated user**, which both (a) reverses §1's
"writes happen in the CX as a user action, never headless" invariant and (b) changes
the audit trail (tickets no longer attributed to a person). Route it through the
decision-#12 lens, not as an implementation choice.

> **Routine-gated creation.** Stable does not need tickets to be created in favor of fix velocity. The **CX routine-gates the "Create ticket"
> availability** and is hidden/disabled for Stable. Enforced in the CX, not the tool.

### Create flow (hybrid UX)

1. Cluster view shows a verdict. If `existingIssue` is set → "Tracked by LPD-XXXX"
   (link), no create button.
2. Else "Create ticket" → an **in-Testray modal** prefilled from the verdict
   (title/description from `culpritFile` + `reason` + affected tests + build A/B
   links).
3. User edits/confirms in the modal → CX calls extended `etc-jira` → Jira `POST`
   → returns the new key.
4. CX writes the key to **native `CaseResult.issues`** via REST — for **every
   `CaseResult` in the cluster** (one root cause = one ticket across all its
   failures).
5. Toast: "LPD-XXXX created." The next run's dedup finds it.

The hybrid (in-Testray modal + headless create) is chosen because only the
headless-create side lets the key be written back automatically — a
browser-draft-only flow can't close the loop.

**Bulk & auto-create (LPD-96615) — see open-Q #8.** Two modes: **(A)** manual
multi-select of clusters → bulk create (optionally merged under one ticket; still a
CX user action), and **(B)** per-team **auto-create** — which needs a service
account / nominated user and must respect dedup + the Stable no-ticket gate (#16).

### Dedup contract

- **Grain:** `clusterKey` — a ticket tracks a root cause, not one failure instance.
- **Scope:** across history, not just the current build pair — a recurring error
  already ticketed is not re-filed.
- **Sources (union):** native `CaseResult.issues` on every `CaseResult` sharing the
  `clusterKey`. Because we read the native field, **manually-added tickets suppress
  duplicates too** — the point of Testray staying the source of truth.
- **Output:** the tool sets `existingIssue` on the cluster; the CX renders
  link-vs-create accordingly.

---

## 12. Transition detection & override capture

### Transition matrix — what we surface

COMPUTE detects **new *or changed* failures**, not only PASSED→FAILED — a baseline
that already had failures otherwise makes PASSED→FAILED silently undercount real
regressions. Baseline (row) → target (column); ✅ = triage candidate:

| baseline ↓ / target → | FAILED | BLOCKED |
|---|---|---|
| **PASSED** | ✅ triage | (existing PASSED→BLOCKED/UNTESTED handling) |
| **FAILED** | ✅ triage — *only if the error signature changed* | ⚠️ awareness only |
| **BLOCKED** | ✅ triage | — |
| **`TESTFIX`** (status) | ✅ triage (best-effort) | — |

- **`FAILED→PASSED` is excluded** — a "what got fixed" signal for a future Insights
  view, not triage.
- **`FAILED→BLOCKED` is awareness, not a triage candidate** — blocked is usually
  infra/dependency, not a product regression; it must not dilute the BUG-hunting
  view.
- **`TESTFIX→FAILED`** = a result a user had marked `status=TESTFIX` is failing
  again. Best-effort, since the status is applied inconsistently. (Note the naming
  trap from decision #14: this `TESTFIX` is a Testray *status*, not our `TEST_FIX`
  *verdict*.)

### The "changed error signature" condition

For same-state `FAILED→FAILED`, the transition alone isn't the signal — the *error
changing* is. This reuses the **`clusterKey` normalization** (§7 / open-Q #5): a row
is surfaced when `normalize(baseline.error) != normalize(target.error)`. One
normalization function, two uses (clustering + changed-failure detection). Signal
quality is hostage to that normalization, so `FAILED→FAILED` is only trustworthy
once it's decent — flag volume until then.

Two script consequences:
- `compute_test_diff` must **retain the baseline error message** (empty for
  PASSED→FAILED; both sides needed for FAILED→FAILED) and tag each row's transition
  type in `diff_list.csv`.
- For changed failures the **prompt carries both baseline and target errors** ("was
  failing with X, now Y") — the reasoning is about the delta — and the rubric gets a
  note that the "baseline was clean" assumption does not hold for these rows.

### Baseline signature prevalence — the strongest cheap filter

Added 2026-08-18. Every triage row carries **`baseline_signature_count`**: how
many times its normalized error signature already occurred among the *baseline
build's own failures*. Computed in `prepare` by
`baseline_signature_prevalence()` over the case results already fetched, so it
costs no extra I/O, and persisted to `diff_list.csv`.

The pipeline previously judged a case purely on its own status pair and never
asked whether the error it now shows is one the baseline was already producing
dozens of times on other tests. That question is what separates a new failure
mode from standing suite noise.

**Measured on the 2026.q1.11 → q1.12-lts pair (349 classifiable rows):**

| baseline occurrences | rows | disposition |
|---|---|---|
| 0 — never seen | 130 | review |
| 1–4 — rare | 33 | deprioritise |
| 5+ — already chronic | **186** | drop |

53% of the classify workload was failing in a way the baseline already produced
at least five times; the worst signature occurred **819** times. It also sharpens
the top of the report: of three `POSSIBLE_BUG` clusters on that run, two were
novel and one had six prior baseline occurrences — a materially weaker
regression candidate, and nothing in the report could say so before.

Unlike the transition label, this signal behaves **identically on `new` and
`changed` rows**, which is what makes it safe to rank and filter on.

**Not yet used to exclude rows pre-classification.** Doing so would cut the
classify workload roughly in half, on the same mechanism as the CI-batch
exclusion (§7), but it is a bigger judgement — a chronic signature can still
hide a new cause — so today it only informs ranking.

### Never filter on the transition label

`changed` (FAILED → FAILED, signature differs) looks like the obvious thing to
drop: a test already failing in the baseline cannot be a regression from this
range. **That reasoning is wrong and it destroys real defects.** A test can be
broken for one reason before and a *different* reason after; the second reason
is this range's regression wearing a pre-existing failure as camouflage. On the
q1.12 run, dropping all 48 `changed` rows produced a 37% undercount, and 27 of
them were failing in a way the baseline never managed.

Concretely: `AutoTranslationAzure#CanTranslateWCFieldIndependently` failed in
both builds — a wording nit in q1.11, the feature not translating at all in
q1.12. Same transition label, entirely different severity.

Transition earns its place **on** the report — a `changed` row is unreadable
without its baseline error printed beside its target error — but it must never
sort the list and must never remove rows from it. Rank on
`baseline_signature_count` instead. This is decision #13 restated: surfacing
only PASSED→FAILED silently undercounts whenever the baseline already had
failures, which on a real acceptance build is most of the time.

### View contract — rules both renderers must obey

`report.py` and the CX view (`liferay-testray-analytics-custom-element`) render
the same data and have already drifted apart once. These are the decisions that
are **not** obvious from reading either implementation, and each one exists
because the naive version was tried and was wrong.

1. **Lead with clusters; cases are fan-out.** The tool has already clustered, so
   the case count is the wrong headline unit. "6 possible bugs" was three
   defects each counted twice; "153 need review" was 100 clusters, one holding
   23 cases. `3 defects, 11 to review` is a tractable morning; `6 bugs, 153 to
   review` reads like a crisis and gets the report ignored.
2. **`NOT_ATTRIBUTABLE` is a display label, never a stored verdict.** A
   `NEEDS_REVIEW` at `low` confidence is the classifier saying "I could not
   attribute this", not "a human must review this". Rendering it as a review
   queue misrepresents what was said. The stored classification stays
   `NEEDS_REVIEW`, so §4, the picklist, the writer and the rubric are untouched.
3. **Severity order is** BUG → POSSIBLE_BUG → NEEDS_REVIEW → TEST_FIX →
   NOT_ATTRIBUTABLE → FALSE_POSITIVE → ENV_FAILURE → DID_NOT_RUN. Verdict and
   confidence are **ordinal**: they must sort by rank, not alphabetically, or
   `DID_NOT_RUN` sorts above `BUG`. `report.py` does this with a `data-sort`
   attribute the generic sorter prefers over cell text.
4. **Collapsed by default.** The cluster list is the overview; expanding every
   member on load buries it under hundreds of rows.
5. **Collapsing a repeated cell is MODE-dependent, not row-dependent.** A member
   may show "↑" instead of its reasoning only while the visible group *is* its
   cluster. Under group-by team/component/verdict the header reports "N distinct
   reasons" and the arrow would point at a cluster that is not on screen. The
   obvious implementation — decide at render time — is the broken one; the cell
   must carry both forms and let the active mode choose.
6. **Sorting reorders clusters, not just members within them.** Sorting only
   within clusters leaves the cluster order fixed, so clicking a header appears
   to do nothing.
7. **A culprit file alone is not actionable.** Show the ticket and commit that
   touched it (`submit` attaches `culprit_commits`); the file says *where*, the
   ticket says *who changed it and why*.
8. **Say what a number is out of.** "Total tests: 557" read as tests run; the
   build ran 17,537. The header states tests-in-build, failures-triaged and
   clusters, and the A×B status matrix carries the rest.
9. **Say what the table is missing.** The two renderers do not see the same rows.
   `report.py` renders the dataframe — every triaged failure — while the CX
   renders what was *written*, and the write policy skips high-confidence
   `FALSE_POSITIVE` and pre-classified rows. On the reference run that is 183 of
   557. A table of 183 with no denominator reads as "this build had 183
   failures", wrong by a factor of three, so the CX shows `Shown`,
   `Failures triaged` and `Not written` side by side and marks clusters
   `117 of 177 triaged`.

10. **Say how much of the build the comparison could see.** The diff is an
   INNER join on case id, so a case that ran on only one side is invisible to
   it. When two builds' suites were re-selected between them the join can be
   almost empty, and a short verdict list then reads as a healthy build. Both
   renderers show `Compared: <n> (<pct>)` beside `Tests in build`, and below 50%
   they say so outright. Found the hard way: a q1.29 → Q1.30 pair shared **54 of
   3,579** case results (1.5%) and reported 5 clusters as though that were the
   whole build.

**Status: both renderers obey all ten rules as of 2026-08-20.** The CX port
landed the four it previously did not — collapsed-by-default, group-reordering
sort, mode-dependent collapse and the denominators above. Rule 10 arrived later,
from a run that exposed it. Two notes for whoever
touches either next:

* Rule 5 is *cheaper* in the CX than in the artifact. `report.py` must emit both
  the `↑` pointer and the full value and let `table[data-mode]` CSS choose,
  because its group-by changes after render. In the CX the mode is React state,
  so the choice is made directly at render. Verified: 733 pointers under
  group-by-cluster, 0 under group-by-team.
* Rule 4 has a corollary the artifact learned first and the CX now copies: **an
  active filter overrides the collapse.** Searching a fully collapsed report
  must reveal its hits, or the search returns nothing and reads as "no matches".

### The verdict vocabulary lives in one place

`verdicts.py` is the single definition of the severity order, the
`NOT_ATTRIBUTABLE` relabel rule, the cluster rollup and the flattened-key
canonicalisation. `report.py` imports it; `submit.py` and `testray_writer.py`
import it; the custom element's `util/verdict.ts` mirrors it by hand. Change one
and change the mirror.

This exists because three consumers restated the rule and two of them drifted.
`TriageRun.verdictCounts` / `verdictClusterCounts` were counted from the raw
`classification`, while the report displayed `display_verdict`. The Testray
index reads those blobs straight onto a column, so it would have reported **100
`NEEDS_REVIEW` clusters for a run whose report showed 11** — the other 89 being
`NOT_ATTRIBUTABLE`. Both numbers were "right"; they just answered different
questions, and no reader could tell which. Fixed 2026-08-20: both blobs now
store display labels, verified against run `r_20260818T200045Z` where the
recomputed counts reproduce the rendered report exactly across all five
verdicts (3/6 POSSIBLE_BUG, 11/13 NEEDS_REVIEW, 9/19 TEST_FIX, 89/140
NOT_ATTRIBUTABLE, 5/5 FALSE_POSITIVE).

Two consequences worth knowing:

* **The blobs are schemaless on purpose**, so adding `NOT_ATTRIBUTABLE` as a key
  needed no Object change — but an existing `TriageRun` row keeps its old counts
  until `submit` is re-run for that bundle. Re-running `submit` is cheap: it
  writes from the bundle's existing `results.json` and calls no model.
* **Verdicts read back *out of* Testray arrive flattened** (`NEEDSREVIEW`, not
  `NEEDS_REVIEW`) because Liferay strips underscores from picklist keys. Anything
  reading them must canonicalise first or the relabel rule silently never
  matches — which is exactly how a verification pass produced zeroes for every
  verdict while the totals looked correct.

### The `transition` vocabulary — one string, two spellings

`prepare` emits lowercase tokens (`TRANSITION_CHANGED == "changed"`); the §12
fixtures use `CHANGED_FAILURE`. Both reach the renderers, and matching only one
is not a cosmetic bug: `report.py`'s changed-failure guard compared against
`"CHANGED_FAILURE"`, a value `compute_test_diff` never produces, so the
"already failing on the baseline" warning **never rendered on real data** — the
single omission this section calls the most misleading thing the report could
make. It passed its test because the test supplied the uppercase form.

Fixed 2026-08-19 in both renderers by accepting either spelling, with a
regression test that asserts the guard fires on the value `prepare` actually
emits. When comparing a transition, normalise; never compare to a literal.

### Override capture (LPD-95851)

Users validate/correct the AI verdict. The old static `report.html` had no way to do
this; the `TriageResult` Object + REST write path is what unlocks it:

- AI writes `classifier=api:…` — e.g. `POSSIBLE_BUG`.
- User disagrees → the **CX writes a second row on the same `CaseResult`**:
  `classifier=human`, e.g. `TEST_FIX`. Keyed by `(caseResult, classifier)`, so the
  **AI row is never overwritten** — both persist (T3.txt: "keep verdict and human
  feedback separate for measurement and fine-tuning").
- **Display rollup:** a `human` row wins over the AI row when both exist.
- New work is only the CX affordance (a verdict control that POSTs the human row)
  and the rollup rule — the data model (decision #9 multiplicity) already supports
  it.
- **The human-wins rollup applies to training extraction, not just display.**
  `culpritFile` feeds the future NN model via `pr_outcomes` (it is **not** a current
  scoring input). So when a human overturns an AI `BUG`, the `pr_outcomes` pull must
  treat the human verdict as ground truth — the overturned BUG's `culpritFile` must
  **not** be fed as a confirmed defect label, or it poisons training. (The RAP pull
  is naive today — `WHERE classification IN ('BUG','POSSIBLE_BUG')` — and would need
  to become override-aware.)

---

## 13. Open questions

Reviewed 2026-07-08 — nearly all resolved. Labels are **literal `#N`** (they're
cross-referenced elsewhere in the doc, so they stay stable regardless of order).

**Resolved**

- **#1 Write idempotency + retry** — per-row idempotent upsert via an
  `externalReferenceCode` = `(buildB, caseId, classifier)`; a rerun overwrites by
  ERC. **Implemented as one `PUT .../by-external-reference-code/{erc}` per row,
  not the bulk batch endpoint** (LPD-95843): `POST /o/c/triageresults/batch`
  *creates* rather than upserts and returns an async job, which loses exactly the
  idempotency this decision requires. Fanning out PUTs also gives per-row failure
  isolation — one bad row is collected and reported, the rest still land. Not a
  cost concern: one row per *classified failure* (the diff set — hundreds), not
  per test (~20k reads); the write is negligible next to the Anthropic
  classification. Verified live 2026-08-11: rerunning the same (build pair,
  classifier) left `totalCount` unchanged.
- **#2 Read DTO** — sufficient; the custom `TestrayCaseResult` DTO exposes every
  diffed field (+ team/routine/run). No raw-endpoint fallback.
- **#3 Portal checkout** — persistent checkout, `git pull` each run (§2); never a
  fresh clone (multi-GB).
- **#4 Triggers** — routine-dependent: Stable + Acceptance on build completion;
  Release on promotion; PR user-driven (§8). Gated per routine by our own
  **`autoTriage`** flag (default off), *not* Testray's `autoanalyze`, which is a
  carry-forward flag with unrelated live semantics. When off, the user selects
  baseline and target manually — same route, same object (§8).
- **#5 `clusterKey` normalization** — strip volatile tokens (timestamps,
  `0x…`/hashes, UUIDs, line numbers, temp paths, ports, `…ms`, build IDs), keep
  exception class + first message line + top ~3 normalized frames, lowercase, hash.
  Conservative v1; iterate under #9.
- **#6 Insights pull cadence** — deferred (later); likely a **weekly weekend** pull.
- **#7 Provenance** — a Jira label **`testray-suggested`** applied at creation via
  `etc-jira`. Provenance lives in Jira (queryable), manual tickets lack it, and
  Testray carries **no extra field**. (Acting user already captured — per-user OAuth.)
- **#9 `clusterKey` versioning** — version tag **embedded in the key string**
  (`v<N>:<sha256-16>`), not a second field, so key and version cannot drift and no
  post-initialization object-field is needed (late-added Objects fields land in
  the `_x` extension table). Raw signature inputs are **not
  copied** onto `TriageResult`: they are recomputed through the `CaseResult` FK,
  which `cascade` guarantees outlives nothing. On a bump — incrementing
  `CLUSTER_KEY_VERSION` whenever `normalize()` changes behaviour — an explicit
  eager `--recompute-cluster-keys` pass re-keys the **~90-day retention window**
  only (T3's retention bounds the cost; older rows age out). Dedup compares within
  a version, and startup warns on stale-version keys. Full rationale in §7.
- **#10 `NEEDS_REVIEW`** — no second-pass (token cost); confidence-gated rubric +
  the `POSSIBLE_BUG`/`TEST_FIX` tiers limit bloat; monitor the rate; tune after
  analyzing overrides (§4).

**Deferred to build time**

- **#8 Bulk creation (LPD-96615)** — two modes approved: **(A)** manual
  **multi-select** → bulk create, incl. merging several selected clusters under
  **one** ticket (CX user action, §1 intact); **(B)** per-team **auto-create**.
  B's mechanics — the **service account / nominated user**, the §1 no-headless-write
  reversal, and respecting dedup + the Stable no-ticket gate (#16) — are finalized
  when bulk is actually built (decision-#12 lens).

---

## 14. Local dev & test loop

**No Jenkins for the PoC.** The tool is CLI-first (decision #10) and Jenkins only
*wraps* the CLI — it adds job mechanics (secret injection, triggers, artifact
archiving, the persistent portal-checkout), none of which the PoC needs to prove.
So **running the CLI by hand IS the "Jenkins job"** locally. Because dev and prod
differ only by config — `base_url` + per-env credentials (decision #5) — the local
CLI run is the exact prod code path. Stand up Jenkins only when the goal shifts from "triage works" to "triage runs
on a trigger" — and even then, likely the team's real CI (`testray2/ci/` already
exists), not a throwaway local container.

### The loop

```
[you] start local Testray DXP @ localhost:8080   (backed by testray_working_db)
  │
[deploy] liferay-testray-analytics-site-initializer CX
  │          → TriageResult Object + /o/c/triageresults endpoint
  │          + the OAuth2 app the CLI authenticates as
[deploy] frontend custom-element CX          → the cluster view
  │
[you run the CLI — this is the "Jenkins job"]:
  prepare  → READ caseresults A+B from localhost REST
           → COMPUTE diff + hunks vs. local liferay-portal checkout
  classify → REASON via Anthropic API
  submit   → WRITE TriageResult rows to localhost REST (batch upsert by ERC)
  │
[open the frontend CX in Testray] → clustered verdicts render
```

### Prerequisites

1. **Headless OAuth2 app** (client_credentials) registered in the local DXP — read
   `Build`/`Case`/`CaseResult`, write `TriageResult`; `client_id`/`secret` into the
   tool's `config.yml`.
2. **`TriageResult` Object deployed first** — `submit` can't write until
   `/o/c/triageresults` exists.
3. **Local `liferay-portal` checkout** containing both builds' git hashes
   (`git pull`) — `prepare` runs `git diff`/`git log` against it.
4. **A valid build A→B pair present in `testray_working_db`** (query REST/DB to pick
   one for a routine).
5. **`ANTHROPIC_API_KEY`** in the environment.

### Run bundles (where output lands)

`prepare` writes each run bundle to **`./runs/r_<id>/`** (cwd-relative) by default —
never inside the installed package. `classify` and `submit` take the bundle path
explicitly, so they operate on a bundle located anywhere. On Jenkins, pass
**`--out <dir>`** to write bundles into the job's workspace/artifacts dir instead of
`./runs`.

### Milestones (match §10)

1. **Tool → Object write works** — run the CLI, verify rows via
   `GET /o/c/triageresults`. No frontend yet; proves the whole read→classify→write
   loop.
2. **Frontend CX renders** the clustered results.
3. **Override capture** — a verdict change writes a `human` row (§12).
4. **Ticket creation** — extended `etc-jira` + write-back to `issues` (needs local
   per-user Jira OAuth).

> "Trigger the analysis" in the PoC = **you run the CLI**. A trigger *button in
> Testray* is the separate "Test trigger" feature (T3 in-scope item 3), later.

---

## 15. First live-run findings (2026-07-14) — tuning backlog

First full Tier-3 run against live Testray (Acceptance routine 590307, builds
496308678 → 497050886; 819 regressions, 736 classified via the real API). The loop
works end to end; these are the quality findings that feed later tuning tickets.

- **Hunk extraction was ~77% of the full diff** — barely filtered. The fragment
  matcher's **fuzzy fallback over-broadened on generic tokens** (`portal`, `resource`,
  `object`, `commerce`, `headless`) → hundreds of low-relevance files; meanwhile 356
  of 384 fragments (specific test classes) matched **nothing**. Net: missed the
  specific culprits, kept the noise. → **hunk-extraction tuning** (don't fuzzy-match
  generic tokens; prefer exact class-name matches).
- **74% NEEDS_REVIEW (545/736), 0 BUG, 6 POSSIBLE_BUG** — but the classifier is
  **sound**: the decisive verdicts were coherent, correctly ticket-attributed
  (LPD-97230, LPD-97016, LPD-85743) and rubric-correct (TEST_FIX → null culprit). So
  the NEEDS_REVIEW bloat is **evidence-driven, not reasoning-driven** — fixing hunk
  extraction should convert much of it into decisive verdicts. Strengthens the case
  for **by-cluster (§7)** and the **NEEDS_REVIEW-rate guardrail (open-Q #10)**.
- **Manifest/commits fallback confirmed working** — case 47786
  (`LayoutSEOLinkManagerPageTitleTest`, a fragment that matched *no* file) was still
  correctly attributed to `GroupLocalServiceImpl` (LPD-97016) via the commits
  section. The transitive-dep safety net does its job.
- **Verdicts cluster naturally** — the 9 decisive verdicts collapsed to ~4 root
  causes (3× `ObjectEntryFolderResourceImpl`, 2× `GroupLocalServiceImpl`, 3× theme
  migration). Concrete payoff waiting for `by-cluster`.
- **component/team blank on api rows** (0/819) — the api reader returns them as IDs,
  not names. Doesn't affect the product (the CX renders component/team from the
  native `CaseResult`), only the local debug report. → resolve IDs → names, or switch
  to the custom DTO (`testrayComponentName`/`testrayTeamName`) in the read-path
  retarget.
- **LLM output fidelity** — a few duplicate / out-of-batch / absent case_ids per run
  (handled gracefully: keep-first / drop / default to NEEDS_REVIEW). 729/736.
