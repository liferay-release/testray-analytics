"""Stage A verify — re-POST (upsert) then read the smoke rows back.

Proves rerun upserts rather than duplicates, and shows how the picklist
values (classification / confidence) come back — key + resolved label.

Prereq: run stage_a_smoke.py first (same config/config.yml target).

    python3 tests/manual/stage_a_verify.py

Expect: totalCount 2 (not 4), each classification/confidence a resolved
{"key": ..., "name": ...}.
"""

import json
import urllib.parse
import urllib.request

from testray_analytics.analysis.prepare import load_config, _testray_oauth_token
from testray_analytics.analysis.testray_writer import post_batch

cfg = load_config()["testray"]
base = cfg["base_url"].rstrip("/")

# 1. Re-post the same two ERCs — should upsert, not duplicate.
items = [
    {"externalReferenceCode": "local-smoke-1", "classification": {"key": "BUG"},
     "confidence": {"key": "high"}, "culpritFile": "x/Foo.java",
     "reason": "smoke test", "classifier": "manual:smoke", "analysisMode": "per-test"},
    {"externalReferenceCode": "local-smoke-2", "classification": {"key": "POSSIBLEBUG"},
     "confidence": {"key": "medium"}, "reason": "smoke test 2",
     "classifier": "manual:smoke", "analysisMode": "per-test"},
]
print("re-post:", post_batch(items, cfg))

# 2. Read back and count.
token = _testray_oauth_token(cfg)
url = f"{base}/o/c/triageresults?" + urllib.parse.urlencode({
    "filter": "classifier eq 'manual:smoke'",
    "pageSize": 50,
})
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
with urllib.request.urlopen(req, timeout=30) as resp:
    body = json.loads(resp.read())

print(f"\ntotalCount (classifier='manual:smoke'): {body.get('totalCount')}  "
      f"(expect 2, not 4)")
for it in body.get("items", []):
    print(f"  - erc={it.get('externalReferenceCode')} "
          f"classification={it.get('classification')} "
          f"confidence={it.get('confidence')} "
          f"culpritFile={it.get('culpritFile')!r}")
