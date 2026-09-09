"""
preflight.py — can this instance do what the run is about to ask of it?

Three questions, asked in the order that makes a failure legible:

  1. Do the credentials mint a token at all?
  2. What scopes does that token actually carry?
  3. Do the triage Objects answer, and if not, WHY not?

Question 3 is the one that pays for this module. `/o/c/triageruns` can fail two
ways that look identical from the outside and mean opposite things:

    404  the analytics client extension is not deployed here. Expected on a
         stock Testray. The run still works: the scanner falls back to marker
         files, and verdicts stay in the local report.
    403  the Object IS deployed, and this OAuth application was not granted its
         scope. A misconfiguration — and a silent one, because
         `TestrayQueue.available()` treats any exception as "not deployed", so
         the queue quietly stays file-based and every write is refused.

The scope list is the other half. A client-credentials app created before the
triage Objects existed carries none of their scopes, and `.read` / `.write`
variants are accepted in the app's configuration and then silently dropped —
so the app's own screen is not evidence. The token's `scope` claim is.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from .prepare import load_config, testray_target

# Objects the pipeline reads or writes, and what breaks without each.
TRIAGE_OBJECTS = (
    ("triageresults", "c_triageresult",
     "submit cannot write verdicts back; the report is the only record"),
    ("triageruns", "c_triagerun",
     "the queue falls back to marker files and no build-list diamond appears"),
    ("triageroutinesettings", "c_triageroutinesetting",
     "per-routine autoTriage settings cannot be read"),
)

OK, WARN, FAIL = "ok", "warn", "fail"


def token_scopes(cfg: dict) -> tuple[str, list[str]]:
    """Mint a token and return it with the scopes it actually carries."""
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    }).encode()
    url = cfg["base_url"].rstrip("/") + "/o/oauth2/token"
    with urllib.request.urlopen(url, data, timeout=30) as resp:
        body = json.loads(resp.read())
    return body.get("access_token", ""), sorted(body.get("scope", "").split())


def probe(cfg: dict, token: str, path: str) -> tuple[int, str]:
    """HTTP status for a one-row GET, and a short note. 0 on a transport error."""
    url = f"{cfg['base_url'].rstrip('/')}/o/c/{path}?pageSize=1"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.reason or ""
    except Exception as e:                                       # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def check(cfg: dict, *, require_objects: bool = False) -> str:
    """Print the report. Returns "ok", "warn" or "fail"."""
    tr = cfg.get("testray") or {}
    print(f"Testray:  {testray_target(cfg)}")

    for field in ("base_url", "client_id", "client_secret"):
        if not tr.get(field):
            print(f"  FAIL  testray.{field} is not set (env or config.yml).")
            return FAIL

    try:
        token, scopes = token_scopes(tr)
    except urllib.error.HTTPError as e:
        # 401 here is the credentials themselves; anything else is the instance.
        print(f"  FAIL  could not mint a token: HTTP {e.code} {e.reason}. "
              f"Check TESTRAY_CLIENT_ID / TESTRAY_CLIENT_SECRET.")
        return FAIL
    except Exception as e:                                       # noqa: BLE001
        print(f"  FAIL  could not reach {tr['base_url']}: "
              f"{type(e).__name__}: {e}")
        return FAIL

    print(f"  ok    token minted, {len(scopes)} scope(s)")

    worst = OK
    for path, scope_prefix, consequence in TRIAGE_OBJECTS:
        granted = any(s.startswith(scope_prefix) for s in scopes)
        status, note = probe(tr, token, path)

        if status == 200 and granted:
            print(f"  ok    /o/c/{path}")
            continue

        if status == 403 or (status == 200 and not granted):
            # The masquerade case: the queue probe cannot tell this from 404.
            print(f"  FAIL  /o/c/{path} → HTTP {status}: the Object is there, "
                  f"but this app has no {scope_prefix} scope.")
            print(f"        Add {scope_prefix}.everything to the OAuth2 "
                  f"application (plain `.everything` — `.read`/`.write` are "
                  f"dropped silently), and give the user it acts as the "
                  f"Testray Administrator role.")
            print(f"        Until then: {consequence}.")
            worst = FAIL
            continue

        if status == 404:
            level = "FAIL" if require_objects else "warn"
            print(f"  {level:<4}  /o/c/{path} → 404, the analytics client "
                  f"extension is not deployed on this instance.")
            print(f"        Consequence: {consequence}.")
            if not granted:
                print(f"        Note: this app also has no {scope_prefix} "
                      f"scope, so add it when the CX is deployed or the probe "
                      f"will keep reading as 404.")
            worst = FAIL if require_objects else (
                worst if worst == FAIL else WARN)
            continue

        print(f"  FAIL  /o/c/{path} → HTTP {status} {note}".rstrip())
        worst = FAIL

    if worst == WARN:
        print("\nUsable, in degraded mode: the queue will use marker files and "
              "verdicts stay in the local report. Deploy the analytics client "
              "extension to get the build-list diamond and write-back.")
    elif worst == OK:
        print("\nReady: rows queue in Testray and verdicts write back.")

    return worst


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Check credentials, OAuth scopes and the triage Objects "
                    "before a run spends anything.")
    ap.add_argument("--require-objects", action="store_true",
                    help="treat a missing Object as a failure. Pass this once "
                         "the analytics CX is deployed, so a regression in the "
                         "deploy is caught instead of silently degrading.")
    args = ap.parse_args()

    result = check(load_config(), require_objects=args.require_objects)
    raise SystemExit(0 if result in (OK, WARN) else 1)


if __name__ == "__main__":
    main()
