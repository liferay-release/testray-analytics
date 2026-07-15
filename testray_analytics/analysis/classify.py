"""
apps/triage/classify_api.py

Second classification path for triage run bundles: sends a prepared bundle
(runs/r_<id>/) through the Anthropic SDK and writes the same results.json
a Claude Code session would produce. Used in Jenkins / headless contexts
where a developer can't sit in a Claude Code session.

Usage:
    python3 -m apps.triage.classify_api <run_dir>
    testray-analysis classify <run_dir> [--classifier api:claude-opus-4-8]
    python3 -m apps.triage.classify_api <run_dir> --dry-run

    <run_dir> must already exist — produce it with apps.triage.prepare.
    After this script writes results.json, submit with:
        python3 -m apps.triage.submit <run_dir>

Why a second path instead of folding this back into prepare.py:
    prepare.py is classifier-agnostic. Keeping the Anthropic SDK out of
    prepare lets local dev laptops skip the extra dependency install
    unless they explicitly opt into API mode.

API design choices:

    * Structured outputs via output_config.format + json_schema. The
      Anthropic API guarantees the response is a single JSON object
      matching results.schema.json — no prompt-based "please return
      JSON" fence-stripping needed. The schema's conditional
      (BUG → culprit_file required) is stripped before sending (the
      structured-outputs subset does not support `if/then`), and
      enforced post-hoc via jsonschema.validate() against the original
      schema.

    * Streaming via client.messages.stream() + get_final_message().
      max_output_tokens=16000 is generous enough that non-streaming
      risks HTTP idle-timeout on slow responses; streaming keeps the
      connection live and collects the full message transparently.

    * output_config.effort = xhigh. Opus 4.7's effort parameter
      matters more than on any prior Opus. xhigh is Claude Code's
      default and the best setting for agentic / classification work.

    * Retries are delegated to the SDK. anthropic.Anthropic(max_retries=3)
      auto-retries 429 / 408 / 409 / 5xx with exponential backoff — we
      don't wrap another retry loop around it. The only locally-handled
      failure is JSONDecodeError (which shouldn't happen with structured
      outputs but we retry once as belt-and-braces).

Batching / cost rationale:

    Each API call pays for input_tokens + output_tokens. The bundle's
    prompt.md splits naturally into a shared header (context + rubric,
    ~1-5k tokens) and a list of per-failure sections with diff hunks.
    Two cost-shaping levers:

    1. Batch size. Bigger batches amortize the shared header over more
       failures. Default max_chars_per_batch = 400_000 (~100k input
       tokens) is sized so typical bundles of 50-150 failures land in
       1-2 batches instead of the 5-10 the old Sonnet-era pipeline used.

    2. Prompt caching. The shared header is sent with cache_control on
       every call, so only the first batch pays full price for the
       header — subsequent batches read it at ~10% cost within the
       5-minute ephemeral cache window. Cache-hit verification: check
       resp.usage.cache_read_input_tokens on batch 2+ of any multi-batch
       run. If it stays at 0, the header is below Opus 4.7's 4096-token
       minimum cacheable prefix (prefix under the threshold silently
       does not cache — no error).
"""

import argparse
import copy
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

try:
    import anthropic
except ImportError:
    sys.stderr.write(
        "anthropic SDK not installed. API mode requires:\n"
        "  pip install -r apps/triage/requirements-api.txt\n"
        "Local Claude Code classification does not need this package.\n"
    )
    raise SystemExit(1)

try:
    import jsonschema
except ImportError:
    sys.stderr.write(
        "jsonschema not installed. API mode requires:\n"
        "  pip install -r apps/triage/requirements-api.txt\n"
    )
    raise SystemExit(1)


from .config import find_config_file

TRIAGE_DIR  = Path(__file__).resolve().parent


def _disp(p) -> str:
    """Display path — relative to the current directory when possible, else absolute.
    Run bundles may live anywhere, not just under the package."""
    p = Path(p).resolve()
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        return str(p)

