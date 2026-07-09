#!/usr/bin/env python3
"""
extract_relevant_hunks.py

Extracts only the diff hunks for files that matched failing test cases,
reducing a large git diff to a triage-relevant subset.

Usage:
    python extract_relevant_hunks.py <diff_file> <matched_files> [options]

Arguments:
    diff_file       Path to the full git diff (or '-' for stdin)
    matched_files   What to match against. Accepts:
                      - triage CSV:        triage_results.csv  (auto-detected; parses Likely Cause)
                      - plain list:        matched_files.txt   (one name/path per line)
                      - stdin:             -

Matching modes (default: --exact):
    --exact         Match only files whose full b/ path ends with the given
                    filename or path fragment. Safest -- avoids false positives
                    from common substrings like "Document" or "Actions".
                    "Document.java" matches only /path/to/Document.java.
                    "calendar-web/bnd.bnd" matches only that specific bnd.bnd.

    --fuzzy         Match any file whose full path contains the fragment as a
                    substring. Use when exact matching misses files.

    --auto          Exact first. If a fragment matches nothing, fall back to
                    fuzzy for that fragment only and warn. Best of both.

Options:
    --output, -o    Write output to this file (default: stdout)
    --stats         Print match statistics to stderr
    --min-lines N   Only include files with >= N changed lines (default: 1)
    --unmatched     List fragments that matched nothing (implies --stats)

Examples:
    # Recommended: exact mode from triage CSV (safest, smallest output)
    python extract_relevant_hunks.py git_diff.md triage_results.csv -o filtered.md --stats

    # Auto mode: exact first, fuzzy fallback for anything that matches nothing
    python extract_relevant_hunks.py git_diff.md triage_results.csv -o filtered.md --auto --stats

    # Fuzzy mode (may over-match on short/common names)
    python extract_relevant_hunks.py git_diff.md matched_files.txt -o filtered.md --fuzzy

    # Inline list via shell
    printf "ObjectEntryFolder.java\nStagingImpl.java" |
        python extract_relevant_hunks.py git_diff.md - -o filtered.md

    # Check what was missed after extraction
    python extract_relevant_hunks.py git_diff.md triage_results.csv --auto --unmatched
"""

import argparse
import csv
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Parsing matched_files input
# ---------------------------------------------------------------------------

def parse_matched_files(path_str):
    """
    Returns an ordered, deduplicated list of lowercase name/path fragments.

    CSV mode (auto-detected by 'Classification' in header):
      - Reads the 'Likely Cause' column
      - Splits on ' / ' (whitespace-slash-whitespace) to separate distinct files
        e.g. "BasicLayout.java / GridViewport.java" -> two fragments
        e.g. "calendar-web/bnd.bnd" -> one fragment (path not split)
      - Strips backticks and parenthetical qualifiers like "(headless/delivery)"

    Plain-list mode:
      - One filename or path fragment per line
      - Lines starting with '#' are skipped (comments)
    """
    if path_str == "-":
        raw = sys.stdin.read()
        lines = raw.splitlines()
        is_csv = bool(lines) and "Classification" in lines[0]
    else:
        p = Path(path_str)
        if not p.exists():
            sys.exit(f"[ERROR] matched_files not found: {path_str}")
        raw = p.read_text(encoding="utf-8")
        lines = raw.splitlines()
        is_csv = path_str.endswith(".csv") or (bool(lines) and "Classification" in lines[0])

    seen = set()
    fragments = []

    def add(frag):
        # Strip wrapping whitespace and backticks
        frag = frag.strip().strip("`").strip()
        # Drop parenthetical qualifiers e.g. "(headless/delivery)"
        frag = re.sub(r"\s*\(.*?\)", "", frag).strip()
        frag = frag.lower()
        if frag and frag not in seen:
            seen.add(frag)
            fragments.append(frag)

    if is_csv:
        reader = csv.DictReader(lines)
        for row in reader:
            cause = row.get("Likely Cause", "")
            # Split ONLY on ' / ' (space-slash-space) to preserve path fragments
            # like "calendar-web/bnd.bnd" intact.
            for part in re.split(r"\s+/\s+", cause):
                add(part)
    else:
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                add(line)

    return fragments


# ---------------------------------------------------------------------------
# Matching strategies
# ---------------------------------------------------------------------------

