"""Regression triage analysis.

Classifies new/changed failures between two Testray builds
(BUG / POSSIBLE_BUG / TEST_FIX / NEEDS_REVIEW / FALSE_POSITIVE) and writes
TriageResult verdicts back to Testray over REST.

Pipeline: prepare -> classify -> submit  (see cli.py).
"""