DEFAULT_CLASSIFIER = "api:claude-opus-4-8"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_api_config() -> dict:
    with open(find_config_file()) as f:
        cfg = (yaml.safe_load(f) or {}).get("triage", {})
    api_cfg = (cfg.get("classifier") or {}).get("api") or {}
    return {
        "model":              api_cfg.get("model", "claude-opus-4-8"),
        "effort":             api_cfg.get("effort", "xhigh"),
        "max_chars_per_batch": int(api_cfg.get("max_chars_per_batch", 400_000)),
        "max_output_tokens":   int(api_cfg.get("max_output_tokens", 16_000)),
        "delay_between":       float(api_cfg.get("delay_between_batches_seconds", 2)),
    }


def build_client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Export it before running API mode:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...\n"
            "Never store the key in config.yml."
        )
    # SDK auto-retries rate limits and 5xx with exponential backoff.
    return anthropic.Anthropic(api_key=key, max_retries=3)


# ---------------------------------------------------------------------------
# Prompt parsing — split prompt.md into (header, [failure sections])
# ---------------------------------------------------------------------------

_FAILURES_MARKER = "## Failures to classify"
_FAILURE_HEAD_RE = re.compile(r"^### (\d+)\. ", re.MULTILINE)
_CASE_ID_RE      = re.compile(r"\*\*case_id:\*\*\s*(\d+)")

# Subtask-mode markers
_SUBTASK_HEAD_RE = re.compile(r"^### (\d+)\. Subtask ", re.MULTILINE)
_SUBTASK_ID_RE   = re.compile(r"subtask_id=(\d+)")
_CASE_IDS_LINE_RE = re.compile(r"\*\*case_ids:\*\*\s*([0-9, ]+?)(?:\(\+|$|·)", re.MULTILINE)
_MEMBER_LINE_RE  = re.compile(r"^- \[(\d+)\]", re.MULTILINE)
_AUTO_TRACE_HDR  = "## Auto-classified subtasks"
_FLAKY_TRACE_HDR = "## Flaky-only subtasks"


@dataclass
class FailureSection:
    index:   int     # 1-based as shown in prompt.md
    case_id: int
    text:    str     # the full section text including header line


@dataclass
class SubtaskSection:
    """One subtask block in subtask-mode prompt.md."""
    index:      int            # 1-based as shown in prompt.md
    subtask_id: int | None     # None for unmapped singletons
    case_ids:   list[int]      # all member case_ids the verdict will fan to
    text:       str            # the full section text


def parse_prompt(prompt_md: Path) -> tuple[str, list[FailureSection]]:
    """Split prompt.md into (cacheable header, list of per-failure sections).

    The header runs from the top of the file up to the '## Failures to
    classify' line. Per-failure sections start at '### N. ' and run
    until the next '### ' or end-of-file.
    """
    text = prompt_md.read_text(encoding="utf-8")
    split_idx = text.find(_FAILURES_MARKER)
    if split_idx < 0:
        raise SystemExit(
            f"prompt.md missing '{_FAILURES_MARKER}' marker — is this a valid run bundle?"
        )

    header = text[:split_idx].rstrip() + "\n"
    body   = text[split_idx:]

    # Drop the '## Failures to classify' line itself and any blank lines
    # that follow, so sections start cleanly with '### N.'
    body = body.split("\n", 1)[1] if "\n" in body else ""

    matches = list(_FAILURE_HEAD_RE.finditer(body))
    if not matches:
        return header, []

    sections: list[FailureSection] = []
    for i, m in enumerate(matches):
        start = m.start()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[start:end].rstrip() + "\n"

        idx = int(m.group(1))
        cid_match = _CASE_ID_RE.search(chunk)
        if not cid_match:
            raise SystemExit(
                f"Failure #{idx} in prompt.md has no **case_id:** line — "
                f"cannot map back to testray_case_id."
            )
        sections.append(FailureSection(
            index=idx, case_id=int(cid_match.group(1)), text=chunk,
        ))

    return header, sections


