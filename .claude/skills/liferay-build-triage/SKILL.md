---
name: liferay-build-triage
description: Triage a Liferay Testray build-comparison run — decide which failures are real product bugs, which are test fixes, and which carry no signal at all — working from a run directory (results.json, diff_list.csv, diff_list_subtasks.csv, run.yml) plus the git range it covers. Use this whenever someone asks you to review, finish, or double-check triage of a Testray report, a build comparison, a POSSIBLE_BUG/NEEDS_REVIEW list, "why did these tests fail between build X and build Y", or hands you a candidate ticket and asks whether it explains a failure. Also use it when asked to classify a single Liferay test failure as BUG vs TEST_FIX against a commit range, even if no report file is mentioned.
---

# Liferay build-comparison triage

An automated classifier has already grouped failures and guessed at culprit tickets.
Its guesses are weak — in practice most proposed candidates are adjacency or keyword
matches, not causes. Your value is in the checks it could not do: reading the actual
diff, following the code path the failing assertion exercises, and refusing to attribute
a failure to a change that cannot produce it.

The cost model that should shape everything: **a wrong attribution is worse than an
honest "unresolved."** A wrong culprit sends someone to read code that is fine, and it
launders a guess into a conclusion. Say what the evidence supports and no more.

## Start here

```bash
python3 scripts/triage_setup.py <run_dir> --repo <liferay-portal checkout>
```

This does the bookkeeping you would otherwise redo by hand for every group: it buckets
groups by regression signal, clusters them by candidate ticket and by test file,
counts how many tests in each file are failing, and cross-references every group against
the test-side files the range changed. Work its worklist top-down.

If it reports it could not diff the range, fix that before going on — fetch the branch,
deepen a shallow clone. The sweep it performs is the single cheapest way to spot a test
fix, and skipping it is how good triage goes wrong.

## The four checks, in order

Each one is cheaper than the next and can end the analysis early. Running them out of
order is how you end up building an elaborate product-side theory for a test change.

### 1. Did the test's own code change in this range?

If a failing spec, its page object, its fixtures, its Poshi macro, or a shared helper
moved inside the range, treat the failure as a **test fix until proven otherwise**.
Attributing it to a production commit means claiming the test-side change was harmless,
and that claim needs its own evidence.

What to look for in the test-side diff:

- **A feature flag flipped** in `featureFlagsTest({...})`. This changes which DOM
  renders, so locators that matched before stop matching. Real example: flipping
  `LPD-11235` (CKEditor 5 → 4) broke a dozen CMS assertions that looked like an
  indexing regression.
- **"await the steps" fixes.** When previously un-awaited assertions start actually
  running, tests that were silently passing begin failing. Nothing regressed; the test
  started doing its job.
- **Locator or page-object refactors**, and label-case corrections.
- **Shared helpers.** A change to one helper can move hundreds of specs at once. See
  `references/known-traps.md` for the signatures that come from helpers rather than
  from the product.

### 2. Is there a regression signal at all?

The transition field decides how much effort a group deserves:

- `new` (PASSED → FAILED) — a genuine signal; this is where to spend time.
- `changed` / `same_failure` (FAILED → FAILED) — already broken; the signature merely
  drifted. Report it as pre-existing rather than hunting a culprit.
- `no_baseline` / `UNTESTED → FAILED` — the test did not run at baseline, so the range
  cannot be implicated either way.

Log-scan groups (`PortalLogAssertorTest`) are usually `no_baseline`, because the shard
numbering is not stable across builds. They rarely support attribution.

### 3. Is this one failure, or one broken file?

When every test in a spec file fails at once, that is one shared cause — a setup step,
a fixture, an environment condition — not N independent regressions. The setup script
prints a `whole-file:N failing` flag for this. A sibling already marked `PRE_EXISTING`
in the same file tells you the file is unstable, which should lower confidence in any
product attribution for its other tests.

Look for the same shape across modules too. If a dozen groups in nine different modules
all die inside the page editor, that is one investigation, not a dozen. Say so — it is
the most useful thing a triager can tell a team.

### 4. Could this change actually produce this error?

Only now open the candidate's commits:

```bash
git log <base>..<target> --name-only --format="### %h %s" --grep=<TICKET>
```

