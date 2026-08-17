"""
report.py — slim, self-contained HTML report for a triage run.

Local inspection / demo before the Testray client extension exists
(LPD-95844). Structure follows the PR-triage report it replaces: a headline
that quantifies the funnel, one section per **cluster** with its member
failures expandable underneath, then the flat all-cases table as drill-down.

Clustered by default, per §7 — per-test is the drill-down, not the front page.
`clusterKey` is recomputed here rather than read from the row: the writer adds
it when building the Testray payload, but the report renders straight from the
submit dataframe, and both derive it from the same inputs via the same
function, so they cannot disagree.

Colours are Testray's own (custom-element `styles/_variables.scss` and
`util/constants.ts` DATA_COLORS) so the report reads as part of the product
rather than a separate tool.
"""

import collections
import html
from pathlib import Path

import pandas as pd

from . import error_signature

# Testray's palette. Verdicts are ours, not Testray statuses, so they are
# mapped onto the nearest product meaning: BUG takes the FAILED red,
# POSSIBLE_BUG the lighter red from the status pills, NEEDS_REVIEW the BLOCKED
# amber ("needs attention"), TEST_FIX the exact TEST_FIX blue, and the
# non-actionable buckets the incomplete/untested greys.
# (bg, fg) — amber and grey need dark text to stay legible.
_VERDICT_ORDER = ["BUG", "POSSIBLE_BUG", "TEST_FIX", "NEEDS_REVIEW",
                  "FALSE_POSITIVE", "ENV_FAILURE", "DID_NOT_RUN"]
_VERDICT_COLOR = {
    "BUG":            ("#E73A45", "#fff"),   # metrics.failed
    "POSSIBLE_BUG":   ("#FE5160", "#fff"),   # $failedColor, lighter
    "TEST_FIX":       ("#59BBFC", "#08243c"),  # metrics.testfix
    "NEEDS_REVIEW":   ("#F8D72E", "#3a3000"),  # metrics.blocked
    "FALSE_POSITIVE": ("#BCBDC0", "#22262a"),  # $untestedColor
    "ENV_FAILURE":    ("#E3E9EE", "#22262a"),  # metrics.incomplete
    "DID_NOT_RUN":    ("#E3E9EE", "#22262a"),
}
_DEFAULT_COLOR = ("#E3E9EE", "#22262a")

_CSS = """
 body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:2rem;
      color:#272833;background:#f1f2f5}
 h1{font-size:1.25rem;margin:0 0 .25rem}
 .meta{color:#6b6c7e;margin-bottom:1rem;font-size:13px}
 .pill{padding:2px 9px;border-radius:10px;font-size:12px;margin-right:6px;
       white-space:nowrap;font-weight:600;display:inline-block}
 .card{background:#fff;border:1px solid #e7e7ed;border-radius:6px;
       margin:0 0 10px;overflow:hidden}
 summary{padding:10px 12px;cursor:pointer;list-style:none;display:flex;
         gap:10px;align-items:baseline}
 summary::-webkit-details-marker{display:none}
 summary:hover{background:#f7f8f9}
 .count{color:#6b6c7e;font-size:12px;white-space:nowrap}
 .sig{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:#272833;
      overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
 table{border-collapse:collapse;width:100%}
 th,td{border-top:1px solid #e7e7ed;padding:6px 10px;text-align:left;
       vertical-align:top;font-size:13px}
 th{background:#f7f8f9;color:#6b6c7e;font-weight:600;border-top:none}
 code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
 h2{font-size:1rem;margin:1.5rem 0 .5rem;color:#6b6c7e}
"""