def parse_prompt_subtask(prompt_md: Path) -> tuple[str, list[SubtaskSection]]:
    """Split a subtask-mode prompt.md into (cacheable header, classifiable
    subtask sections). Sections under the auto-classified / flaky-only
    trace headers are dropped — they exist for human readability but the
    classifier must not write entries for them."""
    text = prompt_md.read_text(encoding="utf-8")
    split_idx = text.find(_FAILURES_MARKER)
    if split_idx < 0:
        raise SystemExit(
            f"prompt.md missing '{_FAILURES_MARKER}' marker — is this a valid run bundle?"
        )

    header = text[:split_idx].rstrip() + "\n"
    body   = text[split_idx:]
    body = body.split("\n", 1)[1] if "\n" in body else ""

    # Truncate body at the first auto/flaky trace header — those subtasks are
    # listed for traceability only and must not produce results.json entries.
    cut = len(body)
    for hdr in (_AUTO_TRACE_HDR, _FLAKY_TRACE_HDR):
        idx = body.find(hdr)
        if idx >= 0 and idx < cut:
            cut = idx
    classifiable = body[:cut]

    matches = list(_SUBTASK_HEAD_RE.finditer(classifiable))
    if not matches:
        return header, []

    sections: list[SubtaskSection] = []
    for i, m in enumerate(matches):
        start = m.start()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(classifiable)
        chunk = classifiable[start:end].rstrip() + "\n"

        idx = int(m.group(1))
        sid_match = _SUBTASK_ID_RE.search(chunk[:200])  # in the header line
        sid = int(sid_match.group(1)) if sid_match else None

        # Pull case_ids: prefer the dedicated **case_ids:** line in meta;
        # fall back to the explicit `- [N]` member list (more authoritative
        # but truncated past 12 members in the prompt — unioning the two
        # gives the complete set).
        line_match = _CASE_IDS_LINE_RE.search(chunk)
        line_ids: list[int] = []
        if line_match:
            line_ids = [int(x.strip()) for x in line_match.group(1).split(",") if x.strip().isdigit()]
        member_ids = [int(m.group(1)) for m in _MEMBER_LINE_RE.finditer(chunk)]
        combined = []
        seen = set()
        for cid in (member_ids + line_ids):
            if cid not in seen:
                seen.add(cid)
                combined.append(cid)
        if not combined:
            raise SystemExit(
                f"Subtask #{idx} in prompt.md has no resolvable case_ids "
                f"(neither **case_ids:** line nor `- [N]` member list)."
            )
        sections.append(SubtaskSection(
            index=idx, subtask_id=sid, case_ids=combined, text=chunk,
        ))

    return header, sections


# ---------------------------------------------------------------------------
# Batching — pack failure sections under a char budget
# ---------------------------------------------------------------------------

def pack_batches(
    sections: list, max_chars: int,
) -> list[list]:
    """Greedy pack: append sections into the current batch until adding
    the next one would exceed max_chars, then start a new batch. Any
    single section larger than max_chars still gets its own batch (we
    don't split a single failure's hunks across calls). Works for both
    FailureSection (per-test) and SubtaskSection (subtask mode) since
    both have a `.text` attribute."""
    batches: list[list] = []
    current: list = []
    current_chars = 0

    for s in sections:
        sec_chars = len(s.text)
        if current and current_chars + sec_chars > max_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(s)
        current_chars += sec_chars

    if current:
        batches.append(current)
    return batches


# ---------------------------------------------------------------------------
# Schema prep for structured outputs
# ---------------------------------------------------------------------------

def prepare_api_schema(full_schema: dict) -> dict:
    """Strip `if`/`then` from the item schema before sending. Anthropic's
    structured-outputs subset does not support conditional keywords — the
    `BUG → culprit_file required` invariant is enforced post-hoc via
    jsonschema.validate() against the original schema."""
    api = copy.deepcopy(full_schema)
    items = api.get("properties", {}).get("results", {}).get("items", {})
    items.pop("if", None)
    items.pop("then", None)
    return api


