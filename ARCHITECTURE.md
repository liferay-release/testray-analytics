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
  longer match). So `clusterKey` carries a version tag (e.g. `v2:<hash>`) and we
  preserve the raw signature inputs so keys can be recomputed on a bump; dedup/rollup
  must be version-aware. Migration strategy on a bump = open-Q #9.
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

> **Triggers (routine-dependent):** Stable + Acceptance fire on **build completion**;
> Release on **promotion**; PR is **user-driven**.

CLI gains `--mode {build-vs-build,routine-history,suite-vs-pr}` (default
`build-vs-build`). The `TriageResult` schema carries `analysisMode` so all three
coexist and the later modes slot in with no rework.

---

## 9. The `TriageResult` Object (integration contract)

Deployed via our own **`liferay-triage-site-initializer`** CX. It is a *new*
Object; defining it auto-creates its backing table and a `/o/c/triageresults`
REST endpoint.

- **Relationship:** `oneToMany` from Testray's `CaseResult` → our `TriageResult`.
  The FK (`r_caseResultToTriageResult_c_caseResultId`) lives on **our**
  `TriageResult` table — Testray's `CaseResult` definition is not edited.
- **Idempotency key (logical):** `(caseResult, classifier)` — re-running a
  classifier overwrites rather than duplicates.

**Proposed fields (lean — to finalize against site-initializer JSON patterns):**

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

---

## 10. Build sequence (follows the tickets)

1. **LPD-95842 (foundational):** this doc; repo scaffold; extract `apps/triage`
   as an api-only, CLI-first tool with the Postgres write removed.
2. **`TriageResult` Object (LPD-95843)** in a new `liferay-triage-site-initializer`
   CX (§9) — the contract everything hangs off.
3. **Headless write-sink (LPD-95843)** — replace the Postgres upsert with `POST
   /o/c/triageresults/batch`; register one OAuth2 service-account app.
4. **Frontend CX** (custom-element) — cluster view (LPD-95844) + verdicts column
   (LPD-95848), grouping by `clusterKey`.
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
  `externalReferenceCode` = `(buildB, caseId, classifier)`, through the headless
  **batch** endpoint (many rows per call, *not* one request per row); a rerun
  overwrites by ERC. Not a cost concern: one row per *classified failure* (the diff
  set — hundreds), not per test (~20k reads); the write is negligible next to the
  Anthropic classification.
- **#2 Read DTO** — sufficient; the custom `TestrayCaseResult` DTO exposes every
  diffed field (+ team/routine/run). No raw-endpoint fallback.
- **#3 Portal checkout** — persistent checkout, `git pull` each run (§2); never a
  fresh clone (multi-GB).
- **#4 Triggers** — routine-dependent: Stable + Acceptance on build completion;
  Release on promotion; PR user-driven (§8).
- **#5 `clusterKey` normalization** — strip volatile tokens (timestamps,
  `0x…`/hashes, UUIDs, line numbers, temp paths, ports, `…ms`, build IDs), keep
  exception class + first message line + top ~3 normalized frames, lowercase, hash.
  Conservative v1; iterate under #9.
- **#6 Insights pull cadence** — deferred (later); likely a **weekly weekend** pull.
- **#7 Provenance** — a Jira label **`testray-suggested`** applied at creation via
  `etc-jira`. Provenance lives in Jira (queryable), manual tickets lack it, and
  Testray carries **no extra field**. (Acting user already captured — per-user OAuth.)
- **#9 `clusterKey` versioning** — version-tag the key (`v2:…`), store raw signature
  inputs, and on a bump **backfill re-key only the ~90-day retention window** (T3's
  retention bounds the cost; older rows age out). Dedup compares within a version.
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
[deploy] liferay-triage-site-initializer CX  → creates the TriageResult Object
  │                                            + /o/c/triageresults endpoint
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
