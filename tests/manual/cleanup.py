"""Delete the rows the Stage A / Stage B manual tests create.

Removes the smoke TriageResults and the seed CaseResult by externalReferenceCode
so a test instance doesn't accumulate scratch data. Safe to run repeatedly —
404s (already gone) are ignored.

    python3 tests/manual/cleanup.py
"""

import urllib.error
import urllib.parse
import urllib.request

from testray_analytics.analysis.prepare import load_config, _testray_oauth_token

cfg = load_config()["testray"]
base = cfg["base_url"].rstrip("/")
token = _testray_oauth_token(cfg)

TARGETS = [
    ("triageresults", "local-smoke-1"),
    ("triageresults", "local-smoke-2"),
    ("triageresults", "local-smoke-fk-1"),
    ("caseresults",   "seed-cr-fktest"),
]

for plural, erc in TARGETS:
    path = f"/o/c/{plural}/by-external-reference-code/{urllib.parse.quote(erc, safe='')}"
    req = urllib.request.Request(f"{base}{path}", method="DELETE",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            print(f"deleted {plural}/{erc}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"gone     {plural}/{erc} (404)")
        else:
            print(f"FAILED   {plural}/{erc}: HTTP {e.code}")