# ---------------------------------------------------------------------------
# API call — one batch per call, with prompt caching + structured outputs
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTIONS = (
    "You are a developer at Liferay triaging test regressions between two "
    "builds. You classify each failure as BUG, POSSIBLE_BUG, TEST_FIX, "
    "NEEDS_REVIEW, or FALSE_POSITIVE based on whether a hunk in the diff "
    "plausibly caused it. culprit_file is required for BUG and expected for "
    "POSSIBLE_BUG (the single candidate); null otherwise.\n\n"
    "Confidence is structural and gates the tier:\n"
    "- BUG (confirmed): `high` confidence on a clearly VERIFIED culprit that is "
    "a genuine defect. MUST name culprit_file.\n"
    "- POSSIBLE_BUG: `medium` confidence with EXACTLY ONE plausible diff-caused "
    "culprit (a single changed file or single ticket cluster) that looks like a "
    "defect but you cannot verify to `high`. Name the single candidate in "
    "culprit_file. This is what distinguishes it from NEEDS_REVIEW — a concrete, "
    "single attribution. BUG and POSSIBLE_BUG culprit_files both feed defect "
    "training data, so only name a culprit you actually believe.\n"
    "- Two or more competing candidates → NEEDS_REVIEW (multi-cause), not "
    "POSSIBLE_BUG. No concrete file, or `low` confidence → NEEDS_REVIEW.\n\n"
    "TEST_FIX: the failure IS diff-caused, but the production change was "
    "intentional and correct and only a stale test lags behind it (the test "
    "asserts on a label/selector/element/API the diff deliberately changed, or "
    "one test layer was migrated and a legacy one left stale). The fix is to "
    "update the test, not production. Do NOT name the production file as "
    "culprit_file (that mislabels a correct change as a defect — BUG culprit "
    "files feed defect training data); leave culprit_file null (or name the "
    "stale test) and describe the test change in `specific_change`. When you "
    "can see the diff caused the failure but cannot tell whether production or "
    "the test is wrong, prefer NEEDS_REVIEW.\n\n"
    "Multiple candidate causes: when 2+ ticket clusters (LPD/LPP/LPS-XXXXX) in "
    "this diff plausibly affect the failing test's space — for example one "
    "cluster rewrote the persistence layer and another restructured build "
    "tooling — classify NEEDS_REVIEW (not BUG or POSSIBLE_BUG) even at high "
    "confidence and list all candidates in `specific_change` separated by `; `. "
    "POSSIBLE_BUG is only for a SINGLE candidate. Generic error "
    "messages ('compile failed', 'BUILD FAILED', aggregate batch status) are a "
    "strong signal that multiple changes could explain the failure.\n\n"
    "You cannot read source files from this prompt. When the failing test "
    "class plausibly imports, extends, or depends on code in a different "
    "changed module — especially when commits cluster under one ticket — do "
    "NOT default to FALSE_POSITIVE. Classify NEEDS_REVIEW with the suspected "
    "file path in `specific_change` so a human can verify the dependency. "
    "Reserve FALSE_POSITIVE for clearly environmental failures (timeouts, "
    "gradle/build infra, chrome version, TEST_SETUP_ERROR) or cases where no "
    "diff hunk could plausibly reach the failing test even via transitive "
    "deps. Erring toward NEEDS_REVIEW for borderline transitive cases is "
    "preferred over explicit dismissal."
)