The file footprint is the fastest discriminator. Ask whether any changed file sits on
the code path the failing assertion exercises — then verify by reading the path, not by
matching names. Name collisions are the most common false lead: `site-item-selector-web`'s
`MySitesItemSelectorViewDisplayContext` has nothing to do with the `site-my-sites-web`
portlet, and a commit that swaps an iframe for inline HTML in a change-tracking renderer
does not touch a different module's preview modal.

A change that is purely additive, guarded, or upgrade-time-only cannot cause a runtime
failure. Note when you establish that — "this is an upgrade process, it does not run
during the test" is a complete and useful answer.

## Signature discipline

A failure signature supports attribution only if nothing else could have produced it.
Two that look specific but are not:

- **A bare `HTTP 500 {"status":"INTERNAL_SERVER_ERROR"}`** carries no exception class,
  no stack, no message. Any server-side throw on that endpoint produces it.
- **`Cannot invoke "X.m()" because "y" is null`** names the *variable*, not the caller.
  When that variable is a local inside a shared static util, every call site in the
  product yields a byte-identical message.

Before calling a bug confirmed, do these:

```bash
# How many other places produce the identical string, at the BASE commit?
git grep -c "SomeUtil.someMethod()" <base> -- '*.java'

# Was the suspect even present when this failed before?
gh pr view <n> --repo liferay/liferay-portal-ee --json comments   # lists failed specs
git merge-base --is-ancestor <suspect-commit> <pr-base-commit>
```

If the same test failed on a build or PR whose base predates the suspect, the suspect is
not the cause — regardless of how well the code reads. This is the single most common way
a confident verdict turns out wrong, and it is cheap to check.

Finding a real code defect along the way is worth reporting — as a **latent defect**,
separately from the failure being triaged. Those are two different claims.

## Verifying without a bundle

Most Liferay checkouts cannot run Poshi, Playwright or Arquillian tests — there is no
built bundle. That is not a dead end: the decisive question is usually narrower than
"does the test pass", and can be answered offline. `references/offline-verification.md`
has working recipes for running a single product class, probing selector semantics in a
real browser, compiling and diffing SCSS, comparing XML serializers, and diffing
upgraded third-party jars.

Whatever you run, **run a control**. A local failure that matches the reported signature
proves nothing until a known-good input passes through the same harness. Controls have
repeatedly caught harness artifacts masquerading as reproductions.

## Verdicts

Use these, and pick the weakest one the evidence supports:

| Verdict | Means |
|---|---|
| `BUG` | A production change is defective. Name the file, line, and mechanism. |
| `TEST_FIX` | The production change was intentional; the test asserts the old behaviour. |
| `PRE_EXISTING` | Already failing at baseline; the range is not implicated. |
| `NO_SIGNAL` | No baseline, or the shard did not run. Nothing can be concluded. |
| `UNRESOLVED` | Candidate rejected, real cause not established. Say what would settle it. |

`UNRESOLVED` is a legitimate, useful outcome. Prefer it to a plausible story.

## Reporting

Lead with what is actionable, not with a group-by-group walk. Structure:

1. **Confirmed bugs** — culprit file and line, the mechanism in a sentence or two, the
   evidence, and the suggested fix. If there are none, say so plainly and early; that is
   a real result.
2. **Test fixes** — what changed intentionally, which assertion encodes the old
   behaviour, and what to update.
3. **Systemic findings** — symptoms spanning many groups that should be one
   investigation. These usually carry the most value.
4. **Rejected candidates** — a compact table: group, ticket, and the one fact that rules
   it out. Terse is fine; the point is that someone can re-check you.
5. **No-signal bucket** — list the group ids and move on.
6. **Still open** — what would settle each, concretely (a trace artifact, a stack, a
   config value).

Also flag anything you noticed about the *report itself* — a whole-file failure split
across three groups, or one test handed a different culprit on each branch. Tooling
feedback compounds.

## Working at scale

Reports can carry a hundred-plus groups. Cluster first and analyse per cluster, not per
group — one ticket often covers seven failures. Give the honest arithmetic at the end
(how many bugs, how many test fixes, how many with no signal, how many still open). A
team reading triage wants to know where to point people, and "36 rejected with evidence,
2 real bugs" is a far more useful sentence than thirty paragraphs of maybes.
