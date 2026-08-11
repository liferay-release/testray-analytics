"""Probe the local OAuth app's read + write scopes on the objects the mirror
and the write-sink need. Creates a throwaway row per type (PUT-by-ERC) then
deletes it. Reports one line per object with GET (read) and PUT/DELETE (write).

    python3 tests/manual/check_local_scopes.py
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from testray_analytics.analysis.prepare import load_config, _testray_oauth_token

cfg = load_config()["testray"]
base = cfg["base_url"].rstrip("/")

# object plural -> a minimal, harmless create body (all these objects have no
# required fields, but we send one field so the row is identifiable).
PROBES = {
    "routines":      {"name": "scope-test"},
    "builds":        {"name": "scope-test"},
    "cases":         {"name": "scope-test"},
    "caseresults":   {"errors": "scope-test"},
    "triageresults": {"reason": "scope-test"},
}
ERC = "scopetest-probe"

try:
    token = _testray_oauth_token(cfg)
    print(f"token: OK  ({base})\n")
except SystemExit as e:
    raise SystemExit(f"token: FAILED — {e}")


def _call(method, path, body=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base}{path}", data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


print(f"{'object':<15} {'READ (GET)':<12} {'WRITE (PUT)':<13} {'DELETE':<8}")
print("-" * 50)
for plural, body in PROBES.items():
    read = _call("GET", f"/o/c/{plural}?pageSize=1")
    erc_path = f"/o/c/{plural}/by-external-reference-code/{urllib.parse.quote(ERC)}"
    write = _call("PUT", erc_path, body)
    delete = _call("DELETE", erc_path) if write < 400 else "-"

    def mark(code):
        if code == "-":
            return "-"
        return f"{code} {'OK' if code < 400 else 'DENIED'}"

    print(f"{plural:<15} {mark(read):<12} {mark(write):<13} {mark(delete):<8}")

print("\nAll four need READ+WRITE OK for the prod->local mirror.")
