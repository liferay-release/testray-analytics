"""Stage B — prove the CaseResult -> TriageResult relationship FK.

1. Seed one CaseResult (no required fields, so a near-empty body works).
2. POST a TriageResult with r_caseResultToTriageResults_c_caseResultId = its id.
3. Read the TriageResult back and confirm the FK stored our id.
4. Read the CaseResult with the relationship nested and confirm it lists the
   TriageResult.

Idempotent (both writes are PUT-by-ERC upserts), so rerunning is safe.

Prereq: config/config.yml `testray` block points at the target with write-scope
creds; the TriageResult Object + caseResultToTriageResults relationship deployed.

    python3 tests/manual/stage_b_fk.py

Expect: step 3 prints OK; step 4 nested count = 1.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from testray_analytics.analysis.prepare import load_config, _testray_oauth_token
from testray_analytics.analysis.testray_writer import post_batch

cfg = load_config()["testray"]
base = cfg["base_url"].rstrip("/")
token = _testray_oauth_token(cfg)


def _req(method, path, body=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base}{path}", data=data, method=method,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# 1. Seed a CaseResult by ERC (upsert).
cr = _req("PUT", "/o/c/caseresults/by-external-reference-code/seed-cr-fktest",
          {"errors": "FK seed for TriageResult test"})
cr_id = cr["id"]
print(f"1. seed CaseResult id = {cr_id}")

# 2. Link a TriageResult to it.
items = [{
    "externalReferenceCode": "local-smoke-fk-1",
    "classification": {"key": "BUG"}, "confidence": {"key": "high"},
    "culpritFile": "x/Foo.java", "reason": "fk link test",
    "classifier": "manual:smoke", "analysisMode": "per-test",
    "r_caseResultToTriageResults_c_caseResultId": cr_id,
}]
print(f"2. post TriageResult: {post_batch(items, cfg)}")

# 3. Read the TriageResult back — did the FK store?
tr = _req("GET", "/o/c/triageresults/by-external-reference-code/local-smoke-fk-1")
fk = tr.get("r_caseResultToTriageResults_c_caseResultId")
print(f"3. TriageResult FK = {fk}  (expect {cr_id})  "
      f"{'OK' if fk == cr_id else 'MISMATCH — FK field name wrong or ignored'}")

# 4. Read the CaseResult with the relationship nested.
try:
    crn = _req("GET", f"/o/c/caseresults/{cr_id}"
                      f"?nestedFields=caseResultToTriageResults")
    nested = crn.get("caseResultToTriageResults")
    n = len(nested) if isinstance(nested, list) else nested
    print(f"4. CaseResult.caseResultToTriageResults nested count = {n}")
except urllib.error.HTTPError as e:
    print(f"4. nested read failed (non-fatal): HTTP {e.code} "
          f"{e.read().decode('utf-8','replace')[:200]}")
