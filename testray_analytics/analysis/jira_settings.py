"""
jira_settings.py — resolve the fields a prefilled Jira draft needs.

The report's "Create Jira ticket" link opens a *draft* in Jira; nothing is ever
created automatically (§11). Three fields cannot be inferred at render time and
have to be resolved up front:

* **parent** — the release's triage parent ticket. It changes every release, so
  it lives in config rather than code, and a single run can override it.
* **reporter** — an Atlassian accountId. The legacy `CreateIssueDetails`
  endpoint does *not* auto-fill Reporter, so without this every draft opens
  with an empty Reporter and the person filing has to remember to set it.
* **labels** — so triage drafts are filterable in Jira as a group.

Precedence, most specific first:
    --jira-parent  >  run.yml `jira_parent`  >  config.yml `jira.parent`

A blank field is **omitted** from the URL rather than sent empty: `parent=` and
`reporter=` with no value read to Jira as a deliberate clear, which is worse
than not saying anything.
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_LABEL = "release-test-failure"

# Liferay's LPD project and its Task issue type. Hard-coded because the draft
# URL is worthless without them and a wrong value fails at Jira rather than
# here; override via `jira.project_id` / `jira.issue_type` if that changes.
DEFAULT_PROJECT_ID = "11106"
DEFAULT_ISSUE_TYPE = "10002"


def fetch_jira_account_id(base_url: str, email: str, api_token: str) -> str:
    """Resolve the caller's Atlassian accountId via /rest/api/3/myself.

    Returns "" on any failure. A blank Reporter is recoverable — the person
    filing sets it — whereas a hard error here would block a report that is
    otherwise fine.
    """
    if not (base_url and email and api_token):
        return ""
    url = f"{base_url.rstrip('/')}/rest/api/3/myself"
    auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    req = urllib.request.Request(
        url, headers={"Authorization": f"Basic {auth}",
                      "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return str(json.loads(resp.read()).get("accountId") or "")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            ValueError, json.JSONDecodeError):
        return ""


def resolve_jira_settings(cfg: dict, run_meta: dict | None = None,
                          parent_override: str | None = None) -> dict:
    """Settings for the report's Jira draft links.

    `cfg` is the whole config (the `jira:` block is read from it), `run_meta`
    is run.yml, and `parent_override` is the CLI flag.
    """
    jira = (cfg or {}).get("jira") or {}
    run_meta = run_meta or {}

    parent = (parent_override
              or run_meta.get("jira_parent")
              or jira.get("parent")
              or "")

    account = str(jira.get("reporter_account_id") or "").strip()
    if not account:
        # Only reach for the network when the id was not supplied — this call
        # runs on every render otherwise.
        account = fetch_jira_account_id(
            str(jira.get("base_url") or ""),
            str(jira.get("email") or ""),
            str(jira.get("api_token") or ""),
        )

    return {
        "base_url": str(jira.get("base_url")
                        or "https://liferay.atlassian.net").rstrip("/"),
        "parent": str(parent).strip(),
        "reporter_account_id": account,
        # No default: the label is per-run, like parent and reporter. An
        # always-on default would tag every draft from every release with the
        # same label, which is exactly the staleness we are avoiding for
        # parent. DEFAULT_LABEL remains exported as the suggested value.
        "label": str(jira.get("label") or ""),
        "project_id": str(jira.get("project_id") or DEFAULT_PROJECT_ID),
        "issue_type": str(jira.get("issue_type") or DEFAULT_ISSUE_TYPE),
    }
