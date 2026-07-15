"""
report.py — slim, self-contained HTML report for a triage run.

Replaces RAP's large per-test render_html with a compact verdict table for
local inspection / demo before the Testray client extension exists. Rows are
ordered by verdict severity then by culprit_file (a stand-in grouping until
clusterKey clustering lands, §7).
"""

import html
from pathlib import Path

import pandas as pd

_VERDICT_ORDER = ["BUG", "POSSIBLE_BUG", "TEST_FIX", "NEEDS_REVIEW",
                  "FALSE_POSITIVE", "ENV_FAILURE", "DID_NOT_RUN"]
_VERDICT_COLOR = {
    "BUG": "#c0392b", "POSSIBLE_BUG": "#e67e22", "TEST_FIX": "#8e44ad",
    "NEEDS_REVIEW": "#f39c12", "FALSE_POSITIVE": "#7f8c8d",
    "ENV_FAILURE": "#95a5a6", "DID_NOT_RUN": "#b0b0b0",
}


def _esc(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return html.escape(str(v))


def _order_index(verdict) -> int:
    return _VERDICT_ORDER.index(verdict) if verdict in _VERDICT_ORDER else 99


def render_run(run_dir, df: pd.DataFrame, meta: dict) -> Path:
    """Write report.html into run_dir and return its path."""
    run_dir = Path(run_dir)
    counts = df["classification"].value_counts().to_dict() if len(df) else {}
    pills = " ".join(
        f'<span class="pill" style="background:{_VERDICT_COLOR.get(k, "#bdc3c7")}">'
        f'{_esc(k)}: {v}</span>'
        for k, v in sorted(counts.items(), key=lambda kv: _order_index(kv[0]))
    )

    doc = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Triage — {_esc(meta.get('run_id'))}</title>
<style>
 body{{font:14px/1.5 system-ui,-apple-system,sans-serif;margin:2rem;color:#222}}
 h1{{font-size:1.2rem;margin:0 0 .25rem}}
 .meta{{color:#555;margin-bottom:1rem}}
 .pill{{color:#fff;padding:2px 8px;border-radius:10px;font-size:12px;margin-right:6px;white-space:nowrap}}
 table{{border-collapse:collapse;width:100%;margin-top:1rem}}
 th,td{{border:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top;font-size:13px}}
 th{{background:#f5f5f5}} tr:nth-child(even){{background:#fafafa}}
 .v{{font-weight:600}} code{{font-size:12px;word-break:break-all}}
</style></head><body>
<h1>Triage report — build {_esc(meta.get('build_id_a'))} &rarr; {_esc(meta.get('build_id_b'))}</h1>
<div class="meta">routine {_esc(meta.get('routine_id'))} &middot; classifier {_esc(meta.get('classifier'))}
&middot; mode {_esc(meta.get('mode') or 'per-test')}<br>{pills}</div>
<table>
<thead><tr><th>#</th><th>Test</th><th>Component</th><th>Team</th><th>Status</th>
<th>Verdict</th><th>Conf.</th><th>Culprit file</th><th>Reasoning</th></tr></thead>
<tbody>
{_rows(df)}
</tbody></table>
</body></html>"""

    out = run_dir / "report.html"
    out.write_text(doc, encoding="utf-8")
    return out


def _rows(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return '<tr><td colspan="9">No rows.</td></tr>'
    ordered = sorted(
        (r for _, r in df.iterrows()),
        key=lambda r: (_order_index(r.get("classification")),
                       str(r.get("culprit_file") or "~")),
    )
    out = []
    for i, r in enumerate(ordered, 1):
        v = r.get("classification")
        color = _VERDICT_COLOR.get(v, "#bdc3c7")
        out.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{_esc(r.get('test_case'))}</td>"
            f"<td>{_esc(r.get('component_name'))}</td>"
            f"<td>{_esc(r.get('team_name'))}</td>"
            f"<td>{_esc(r.get('status_b') if r.get('status_b') is not None else r.get('status'))}</td>"
            f'<td class="v" style="color:{color}">{_esc(v)}</td>'
            f"<td>{_esc(r.get('confidence'))}</td>"
            f"<td><code>{_esc(r.get('culprit_file'))}</code></td>"
            f"<td>{_esc(r.get('reason'))}</td>"
            "</tr>"
        )
    return "\n".join(out)