def _esc(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return html.escape(str(v))


def _order_index(verdict) -> int:
    return _VERDICT_ORDER.index(verdict) if verdict in _VERDICT_ORDER else 99


def _pill(verdict) -> str:
    bg, fg = _VERDICT_COLOR.get(verdict, _DEFAULT_COLOR)
    return f'<span class="pill" style="background:{bg};color:{fg}">{_esc(verdict)}</span>'


def _cluster_of(row) -> str:
    return error_signature.cluster_key(row.get("culprit_file"),
                                       row.get("test_case"),
                                       row.get("error_message"))


def render_run(run_dir, df: pd.DataFrame, meta: dict) -> Path:
    """Write report.html into run_dir and return its path."""
    run_dir = Path(run_dir)
    counts = df["classification"].value_counts().to_dict() if len(df) else {}
    pills = " ".join(
        f'<span class="pill" style="background:{_VERDICT_COLOR.get(k, _DEFAULT_COLOR)[0]};'
        f'color:{_VERDICT_COLOR.get(k, _DEFAULT_COLOR)[1]}">{_esc(k)}: {v}</span>'
        for k, v in sorted(counts.items(), key=lambda kv: _order_index(kv[0])))

    clusters = _clusters(df)
    funnel = (f"{len(df)} failures &middot; {len(clusters)} clusters"
              if len(df) else "no rows")

    doc = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Triage — {_esc(meta.get('run_id'))}</title>
<style>{_CSS}</style></head><body>
<h1>Triage report — build {_esc(meta.get('build_id_a'))} &rarr; {_esc(meta.get('build_id_b'))}</h1>
<div class="meta">routine {_esc(meta.get('routine_id'))} &middot;
classifier {_esc(meta.get('classifier'))} &middot;
mode {_esc(meta.get('mode') or 'per-test')} &middot; {funnel}<br>{pills}</div>
{_cluster_cards(clusters)}
<h2>All cases</h2>
<div class="card"><table>
<thead><tr><th>#</th><th>Test</th><th>Component</th><th>Team</th><th>Status</th>
<th>Verdict</th><th>Conf.</th><th>Culprit file</th><th>Reasoning</th></tr></thead>
<tbody>
{_rows(df)}
</tbody></table></div>
</body></html>"""

    out = run_dir / "report.html"
    out.write_text(doc, encoding="utf-8")
    return out


def _clusters(df: pd.DataFrame):
    """Group rows by clusterKey, worst verdict first, then biggest."""
    if df is None or df.empty:
        return []
    buckets = collections.defaultdict(list)
    for _, r in df.iterrows():
        buckets[_cluster_of(r)].append(r)
    out = []
    for key, members in buckets.items():
        worst = min((_order_index(m.get("classification")) for m in members),
                    default=99)
        out.append((key, worst, members))
    # Severity first, then size — a 30-member NEEDS_REVIEW cluster still ranks
    # below a single BUG, because the BUG is the thing to act on.
    out.sort(key=lambda t: (t[1], -len(t[2])))
    return out


def _cluster_cards(clusters) -> str:
    if not clusters:
        return ""
    cards = []
    for key, worst, members in clusters:
        verdict = _VERDICT_ORDER[worst] if worst < len(_VERDICT_ORDER) else None
        first = members[0]
        sig = error_signature.normalize(first.get("error_message")) or "(no error text)"
        culprit = first.get("culprit_file")
        # Collapsed by default for single-member clusters, open for the rest:
        # a cluster of one is just a row, a cluster of many is the finding.
        open_attr = " open" if len(members) > 1 and worst <= 1 else ""
        rows = "".join(
            f"<tr><td>{_esc(m.get('test_case'))}</td>"
            f"<td>{_esc(m.get('component_name'))}</td>"
            f"<td>{_esc(m.get('team_name'))}</td>"
            f"<td>{_pill(m.get('classification'))}</td>"
            f"<td>{_esc(m.get('confidence'))}</td>"
            f"<td>{_esc(m.get('reason'))}</td></tr>"
            for m in sorted(members, key=lambda m: _order_index(m.get("classification")))
        )
        cards.append(
            f'<details class="card"{open_attr}><summary>'
            f'{_pill(verdict)}'
            f'<span class="sig">{_esc(sig[:160])}</span>'
            f'<span class="count">{len(members)} failure(s)</span>'
            f'</summary>'
            + (f'<div style="padding:6px 12px"><code>{_esc(culprit)}</code></div>'
               if culprit else "")
            + '<table><thead><tr><th>Test</th><th>Component</th><th>Team</th>'
              '<th>Verdict</th><th>Conf.</th><th>Reasoning</th></tr></thead>'
              f'<tbody>{rows}</tbody></table></details>'
        )
    return "\n".join(cards)


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
        out.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{_esc(r.get('test_case'))}</td>"
            f"<td>{_esc(r.get('component_name'))}</td>"
            f"<td>{_esc(r.get('team_name'))}</td>"
            f"<td>{_esc(r.get('status_b') if r.get('status_b') is not None else r.get('status'))}</td>"
            f"<td>{_pill(r.get('classification'))}</td>"
            f"<td>{_esc(r.get('confidence'))}</td>"
            f"<td><code>{_esc(r.get('culprit_file'))}</code></td>"
            f"<td>{_esc(r.get('reason'))}</td>"
            "</tr>"
        )
    return "\n".join(out)
