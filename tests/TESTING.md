# Testing — testray-analytics

How this tool is validated, and how to re-run each check. Two layers:

- **Automated (offline, no network):** `pytest` unit tests under `tests/`.
- **Manual (live, against a Testray instance):** scripts under `tests/manual/`
  that exercise the real read/write path.

The full pipeline is `prepare → classify → submit`. Historically we validated
it in three tiers; LPD-95843 added the headless write-back, validated in two
stages (A/B).

---

## Automated tests — `pytest`

```bash
source .venv/bin/activate
pip install -e ".[dev]"     # first time only — installs pytest
pytest -q
```

| File | Covers |
|---|---|
| `tests/test_post_batch.py` | `testray_writer.post_batch` — the HTTP upsert path, against a throwaway localhost server impersonating Testray. PUT-by-ERC verb/path, URL-quoted ERC, auth + JSON headers, one token per batch, 401 re-mint + retry, no-retry-on-4xx, retry-then-report on 5xx, partial batch (one bad item doesn't abort), dropped connection, unreachable host. No DXP. |
| `tests/test_writer.py` | `testray_writer.build_batch` — the `/o/c/triageresults` payload. Enum→picklist-key flattening (`POSSIBLE_BUG→POSSIBLEBUG`, …), the `{"key": …}` picklist shape, the CaseResult relationship FK (`r_caseResultToTriageResults_c_caseResultId`), unlinked-when-no-`caseresult_id`, the ERC format, and the write inclusion policy (FALSE_POSITIVE-high + auto buckets excluded). No network. |

Add new offline logic tests here — anything that doesn't need a live DXP.

---

## Manual / integration — `tests/manual/`

These hit a live Testray. **Prerequisites:**

1. The `liferay-testray-analytics-site-initializer` client extension deployed to
   the target instance (TriageResult Object + the two picklists + the
   `caseResultToTriageResults` relationship).
2. `config/config.yml` `testray` block pointed at that instance with
   **write-scope** OAuth2 client-credentials:
   ```yaml
   testray:
     base_url: "http://localhost:8080"
     client_id:     <app id>
     client_secret: <app secret>
   ```
   The app's service-account user needs Add/Update permission on TriageResult
   (Control Panel → Security → OAuth2 Administration → Client Credentials app).

| Script | What it proves | Needs Testray data? |
|---|---|---|
| `stage_a_smoke.py` | OAuth write scope, `{"key": …}` picklist write shape, PUT-by-ERC create. Writes 2 **unlinked** TriageResults. | No |
| `stage_a_verify.py` | Rerun **upserts** (not duplicates) + picklist keys resolve to labels on read (`{"key":"BUG","name":"Bug"}`). | No |
| `stage_b_fk.py` | The **CaseResult→TriageResult FK**: seeds one CaseResult, links a TriageResult, confirms the FK stored and the relationship nests back. | One CaseResult (the script seeds it) |
| `cleanup.py` | Deletes the smoke rows + seed CaseResult by ERC. Run when done. | — |

Run order:

```bash
python3 tests/manual/stage_a_smoke.py      # expect: result: (2, 0, [])
python3 tests/manual/stage_a_verify.py     # expect: totalCount 2 (not 4)
python3 tests/manual/stage_b_fk.py         # expect: step 3 OK, step 4 count = 1
python3 tests/manual/cleanup.py            # tidy up
```

> These live in `tests/manual/` (not collected by `pytest`, which only picks up
> `test_*.py`) because they require a running DXP and mutate data.

---

## Validation log

**2026-08-11 — LPD-95843 write path re-validated on a rebuilt local DXP,
now through the CLI (`testray-analysis submit`), not just the stage scripts:**
- OAuth: the CX's headless-server app authenticates; object scopes resolve only
  as plain `c_<object>.everything` (`.read` / `.write` leaves are dropped
  silently at deploy). Check the token's `scope` claim, not the yaml.
- Stage A `(2, 0, [])`, rerun kept `totalCount = 2`; picklists resolved as
  `{"key":"BUG","name":"Bug"}`. Stage B FK stored (`35894`) and nested back.
- `submit` end-to-end: 2 rows written, FK resolved 2/2, 12 excluded by the
  write policy. Second identical `submit` left `totalCount = 2` — upsert, no
  duplicates. Each row nests back from its CaseResult.
- Retrieval by build uses the ERC prefix
  (`startswith(externalReferenceCode,'51996_')`) — TriageResult has no buildId.
- Caveat: the instance had no routines/builds/cases, so the run's CaseResults
  were seeded and `diff_list.csv` repointed at the local ids. The full
  `prepare → classify → submit` still needs real data mirrored in.

**2026-07-15 — LPD-95843 write path, against local DXP (`localhost:8080`):**
- Stage A: `(2, 0, [])`; rerun kept `totalCount = 2` (upsert, no dupes);
  classification/confidence returned as resolved `{"key","name"}` picklists.
- Stage B: FK stored (`35871 == 35871`, OK); CaseResult nested
  `caseResultToTriageResults` count = 1.
- All four write assumptions confirmed: write scope, `{"key": …}` picklist
  shape, PUT-by-ERC upsert, FK field name `r_caseResultToTriageResults_c_caseResultId`.
- `tests/test_writer.py`: 20 passed.

**Earlier — Tier 1/2/3 (LPD-95842), the read/classify pipeline** (see git
history / `ARCHITECTURE.md §15`):
- **Tier 1** — install + CLI (`testray-analysis prepare|classify|submit --help`).
- **Tier 2** — offline: bundle shape, `triageresults_batch.json` inclusion policy.
- **Tier 3** — full loop end-to-end against **live** `testray.liferay.com` + real
  Anthropic API. Classifier sound; known evidence gap = hunk extraction
  over-broadens (→ NEEDS_REVIEW skew), tracked separately.

---

## Known gaps / next

- **Full local loop** (`prepare → classify → submit` against localhost) still
  needs real caseresult data on the instance — localhost is otherwise empty.
  Until then, the write path is validated via the Stage A/B scripts above.
- `prepare.fetch_build_caseresults_api` does not yet carry the caseResult
  object id into the diff, so **new** runs write unlinked TriageResults. Run
  dirs from 2026-07-17 onward already have a `caseresult_id` column in
  `diff_list.csv` and do resolve the FK — use one of those to exercise the
  linked write path until prepare is updated.
