from __future__ import annotations

import pytest

from spikes.dualtrack_nautilus_gold_parity_fixture import SCENARIOS, _exit_event, _scenario


def test_parity_fixture_declares_both_market_directions_with_conservative_stop_bars() -> None:
    assert set(SCENARIOS) == {
        "long_stop",
        "short_stop",
        "long_target",
        "short_target",
        "long_same_bar_stop_first",
        "limit_entry_waits_for_touch",
        "scale_in_weighted_average",
        "partial_reduction_then_close",
        "duplicate_command_event_replay",
        "restart_replay_and_reconciliation",
    }
    assert SCENARIOS["long_stop"]["side"] == "buy"
    assert SCENARIOS["long_stop"]["exit_bar"]["low"] < SCENARIOS["long_stop"]["sl"]
    assert SCENARIOS["short_stop"]["side"] == "sell"
    assert SCENARIOS["short_stop"]["exit_bar"]["high"] > SCENARIOS["short_stop"]["sl"]
    same_bar = SCENARIOS["long_same_bar_stop_first"]
    assert same_bar["exit_bar"]["low"] < same_bar["sl"] < same_bar["tp"] < same_bar["exit_bar"]["high"]
    assert same_bar["exit_event"] == "stop"
    limit = SCENARIOS["limit_entry_waits_for_touch"]
    assert limit["entry_order_type"] == "limit"
    assert limit["exit_bar"]["low"] > limit["entry_price"]


def test_parity_fixture_rejects_unknown_scenario() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        _scenario("not_a_scenario")


def test_exit_event_is_inferred_from_actual_fill_price_not_declared_expectation() -> None:
    scenario = _scenario("long_same_bar_stop_first")
    assert _exit_event(95.0, scenario) == "stop"
    assert _exit_event(105.0, scenario) == "target"