_SYSTEM_INSTRUCTIONS_SUBTASK = (
    "You are a developer at Liferay triaging test regressions between two "
    "builds. **Unit of analysis: Testray Subtask** — each subtask groups N "
    "case-results that share a single error fingerprint. You write ONE "
    "verdict per subtask (BUG, POSSIBLE_BUG, TEST_FIX, NEEDS_REVIEW, or "
    "FALSE_POSITIVE), and the verdict fans out to every member case_id in the "
    "group when the bundle is submitted.\n\n"
    "Output format: one entry per subtask. Each entry MUST include "
    "`subtask_id` (integer or null), `case_ids` (non-empty array of every "
    "member case_id you saw in that subtask block — do not invent or omit), "
    "`classification`, `confidence`, and `reason`. culprit_file is required for "
    "BUG and expected for POSSIBLE_BUG (the single candidate), null otherwise.\n\n"
    "The exact same rubric applies as in per-test mode, only at the subtask "
    "level. Confidence is structural and gates the tier: BUG (confirmed) "
    "requires `high` confidence on a VERIFIED culprit for the *shared* error. "
    "POSSIBLE_BUG is `medium` confidence with EXACTLY ONE plausible diff-caused "
    "culprit (single file or single ticket cluster) named in culprit_file. Two "
    "or more competing candidates → NEEDS_REVIEW (multi-cause) with all "
    "candidates in `specific_change` separated by `; `, even at high confidence. "
    "No concrete single culprit, or `low` confidence → NEEDS_REVIEW.\n\n"
    "TEST_FIX: the shared failure is diff-caused but the production change was "
    "intentional and correct and the member tests simply assert on the old "
    "behavior (changed label/selector/element/API, or a migrated test layer "
    "left stale). The fix is to update the tests, not production. Do NOT name "
    "the production file as culprit_file; leave it null (or name the stale "
    "test) and describe the test change in `specific_change`.\n\n"
    "FALSE_POSITIVE is appropriate when the shared error is clearly "
    "environmental (timeouts, gradle/build infra, TEST_SETUP_ERROR, Poshi "
    "ElementNotFoundPoshiRunnerException, Selenium NoSuchElementException) "
    "or when no diff hunk could plausibly reach the group's failing tests "
    "via direct or transitive dependencies. The fact that one verdict covers "
    "many member tests does not change the rubric — a flake pattern is still "
    "a flake pattern when 30 tests share it. For borderline transitive "
    "cases prefer NEEDS_REVIEW over dismissal.\n\n"
    "Do not write entries for subtasks listed under '## Auto-classified' or "
    "'## Flaky-only' headers — those are traceability-only and submit.py "
    "handles them directly. Only classifiable subtasks (those above those "
    "section headers) need an entry."
)


def _build_user_text(
    batch: list, batch_number: int, total_batches: int,
    classifier: str, run_id: str, mode: str = "per-test",
) -> str:
    """Wrap the per-failure sections with headers naming the expected
    run_id / classifier so the structured output lands with the right
    provenance. `mode` selects the output instructions — per-test mode
    expects `testray_case_id` per entry, subtask mode expects
    `subtask_id` + `case_ids` array per entry."""
    if mode == "by-subtask":
        instructions = (
            f"\n\nPopulate run_id=\"{run_id}\" and classifier=\"{classifier}\" in "
            f"the output. Include exactly one result per Subtask shown above. "
            f"For each entry: set `subtask_id` to the integer from the section "
            f"header (or null when the section header says 'no subtask link "
            f"(singleton)'); set `case_ids` to the full list of member case_ids "
            f"shown in that section's `**case_ids:**` line and `**members:**` "
            f"list (every case_id, do not omit any). Do not invent case_ids."
        )
    else:
        instructions = (
            f"\n\nPopulate run_id=\"{run_id}\" and classifier=\"{classifier}\" in "
            f"the output. Include exactly one result per failure shown above, "
            f"keyed by its **case_id** value. Do not invent case_ids."
        )
    return (
        f"## Failures to classify (batch {batch_number} of {total_batches})\n\n"
        + "".join(s.text for s in batch)
        + instructions
    )


