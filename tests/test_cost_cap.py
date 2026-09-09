"""
Tests for the per-run cost cap.

Triage runs unattended: the scanner queues work nobody sized in advance, and a
wide build pair can carry hundreds of clusters. So one run must not be able to
spend an unbounded amount, and the guard has to hold in the two ways it can be
wrong:

  - an over-cap run must be refused BEFORE anything is sent, and the refusal
    must say the number, the limit and what to do instead;
  - an under-estimate must still be caught mid-run, from measured spend, with
    the batches already paid for kept rather than discarded.

The pricing here is the first-party Anthropic rate card. If a model's price
changes, this file is where it should fail.
"""

import pytest

from testray_analytics.analysis import classify as C


class _Section:
    """Stands in for a batch section: only its text length matters here."""

    def __init__(self, chars):
        self.text = "x" * chars


def batches(*sizes):
    return [[_Section(n)] for n in sizes]


# --- pricing ---------------------------------------------------------------

def test_opus_is_priced_at_the_published_rate():
    """$5 in / $25 out per million tokens."""
    assert C.cost_of("claude-opus-4-8", input_tokens=1_000_000) == pytest.approx(5.00)
    assert C.cost_of("claude-opus-4-8", output_tokens=1_000_000) == pytest.approx(25.00)


def test_cheaper_models_are_priced_lower():
    assert C.cost_of("claude-sonnet-5", input_tokens=1_000_000) == pytest.approx(2.00)
    assert C.cost_of("claude-haiku-4-5", input_tokens=1_000_000) == pytest.approx(1.00)


def test_cache_reads_cost_a_tenth_of_input():
    """Used by the mid-run guard, which prices REAL usage from the API
    response. Note this is not evidence the header is cheap in practice: on the
    run with a known invoice it plainly was not — see the estimate section."""
    assert C.cost_of("claude-opus-4-8",
                     cache_read_tokens=1_000_000) == pytest.approx(0.50)


def test_an_unknown_model_is_priced_as_the_most_expensive_tier():
    """A cap that under-prices an unrecognised model is not a cap."""
    unknown = C.cost_of("claude-something-new-9", input_tokens=1_000_000)
    known_max = max(C.cost_of(m, input_tokens=1_000_000) for m in C.MODEL_PRICES)
    assert unknown >= known_max


# --- the estimate ----------------------------------------------------------
#
# Calibrated against the single run with a known invoice: release routine 82964,
# bundle r_20260904T220132Z — 21 calls, 40.72 MB of prompt sent, $200 billed.
#
# The estimate this replaced put that run at $6.61. It priced the shared header
# once and assumed cache reads afterwards, when in fact the header is re-sent on
# every call and is 96% of the bytes. These tests exist to stop that returning.

CFG = {"model": "claude-opus-4-8", "max_output_tokens": 16_000,
       "max_cost_usd": 15.0}

MB = 1024 * 1024
KNOWN_HEADER_CHARS = 1_870_127     # measured from that run's batch previews
KNOWN_CALLS = 21
KNOWN_INVOICE = 200.0


def test_the_known_invoice_is_reconstructed():
    """The one hard data point. Within 10% or the rate is wrong."""
    estimate = C.estimate_cost(batches(*([70_000] * KNOWN_CALLS)),
                               header_chars=KNOWN_HEADER_CHARS,
                               instruction_chars=6_000, cfg=CFG)
    assert estimate == pytest.approx(KNOWN_INVOICE, rel=0.10)


def test_the_header_is_charged_on_every_call():
    """The whole reason the old estimate was 30x low.

    Ten calls with a 2 MB header cost ten headers, not one plus nine cheap
    cache reads.
    """
    one = C.bytes_sent(batches(1_000), 2 * MB, 0)
    ten = C.bytes_sent(batches(*([1_000] * 10)), 2 * MB, 0)
    assert ten == pytest.approx(one * 10, rel=0.01)


def test_smaller_batches_cost_more_not_less():
    """Counter-intuitive and worth pinning: lowering max_chars_per_batch packs
    fewer clusters per call, and every extra call pays the header again. The
    run whose invoice we know was $200 at 21 calls; the same content in 9 calls
    would have been well under half that."""
    same_body = 1_400_000
    few = C.estimate_cost(batches(*([same_body // 4] * 4)),
                          KNOWN_HEADER_CHARS, 6_000, CFG)
    many = C.estimate_cost(batches(*([same_body // 21] * 21)),
                           KNOWN_HEADER_CHARS, 6_000, CFG)
    assert many > few * 2


def test_a_stable_sized_run_is_cheap():
    """Stable's fixed prompt is ~54 KB, not 1.87 MB — the routine this job
    actually runs against costs cents, and the cap must not interfere."""
    stable = C.estimate_cost(batches(40_000), header_chars=54 * 1024,
                             instruction_chars=6_000, cfg=CFG)
    assert stable < 1.0


def test_a_release_sized_run_is_refused_by_the_default_cap():
    """The accident the cap exists for: pointing this at routine 82964."""
    release = C.estimate_cost(batches(*([70_000] * KNOWN_CALLS)),
                              KNOWN_HEADER_CHARS, 6_000, CFG)
    assert release > C.DEFAULT_MAX_COST_USD


def test_an_empty_run_costs_nothing():
    assert C.estimate_cost([], 50_000, 5_000, CFG) == 0.0


def test_the_rate_is_overridable_without_a_code_change():
    doubled = C.estimate_cost(batches(100_000), 50_000, 5_000,
                              {**CFG, "usd_per_mb_sent": C.USD_PER_MB_SENT * 2})
    base = C.estimate_cost(batches(100_000), 50_000, 5_000, CFG)
    assert doubled == pytest.approx(base * 2)


# --- the cap ---------------------------------------------------------------

def test_the_default_limit_is_fifteen_dollars():
    assert C.DEFAULT_MAX_COST_USD == 15.0


def test_the_limit_can_be_raised_deliberately_for_one_command(monkeypatch):
    monkeypatch.setenv("TRIAGE_MAX_COST_USD", "40")
    assert C.load_api_config()["max_cost_usd"] == 40.0


def test_the_limit_can_be_lowered_by_a_ci_job(monkeypatch):
    monkeypatch.setenv("TRIAGE_MAX_COST_USD", "5")
    assert C.load_api_config()["max_cost_usd"] == 5.0


def test_the_refusal_names_the_cost_the_limit_and_the_way_out():
    """Someone reading this in a Jenkins console must not have to ask anyone
    what happened or what to do."""
    message = C.over_cap_message(23.4, 15.0, "runs/r_x")

    assert "$23.40" in message
    assert "$15.00" in message
    assert "Nothing was sent" in message
    assert "fork the repo and run it locally" in message
    assert "TRIAGE_MAX_COST_USD" in message
    assert "runs/r_x" in message
