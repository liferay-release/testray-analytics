#!/usr/bin/env python3
"""Build a triage worklist from a Testray build-comparison run directory.

Does the three things that are pure bookkeeping, so the analysis time goes to
judgement instead:

  1. Buckets every failure group by regression signal (transition).
  2. Clusters groups by candidate ticket, by spec/test file, and by shared error.
  3. Cross-references each group against the test-side files the range changed.

Usage:
    python3 triage_setup.py <run_dir> [--repo PATH] [--scope CLASS,CLASS] [--json OUT]
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

csv.field_size_limit(10 ** 7)

# Where test-side (as opposed to product) code lives in liferay-portal.
TEST_PATHS = [
    "modules/test/playwright/tests",
    "modules/test/playwright/utils",
    "modules/test/playwright/pages",
    "modules/test/playwright/fixtures",
    "portal-web/test/functional",
]

# Transitions that carry a real "this build broke it" signal.
SIGNAL = {"new"}
PRE_EXISTING = {"changed", "same_failure"}


def read_run_yml(run_dir):
    """run.yml is flat key: value; avoid a PyYAML dependency."""
    meta = {}
    path = os.path.join(run_dir, "run.yml")
    if os.path.exists(path):
        for line in open(path):
            m = re.match(r"^([a-z_]+):\s*(\S.*)$", line)
            if m:
                meta[m.group(1)] = m.group(2).strip()
    return meta


def split_ids(raw):
    return [int(x) for x in re.split(r"[|,]", raw or "") if x.strip().isdigit()]


def load(run_dir, scope):
    results = json.load(open(os.path.join(run_dir, "results.json")))["results"]
    groups = {
        r["group_id"]: r for r in results
        if not scope or r["classification"] in scope
    }

    subtasks = {}
    with open(os.path.join(run_dir, "diff_list_subtasks.csv")) as f:
        for row in csv.DictReader(f):
            subtasks[int(row["group_id"])] = row

    case_to_group = {}
    for gid in groups:
        for cid in split_ids(subtasks.get(gid, {}).get("member_case_ids", "")):
            case_to_group[cid] = gid

    cases = defaultdict(list)
    per_file = defaultdict(list)  # spec/testcase basename -> [case rows]
    with open(os.path.join(run_dir, "diff_list.csv")) as f:
        for row in csv.DictReader(f):
            name = row["test_case"]
            per_file[test_file_of(name)].append(row)
            cid = int(row["testray_case_id"])
            if cid in case_to_group:
                cases[case_to_group[cid]].append(row)

    return results, groups, subtasks, cases, per_file


def test_file_of(test_case):
    """Map a Testray test name to the file that defines it."""
    m = re.search(r"([\w.\-]+\.spec\.ts)", test_case or "")
    if m:
        return m.group(1)
    m = re.match(r"LocalFile\.(\w+)#", test_case or "")
    if m:
        return m.group(1) + ".testcase"
    return None


def changed_test_files(repo, base, target):
    if not (repo and base and target):
        return None
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base}..{target}", "--"] + TEST_PATHS,
            cwd=repo, capture_output=True, text=True, timeout=120,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return {os.path.basename(p): p for p in out.stdout.split() if p}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--scope", default="POSSIBLE_BUG,NEEDS_REVIEW",
                    help="classifications to work; 'ALL' for everything")
    ap.add_argument("--json", help="also write the worklist as JSON")
    args = ap.parse_args()

    scope = set() if args.scope.upper() == "ALL" else set(args.scope.split(","))
    meta = read_run_yml(args.run_dir)
    base, target = meta.get("git_hash_a"), meta.get("git_hash_b")
    results, groups, subtasks, cases, per_file = load(args.run_dir, scope)

    print(f"run        {meta.get('run_id', '?')}  branch {meta.get('base_branch', '?')}")
    print(f"range      {(base or '?')[:12]}..{(target or '?')[:12]}")
    print(f"all groups {len(results)}  {dict(Counter(r['classification'] for r in results))}")
    print(f"in scope   {len(groups)} ({args.scope})\n")

    changed = changed_test_files(args.repo, base, target)
    if changed is None:
        print("!! could not diff the range in --repo; test-side sweep skipped.")
        print("!! fetch/deepen the branch first -- this sweep is the cheapest")
        print("!! way to spot test fixes, so do not skip it silently.\n")
    else:
        print(f"test-side files changed in range: {len(changed)}\n")

    worklist = []
    for gid, g in sorted(groups.items()):
        st = subtasks.get(gid, {})
        rows = cases.get(gid, [])
        trans = Counter(r["transition"] for r in rows)
        signal = "SIGNAL" if SIGNAL & set(trans) else (
            "pre-existing" if set(trans) & PRE_EXISTING else "no-signal")

        tfile = test_file_of(st.get("member_test_cases", ""))
        tchanged = bool(changed and tfile and tfile in changed)

        # Whole-file check: every failing case in this file, not just this group.
        siblings = per_file.get(tfile, []) if tfile else []
        sib_failed = [r for r in siblings if r["status_b"] == "FAILED"]
        whole_file = len(sib_failed) > 1

        tickets = sorted({c["ticket"] for c in (g.get("candidates") or []) if c.get("ticket")})
        worklist.append(dict(
            group_id=gid, classification=g["classification"], signal=signal,
            test_file=tfile, test_file_changed=tchanged,
            failing_in_file=len(sib_failed), whole_file=whole_file,
            tickets=tickets, components=st.get("components", ""),
            tests=st.get("member_test_cases", ""),
            error=(st.get("shared_error") or "")[:200],
        ))

    order = {"SIGNAL": 0, "pre-existing": 1, "no-signal": 2}
    worklist.sort(key=lambda w: (order[w["signal"]], not w["test_file_changed"], w["group_id"]))

    print("=" * 100)
    print("WORKLIST  (signal first; TEST-SIDE CHANGED means triage as a test fix first)")
    print("=" * 100)
    for w in worklist:
        flags = []
        if w["test_file_changed"]:
            flags.append("TEST-SIDE CHANGED")
        if w["whole_file"]:
            flags.append(f"whole-file:{w['failing_in_file']} failing")
        print(f"G{w['group_id']:<5d} {w['signal']:<12s} {','.join(w['tickets']) or '-':<24s} "
              f"{(w['test_file'] or '?'):<42s} {' | '.join(flags)}")

    print("\n" + "=" * 100)
    print("CLUSTERS  (one analysis per cluster, not per group)")
    print("=" * 100)
    for label, key in (("ticket", "tickets"), ("test file", "test_file")):
        buckets = defaultdict(list)
        for w in worklist:
            for v in (w[key] if key == "tickets" else [w[key]]):
                if v:
                    buckets[v].append(w["group_id"])
        multi = {k: v for k, v in buckets.items() if len(v) > 1}
        print(f"\nby {label}: {len(multi)} clusters covering "
              f"{len(set().union(*multi.values())) if multi else 0} groups")
        for k, v in sorted(multi.items(), key=lambda kv: -len(kv[1])):
            print(f"  {k:<46s} x{len(v):<3d} {v}")

    sig = [w for w in worklist if w["signal"] == "SIGNAL"]
    tside = [w for w in sig if w["test_file_changed"]]
    print("\n" + "=" * 100)
    print(f"{len(sig)} of {len(worklist)} in-scope groups carry a regression signal.")
    print(f"{len(tside)} of those had their own test file changed in the range -> start there.")
    print("Groups with no signal are pre-existing or unbaselined; say so rather than")
    print("hunting a culprit for them.")

    if args.json:
        json.dump(worklist, open(args.json, "w"), indent=2)
        print(f"\nworklist written to {args.json}")


if __name__ == "__main__":
    sys.exit(main())