def call_api(
    client: anthropic.Anthropic,
    system_header: str,
    batch: list,
    cfg: dict,
    classifier: str,
    run_id: str,
    batch_number: int,
    total_batches: int,
    api_schema: dict,
    mode: str = "per-test",
) -> tuple[list[dict], dict]:
    """Send one batch. Returns (parsed_results, usage_info).

    Structured outputs guarantee a single JSON-valid text block in the
    response. SDK retries rate limits / 5xx; we retry once on
    JSONDecodeError as belt-and-braces (shouldn't happen with structured
    outputs, but one batch of wasted credit is cheaper than a full rerun)."""
    user_text = _build_user_text(
        batch, batch_number, total_batches, classifier, run_id, mode=mode,
    )

    instructions = (_SYSTEM_INSTRUCTIONS_SUBTASK if mode == "by-subtask"
                    else _SYSTEM_INSTRUCTIONS)

    request_args = dict(
        model=cfg["model"],
        max_tokens=cfg["max_output_tokens"],
        system=[
            {"type": "text", "text": instructions},
            {
                "type": "text",
                "text": system_header,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": user_text}],
        output_config={
            "effort": cfg["effort"],
            "format": {"type": "json_schema", "schema": api_schema},
        },
    )

    last_err: Exception | None = None
    for attempt in range(2):
        with client.messages.stream(**request_args) as stream:
            final_message = stream.get_final_message()

        raw = next(
            (b.text for b in final_message.content if b.type == "text"),
            None,
        )
        if raw is None:
            raise SystemExit(
                f"batch {batch_number}: response contained no text block "
                f"(content types: {[b.type for b in final_message.content]})"
            )

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            last_err = e
            if attempt == 0:
                print(
                    f"  batch {batch_number}: JSON parse failed "
                    f"({e.msg} at char {e.pos}) — retrying once",
                    file=sys.stderr,
                )
                continue
            raise SystemExit(
                f"batch {batch_number} returned invalid JSON after one retry: {e}\n"
                f"First 400 chars of raw response:\n{raw[:400]}"
            )

        results = parsed.get("results") or []
        usage = {
            "input_tokens":                final_message.usage.input_tokens,
            "output_tokens":               final_message.usage.output_tokens,
            "cache_creation_input_tokens": getattr(
                final_message.usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens":     getattr(
                final_message.usage, "cache_read_input_tokens", 0) or 0,
        }
        return results, usage

    # Unreachable — loop either returns or raises.
    raise SystemExit(f"batch {batch_number}: retry loop exited without result ({last_err})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def classify(run_dir: Path, classifier: str, dry_run: bool) -> Path:
    prompt_md   = run_dir / "prompt.md"
    run_yml     = run_dir / "run.yml"
    schema_path = run_dir / "results.schema.json"
    for p in (prompt_md, run_yml, schema_path):
        if not p.exists():
            raise SystemExit(f"Missing required file: {p}")

    meta   = yaml.safe_load(run_yml.read_text())
    run_id = meta["run_id"]
    mode   = meta.get("mode") or "per-test"
    if mode not in ("per-test", "by-subtask"):
        raise SystemExit(f"Unknown mode in run.yml: {mode!r}")
    schema = json.loads(schema_path.read_text())
    api_schema = prepare_api_schema(schema)

    if mode == "by-subtask":
        header, sections = parse_prompt_subtask(prompt_md)
    else:
        header, sections = parse_prompt(prompt_md)
    if not sections:
        raise SystemExit(
            f"No {'subtask' if mode == 'by-subtask' else 'failure'} sections "
            f"found in prompt.md — nothing to classify. (All cases may be "
            f"pre-classified or flaky; check run.yml.)"
        )

    cfg = load_api_config()
    batches = pack_batches(sections, cfg["max_chars_per_batch"])

    unit = "subtasks" if mode == "by-subtask" else "failures"
    print(f"Run:          {run_id}")
    print(f"Classifier:   {classifier}")
    print(f"Mode:         {mode}")
    print(f"Model:        {cfg['model']}  (effort={cfg['effort']})")
    print(f"{unit.capitalize():14s}{len(sections)}"
          + (f"  (covering {sum(len(s.case_ids) for s in sections)} member case-results)"
             if mode == "by-subtask" else ""))
    print(f"Batches:      {len(batches)} "
          f"(max {cfg['max_chars_per_batch']:,} chars/batch)")

    if dry_run:
        batches_dir = run_dir / "batches"
        batches_dir.mkdir(exist_ok=True)
        active_instructions = (_SYSTEM_INSTRUCTIONS_SUBTASK if mode == "by-subtask"
                                else _SYSTEM_INSTRUCTIONS)
        for i, b in enumerate(batches, 1):
            total = sum(len(s.text) for s in b)
            print(f"  batch {i}: {len(b)} {unit}, {total:,} chars "
                  f"(~{total // 4:,} tokens)")
            user_text = _build_user_text(
                b, i, len(batches), classifier, run_id, mode=mode,
            )
            if mode == "by-subtask":
                ids_label = (f"subtask_ids: {[s.subtask_id for s in b]}; "
                              f"member_count: {sum(len(s.case_ids) for s in b)}")
            else:
                ids_label = f"case_ids: {[s.case_id for s in b]}"
            preview = (
                f"# Batch {i} of {len(batches)} — what would be sent to Anthropic\n\n"
                f"**Run:** `{run_id}` · **Mode:** `{mode}`\n"
                f"**Classifier:** `{classifier}`\n"
                f"**Model:** `{cfg['model']}` · effort=`{cfg['effort']}` · "
                f"max_tokens={cfg['max_output_tokens']}\n"
                f"**{unit.capitalize()} in this batch:** {len(b)} "
                f"({ids_label})\n"
                f"**Output schema:** validated against `results.schema.json` "
                f"(if/then stripped for API call, enforced post-hoc)\n\n"
                f"---\n\n"
                f"## System block 1 — instructions (not cached, ~{len(active_instructions)} chars)\n\n"
                f"{active_instructions}\n\n"
                f"---\n\n"
                f"## System block 2 — shared header (cached, ~{len(header):,} chars)\n\n"
                f"{header}\n\n"
                f"---\n\n"
                f"## User message — per-batch {unit} + output instructions "
                f"(~{len(user_text):,} chars)\n\n"
                f"{user_text}\n"
            )
            (batches_dir / f"batch_{i:02d}.md").write_text(preview, encoding="utf-8")
        print(f"\n--dry-run: wrote {len(batches)} batch preview file(s) to "
              f"{_disp(batches_dir)}/")
        print("Inspect the batch_*.md files to see what would be sent. "
              "No API calls made.")
        return run_dir / "results.json"

    client = build_client()

    all_results: list[dict] = []
    if mode == "by-subtask":
        # Track subtask_ids (None for unmapped singletons — track those by
        # the synthetic key of their first case_id since None is not unique)
        seen_subtask_ids: set[int] = set()
        seen_case_ids:    set[int] = set()
        expected_subtask_ids = {s.subtask_id for s in sections if s.subtask_id is not None}
        expected_case_ids    = {cid for s in sections for cid in s.case_ids}
    else:
        seen_ids: set[int] = set()
        expected_ids = {s.case_id for s in sections}

    usage_totals = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }

    for i, batch in enumerate(batches, 1):
        total = sum(len(s.text) for s in batch)
        print(f"\n→ batch {i}/{len(batches)}: {len(batch)} {unit} "
              f"({total:,} chars, ~{total // 4:,} tokens)")

        results, usage = call_api(
            client=client,
            system_header=header,
            batch=batch,
            cfg=cfg,
            classifier=classifier,
            run_id=run_id,
            batch_number=i,
            total_batches=len(batches),
            api_schema=api_schema,
            mode=mode,
        )

        if mode == "by-subtask":
            batch_subtask_ids = {s.subtask_id for s in batch if s.subtask_id is not None}
            batch_case_ids    = {cid for s in batch for cid in s.case_ids}
            for r in results:
                sid = r.get("subtask_id")
                cids = r.get("case_ids") or []
                if isinstance(sid, int) and sid in seen_subtask_ids:
                    print(f"  WARN: duplicate subtask_id={sid} — keeping first",
                          file=sys.stderr)
                    continue
                if isinstance(sid, int) and sid not in batch_subtask_ids:
                    print(f"  WARN: model emitted subtask_id={sid} "
                          f"(not in this batch) — dropping", file=sys.stderr)
                    continue
                # Validate every case_id in the entry is from this batch and
                # not yet claimed by another entry.
                bad = [c for c in cids if c not in batch_case_ids or c in seen_case_ids]
                if bad:
                    print(f"  WARN: subtask_id={sid} entry has invalid/duplicate "
                          f"case_ids={bad} — dropping entry", file=sys.stderr)
                    continue
                if isinstance(sid, int):
                    seen_subtask_ids.add(sid)
                seen_case_ids.update(cids)
                all_results.append(r)
        else:
            batch_ids = {s.case_id for s in batch}
            for r in results:
                cid = r.get("testray_case_id")
                if cid in seen_ids:
                    print(f"  WARN: duplicate testray_case_id={cid} — keeping first",
                          file=sys.stderr)
                    continue
                if cid not in batch_ids:
                    print(f"  WARN: model emitted unexpected testray_case_id={cid} "
                          f"(not in this batch) — dropping", file=sys.stderr)
                    continue
                seen_ids.add(cid)
                all_results.append(r)

        for k in usage_totals:
            usage_totals[k] += usage.get(k, 0)

        print(f"   returned {len(results)} rows "
              f"(in={usage['input_tokens']:,}, out={usage['output_tokens']:,}, "
              f"cache_read={usage['cache_read_input_tokens']:,})")

        if i < len(batches):
            time.sleep(cfg["delay_between"])

    if mode == "by-subtask":
        missing = expected_case_ids - seen_case_ids
        if missing:
            print(f"\nWARN: {len(missing)} member case_id(s) got no verdict from "
                  f"the model — they will be absent from results.json and "
                  f"submit.py will default them to NEEDS_REVIEW: "
                  f"{sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}",
                  file=sys.stderr)
    else:
        missing = expected_ids - seen_ids
        if missing:
            print(f"\nWARN: {len(missing)} failure(s) got no classification from "
                  f"the model — they will be absent from results.json and "
                  f"submit.py will default them to NEEDS_REVIEW: "
                  f"{sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}",
                  file=sys.stderr)

    payload = {
        "run_id":     run_id,
        "classifier": classifier,
        "results":    all_results,
    }

    # Validate against the ORIGINAL schema (with if/then) so the
    # BUG→culprit_file invariant is enforced post-hoc.
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as e:
        raw_path = run_dir / "results.api.raw.json"
        raw_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        raise SystemExit(
            f"API output failed schema validation: {e.message}\n"
            f"Raw payload written to {raw_path} for inspection."
        )

    out = run_dir / "results.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"Wrote {_disp(out)}")
    print(f"Classified: {len(all_results)} / {len(sections)} failures")
    print(f"Tokens:     in={usage_totals['input_tokens']:,} "
          f"out={usage_totals['output_tokens']:,} "
          f"cache_created={usage_totals['cache_creation_input_tokens']:,} "
          f"cache_read={usage_totals['cache_read_input_tokens']:,}")
    if len(batches) > 1 and usage_totals["cache_read_input_tokens"] == 0:
        print("NOTE: cache_read_input_tokens=0 across a multi-batch run — "
              "shared header is below the model's 4096-token cacheable "
              "minimum, so caching did not activate.", file=sys.stderr)
    print(f"Next:       testray-analysis submit {_disp(out.parent)}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Classify a prepared triage bundle via the Anthropic API. "
                    "Writes results.json into <run_dir> for submit.py to pick up.",
    )
    ap.add_argument("run_dir", type=Path,
                    help="Path to runs/r_<id>/")
    ap.add_argument("--classifier", default=DEFAULT_CLASSIFIER,
                    help=f"Classifier label written into results.json "
                         f"(default: {DEFAULT_CLASSIFIER})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse + batch the bundle and print the plan, but "
                         "make no API calls.")
    args = ap.parse_args()

    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"Not a directory: {run_dir}")

    classify(run_dir, classifier=args.classifier, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
