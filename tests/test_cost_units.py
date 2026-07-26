"""How the CLI's cost figure is rendered.

Wes is on a Max subscription, so `total_cost_usd` is a notional API-rate figure
rather than money he owes. The default rendering keeps the magnitude but drops
the dollar sign; dollars stay available as an explicit choice.
"""
from __future__ import annotations

import pytest

from app import db, usage


def test_default_units_are_weight():
    assert usage.cost_units() == "weight"
    assert usage.cost_noun() == "weight"
    assert usage.format_cost(0.4213) == "0.421w"


def test_usd_units_render_dollars():
    db.set_setting("cost_units", "usd")
    assert usage.cost_units() == "usd"
    assert usage.cost_noun() == "cost"
    assert usage.format_cost(0.4213) == "$0.421"


def test_unknown_setting_falls_back_to_the_default():
    db.set_setting("cost_units", "doubloons")
    assert usage.cost_units() == "weight"
    assert usage.format_cost(1.5) == "1.500w"


def test_precision_is_caller_controlled():
    assert usage.format_cost(16.4231, precision=2) == "16.42w"
    assert usage.format_cost(16.4231, precision=2, units="usd") == "$16.42"


@pytest.mark.parametrize("value", [None, "", "abc", object()])
def test_unpriced_runs_render_as_a_dash_not_zero(value):
    # Older runs predate cost recording. Rendering them as 0 would claim they
    # were free, which is a different statement from "we don't know".
    assert usage.format_cost(value) == "-"


def test_units_argument_overrides_the_setting():
    db.set_setting("cost_units", "usd")
    assert usage.format_cost(2.0, units="weight") == "2.000w"
    assert usage.cost_noun("weight") == "weight"
