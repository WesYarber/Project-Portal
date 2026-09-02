"""Price a run from its token counts, for the runs the CLI never priced.

A watched run's cost comes from the CLI's `result` event (`total_cost_usd`),
computed by the CLI from its own price table. A run that outlived a portal
restart has no result event - its report is read back out of the transcript
(app/transcript.py) - but the transcript carries every API call's `usage`, so
the same figure can be rebuilt here from the token counts and the model's list
prices.

The table below is Anthropic's first-party API pricing per million tokens.
Cache writes are priced by their lifetime: a 5-minute entry costs 1.25x the
input rate and a 1-hour entry 2x, and the CLI's usage records split
`cache_creation_input_tokens` into `cache_creation.ephemeral_5m_input_tokens`
and `ephemeral_1h_input_tokens` so that split is priced exactly. A usage record
without the split (an older CLI) is priced as 5-minute writes.

Calibrated on 2026-09-02 against six watched Fable 5.1 runs: the estimate
landed within 1% of the CLI's own figure on every one (the CLI adds a constant
few cents per run that the transcript does not carry). A model not in the
table prices as None - a blank on the run page is honest, a guess is not.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class Rates:
    """Dollars per million tokens."""
    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float


def _standard(input_rate: float, output_rate: float) -> Rates:
    """The usual ratios: cache reads at a tenth of input, 5-minute cache writes
    at 1.25x, 1-hour writes at 2x."""
    return Rates(
        input=input_rate,
        output=output_rate,
        cache_read=input_rate * 0.1,
        cache_write_5m=input_rate * 1.25,
        cache_write_1h=input_rate * 2.0,
    )


# Exact model ids first. A dated variant (`claude-opus-4-6-20260212`) resolves
# to the longest key it starts with, see `rates_for`.
RATES: dict[str, Rates] = {
    # Fable 5.1 reads its cache at a flat $0.25/MTok, not the tenth-of-input the
    # rest of the family uses - the calibration above only fits at that rate.
    "claude-fable-5-1": Rates(10.0, 50.0, 0.25, 12.5, 20.0),
    "claude-mythos-5-1": Rates(10.0, 50.0, 0.25, 12.5, 20.0),
    "claude-fable-5": _standard(10.0, 50.0),
    "claude-mythos-5": _standard(10.0, 50.0),
    "claude-opus-5": _standard(5.0, 25.0),
    "claude-opus-4-8": _standard(5.0, 25.0),
    "claude-opus-4-7": _standard(5.0, 25.0),
    "claude-opus-4-6": _standard(5.0, 25.0),
    "claude-opus-4-5": _standard(5.0, 25.0),
    "claude-opus-4-1": _standard(15.0, 75.0),
    "claude-opus-4": _standard(15.0, 75.0),
    "claude-sonnet-5": _standard(2.0, 10.0),
    "claude-sonnet-4-6": _standard(3.0, 15.0),
    "claude-sonnet-4-5": _standard(3.0, 15.0),
    "claude-sonnet-4": _standard(3.0, 15.0),
    "claude-haiku-4-5": _standard(1.0, 5.0),
    "claude-3-5-haiku": _standard(0.8, 4.0),
}

# The fields of a usage record this module reads, and the `Usage` totals it
# keeps. `cache_write` is the undivided figure; the two lifetimes sum to it
# when the CLI recorded the split.
USAGE_KEYS = ("input", "output", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h")


def rates_for(model: Optional[str]) -> Optional[Rates]:
    """The price row for a model id, or None for one the table does not know.

    A dated or suffixed id (`claude-opus-4-6-20260212`) prices as its family;
    the longest matching key wins so `claude-opus-4-6` is not read as
    `claude-opus-4`."""
    if not model:
        return None
    name = str(model).strip().lower()
    if name in RATES:
        return RATES[name]
    best = None
    for key in RATES:
        if name.startswith(key + "-") and (best is None or len(key) > len(best)):
            best = key
    return RATES[best] if best else None


def totals_from_usage(usage: Optional[Mapping]) -> dict[str, int]:
    """The token counts of one API call, in this module's names. Missing
    fields read as zero; a record without the cache-lifetime split has all of
    its cache writes under `cache_write` and neither lifetime."""
    usage = usage or {}
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), Mapping) else {}
    return {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_write_5m": int(split.get("ephemeral_5m_input_tokens") or 0),
        "cache_write_1h": int(split.get("ephemeral_1h_input_tokens") or 0),
    }


def billable(totals: Mapping[str, int]) -> int:
    """Tokens that cost anything. A synthetic message (the CLI writes one with
    model `<synthetic>` when it fills a turn in itself) has none, and must not
    make a run unpriceable."""
    return sum(int(totals.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write"))


def price(model: Optional[str], totals: Mapping[str, int]) -> Optional[float]:
    """Dollars for one model's totals; None when the model is not priced and
    the totals are not empty."""
    if not billable(totals):
        return 0.0
    rates = rates_for(model)
    if rates is None:
        return None
    write_5m = int(totals.get("cache_write_5m") or 0)
    write_1h = int(totals.get("cache_write_1h") or 0)
    unsplit = int(totals.get("cache_write") or 0) - write_5m - write_1h
    if unsplit > 0:
        # No lifetime recorded for these: the CLI's default cache is the
        # 5-minute one.
        write_5m += unsplit
    dollars = (
        int(totals.get("input") or 0) * rates.input
        + int(totals.get("output") or 0) * rates.output
        + int(totals.get("cache_read") or 0) * rates.cache_read
        + write_5m * rates.cache_write_5m
        + write_1h * rates.cache_write_1h
    )
    return dollars / 1_000_000


def estimate(usage_by_model: Mapping[str, Mapping[str, int]]) -> Optional[float]:
    """The cost of a run from its per-model totals. None the moment any model
    that spent tokens is missing from the table: a partial sum would read as
    the whole cost."""
    total = 0.0
    for model, totals in usage_by_model.items():
        part = price(model, totals)
        if part is None:
            return None
        total += part
    return total
