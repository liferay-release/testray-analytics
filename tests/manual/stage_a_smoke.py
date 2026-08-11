"""Stage A smoke test — POST two unlinked TriageResults to a live Testray.

Confirms, against a deployed TriageResult Object: OAuth write scope, the
picklist {"key": ...} write shape, and externalReferenceCode upsert. No
CaseResult FK (so it needs no Testray data) — both rows write unlinked.

Prereq: config/config.yml `testray` block points at the target instance with
write-scope creds (see tests/TESTING.md).

    python3 tests/manual/stage_a_smoke.py

Expect: result: (2, 0, []).  Run twice — the second run still reports 2 ok and
must not create duplicates (see stage_a_verify.py).
"""

from testray_analytics.analysis.prepare import load_config
from testray_analytics.analysis.testray_writer import post_batch

cfg = load_config()["testray"]
print(f"Target: {cfg['base_url']}")

items = [
    {"externalReferenceCode": "local-smoke-1", "classification": {"key": "BUG"},
     "confidence": {"key": "high"}, "culpritFile": "x/Foo.java",
     "reason": "smoke test", "classifier": "manual:smoke", "analysisMode": "per-test"},
    {"externalReferenceCode": "local-smoke-2", "classification": {"key": "POSSIBLEBUG"},
     "confidence": {"key": "medium"}, "reason": "smoke test 2",
     "classifier": "manual:smoke", "analysisMode": "per-test"},
]

n_ok, n_fail, failures = post_batch(items, cfg)
print(f"result: ({n_ok}, {n_fail}, {failures})")
