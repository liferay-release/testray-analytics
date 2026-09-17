# Known traps

Failure signatures and candidate shapes that have repeatedly produced wrong verdicts.
Check these before writing a conclusion — each one has cost a real triage.

## Signatures that come from the harness, not the product

**`locator.click: Timeout 100ms exceeded`** — only `modules/test/playwright/utils/clickAndExpectToBeVisible.ts`
can produce this. It destructures `timeout = 100` and applies it to the **action** as well
as the assertion:

```ts
if (!(await target.isVisible())) {
    await trigger.click({timeout});   // 100ms on the click itself
}
await expect(target).toBeVisible({timeout});
```

`playwright.config.ts` sets no `actionTimeout`, so an ordinary `locator.click()` is bounded
only by the 90 s test timeout. The call log shows the element resolved fine; it is a
too-tight action timeout on a slow-mounting page, not an unclickable element. Roughly 230
files call this helper, so one change to it moves a lot of specs at once.

**`Test timeout of 90000ms exceeded while running "beforeEach" hook`** — the whole spec
file is down, which is a setup or environment problem, not N product regressions. Note
which setup step is unbounded: `PageEditorPage.publishPage()` wraps
`button.click({timeout: 1000})` + `waitForAlert` in an unbounded `expect(...).toPass()`,
so a publish that stops succeeding spins until the test dies.

**An element that resolves but cannot be interacted with** (`locator resolved to <input …>`
then a fill/click timeout) means something intercepts pointer events or the element keeps
moving. Look for overlays and re-renders, not for missing markup.

## Signatures that cannot name their cause

- **Bare `HTTP 500 {"status":"INTERNAL_SERVER_ERROR"}`** — no exception class, no stack.
  Any server-side throw on that endpoint produces it.
- **`Cannot invoke "X.m()" because "y" is null`** — names the null *variable*. If that is a
  local inside a shared static util (`ClusterExecutorUtil.isEnabled()` assigns
  `Snapshot.get()` to a local named `clusterExecutor`), every call site yields the same
  message. Count call sites at the **base** commit before blaming a new one.
- **Generic timeouts and "element is not present"** — carry almost no attribution weight on
  their own.

For all of these: look for the same test failing on an earlier build or on a PR whose base
predates the suspect. `gh pr view <n> --json comments` lists failed specs;
`git merge-base --is-ancestor <suspect> <pr-base>` says whether the suspect existed.

## Candidate shapes that cannot be the cause

- **Upgrade processes** (`internal/upgrade/vN_M_0/…`, `*UpgradeStepRegistrator`) run at
  module upgrade, not during a runtime test on a fresh database. They can still make an
  *upgraded* database diverge from a fresh one, which matters only for schema-comparison
  tests.
- **Purely additive changes** — a new overloaded constructor, a new sentinel constant, an
  `OR`-ed fallback in a permission check. These widen behaviour; they cannot remove an
  element or fail a check that passed before.
- **Guarded / flag-gated code** that the test's configuration does not enable.
- **Changes behind a `.catch`** that swallows the error, or gated on a condition the test
  never meets.

## Name collisions

The most common false lead. Verify the module path, not the class name:

- `site-item-selector-web`'s `MySitesItemSelectorViewDisplayContext` is the "pick a site"
  modal — nothing to do with the `site-my-sites-web` portlet.
- A commit that replaces an iframe with inline HTML in `LayoutCTDisplayRenderer`
  (change tracking) does not touch another module's preview modal that also uses an iframe.
- A "cluster" keyword in a commit message is not a link to a `ClusterExecutor` NPE.
- A dependency bump that mentions an XML library is not a link to an XML parser warning
  unless the string is actually in that library — grep the jar.

## Test-side changes that look like product regressions

- **A feature flag flipped in `featureFlagsTest({...})`** changes which DOM renders.
  Flipping `LPD-11235` (CKEditor 5 → 4) broke a batch of CMS assertions that read exactly
  like an indexing regression.
- **"await the steps so the assertions run"** fixes make previously silent assertions
  execute. Nothing regressed.
- **Label-case corrections** (`getByLabel('contents')` → `'Contents'`) and page-object
  refactors move locators wholesale.
- **A locator that encodes old behaviour** — e.g. a Poshi path asserting an English
  "Select" button next to a localized field label. When the product starts localizing both,
  the locator, not the product, is wrong.

## Things worth checking once per report

- **Fixture data that was never valid.** A Poshi `.config` dependency carrying
  `privacyPolicyLink="INSTANCE_LEVEL"` starts failing the moment the product validates
  policy links. The product change is the security fix; the fixture is the bug.
- **Golden files not regenerated** after an intentional serializer or format change.
- **Whole-file failures split across several groups** — the report's grouping can hide
  that one file is down. The setup script's `whole-file:N failing` flag catches this.
- **The same test failing on two branches** with different candidates proposed on each.
  That is strong evidence neither range is responsible.