def exact_match(diff_path, fragment):
    """
    True if diff_path (lowercased) equals fragment OR ends with '/' + fragment.

    Handles:
      "Document.java"           -> matches .../v1_0/Document.java
      "dto/v1_0/Document.java"  -> matches .../headless/dto/v1_0/Document.java
      "calendar-web/bnd.bnd"    -> matches only that module's bnd.bnd

    Does NOT match:
      "Document.java" against "DocumentHelper.java"  (different basename)
      "bnd.bnd"       against every bnd.bnd in the repo  (too broad)
    """
    lower = diff_path.lower()
    frag = fragment.lower()
    return lower == frag or lower.endswith("/" + frag)


def fuzzy_match(diff_path, fragment):
    """True if fragment appears anywhere in diff_path (case-insensitive)."""
    return fragment.lower() in diff_path.lower()


def build_matcher(mode, fragments, all_diff_paths, stats_sink):
    """
    Pre-scans all_diff_paths against each fragment under the chosen strategy.
    Returns:
      check_fn(diff_path) -> bool    used during extraction
      matched_by dict                fragment -> [matched paths]  (for reporting)
    """
    matched_by = {f: [] for f in fragments}

    if mode == "exact":
        for dp in all_diff_paths:
            for frag in fragments:
                if exact_match(dp, frag):
                    matched_by[frag].append(dp)

        def check(dp):
            return any(exact_match(dp, f) for f in fragments)

    elif mode == "fuzzy":
        for dp in all_diff_paths:
            for frag in fragments:
                if fuzzy_match(dp, frag):
                    matched_by[frag].append(dp)

        def check(dp):
            return any(fuzzy_match(dp, f) for f in fragments)

    else:  # --auto
        exact_hit = set()
        for dp in all_diff_paths:
            for frag in fragments:
                if exact_match(dp, frag):
                    matched_by[frag].append(dp)
                    exact_hit.add(frag)

        fuzzy_frags = [f for f in fragments if f not in exact_hit]
        fuzzy_hit = set()
        for dp in all_diff_paths:
            for frag in fuzzy_frags:
                if fuzzy_match(dp, frag):
                    matched_by[frag].append(dp)
                    fuzzy_hit.add(frag)

        if fuzzy_hit and stats_sink:
            print(
                f"[AUTO] {len(fuzzy_hit)} fragment(s) had no exact match -- fell back to fuzzy:",
                file=stats_sink,
            )
            for f in sorted(fuzzy_hit):
                print(f"       !  {f}  ({len(matched_by[f])} fuzzy hit(s))", file=stats_sink)

        def check(dp):
            for f in fragments:
                if f in exact_hit and exact_match(dp, f):
                    return True
                if f in fuzzy_hit and fuzzy_match(dp, f):
                    return True
            return False

    return check, matched_by


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_hunks(diff_lines, check_fn, min_lines=1):
    """
    Yields (diff_path, header_lines, hunk_lines, changed_count) for each file
    block in diff_lines whose b/ path passes check_fn and has >= min_lines
    changed lines.
    """
    i = 0
    n = len(diff_lines)

    while i < n:
        line = diff_lines[i]

        if not line.startswith("diff --git "):
            i += 1
            continue

        header = [line]
        i += 1
        diff_path = ""

        m = re.match(r"diff --git a/(.+) b/(.+)", line)
        if m:
            diff_path = m.group(2)

        # Consume header lines (index, ---, +++) until first @@ or next diff block
        while i < n and not diff_lines[i].startswith("@@") and not diff_lines[i].startswith("diff --git "):
            hline = diff_lines[i]
            header.append(hline)
            if hline.startswith("+++ b/"):
                diff_path = hline[6:].strip()
            elif hline.startswith("+++ /dev/null"):
                diff_path = "/dev/null"
            i += 1

        # Skip deleted files and non-matching files
        if not diff_path or diff_path == "/dev/null" or not check_fn(diff_path):
            while i < n and not diff_lines[i].startswith("diff --git "):
                i += 1
            continue

        # Collect all hunk lines for this file
        hunk_lines = []
        changed = 0

        while i < n and not diff_lines[i].startswith("diff --git "):
            hunk_line = diff_lines[i]
            hunk_lines.append(hunk_line)
            if hunk_line.startswith("+") and not hunk_line.startswith("+++"):
                changed += 1
            elif hunk_line.startswith("-") and not hunk_line.startswith("---"):
                changed += 1
            i += 1

        if changed >= min_lines:
            yield diff_path, header, hunk_lines, changed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract triage-relevant hunks from a large git diff.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("diff_file",
                        help="Path to full git diff file (or '-' for stdin)")
    parser.add_argument("matched_files",
                        help="Triage CSV, plain filename list, or '-' for stdin")
    parser.add_argument("--output", "-o", default=None,
                        help="Output file path (default: stdout)")
    parser.add_argument("--stats", action="store_true",
                        help="Print match statistics to stderr")
    parser.add_argument("--unmatched", action="store_true",
                        help="List fragments that matched nothing (implies --stats)")
    parser.add_argument("--min-lines", type=int, default=1, metavar="N",
                        help="Only include files with >= N changed lines (default: 1)")

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--exact", dest="mode", action="store_const", const="exact",
                            help="Match files ending with the fragment -- safest (default)")
    mode_group.add_argument("--fuzzy", dest="mode", action="store_const", const="fuzzy",
                            help="Match files containing the fragment as a substring")
    mode_group.add_argument("--auto", dest="mode", action="store_const", const="auto",
                            help="Exact first; fuzzy fallback for zero-hit fragments only")
    parser.set_defaults(mode="exact")

    args = parser.parse_args()

    if args.unmatched:
        args.stats = True

    if args.diff_file == "-" and args.matched_files == "-":
        sys.exit("[ERROR] diff_file and matched_files cannot both be stdin.")

    # Load fragments
    fragments = parse_matched_files(args.matched_files)
    if not fragments:
        sys.exit("[ERROR] No filenames parsed from matched_files input.")

    stats_sink = sys.stderr if args.stats else None

    if args.stats:
        print(f"[INFO] Mode         : --{args.mode}", file=sys.stderr)
        print(f"[INFO] Fragments    : {len(fragments)}", file=sys.stderr)

    # Load diff
    if args.diff_file == "-":
        diff_lines = sys.stdin.read().splitlines()
    else:
        p = Path(args.diff_file)
        if not p.exists():
            sys.exit(f"[ERROR] diff_file not found: {args.diff_file}")
        diff_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()

    if args.stats:
        print(f"[INFO] Diff lines   : {len(diff_lines):,}", file=sys.stderr)

    # Pre-scan diff paths for matcher build
    all_diff_paths = []
    for line in diff_lines:
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/.+ b/(.+)", line)
            if m:
                all_diff_paths.append(m.group(1))

    if args.stats:
        print(f"[INFO] Files in diff: {len(all_diff_paths):,}", file=sys.stderr)

    # Build matcher
    check_fn, matched_by = build_matcher(args.mode, fragments, all_diff_paths, stats_sink)

    # Extract
    results = list(extract_hunks(diff_lines, check_fn, args.min_lines))

    # Print stats
    if args.stats:
        total_changed = sum(r[3] for r in results)
        print(f"[INFO] Output files : {len(results)} ({total_changed:,} changed lines)", file=sys.stderr)
        for diff_path, _, _, changed in results:
            print(f"       + {diff_path}  ({changed:,} changed lines)", file=sys.stderr)

    if args.unmatched:
        no_hits = [f for f, hits in matched_by.items() if not hits]
        if no_hits:
            print(f"[WARN] {len(no_hits)} fragment(s) matched nothing:", file=sys.stderr)
            for f in sorted(no_hits):
                print(f"       x {f}", file=sys.stderr)
        else:
            print("[INFO] All fragments matched at least one file.", file=sys.stderr)

    # Build and write output
    out_lines = []
    for _, header, hunk_lines, _ in results:
        out_lines.extend(header)
        out_lines.extend(hunk_lines)
        out_lines.append("")

    output_text = "\n".join(out_lines)

    if args.output:
        Path(args.output).write_text(output_text, encoding="utf-8")
        if args.stats:
            orig_chars = sum(len(l) + 1 for l in diff_lines)
            out_chars = len(output_text)
            pct = 100.0 * out_chars / max(1, orig_chars)
            print(
                f"[INFO] Written      : {args.output} "
                f"({len(out_lines):,} lines / {out_chars:,} chars / ~{pct:.1f}% of original)",
                file=sys.stderr,
            )
    else:
        print(output_text)


if __name__ == "__main__":
    main()
