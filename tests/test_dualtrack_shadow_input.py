from __future__ import annotations

import pytest

from services.dualtrack_shadow_input import build_shadow_input


def _event(*, event_id: str = "") -> dict:
    result = {
        "cycle_id": "2026-07-10_DAY",
        "ts_event": "2026-07-10T01:01:00+00:00",
        "event_started_at": "2026-07-10T01:00:00+00:00",
        "price": 100.0,
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm",
        "provider": "binance_usdm",
        "instrument_id": "XAUUSDT",
    }
    if event_id:
        result["event_id"] = event_id
    return result


def _snapshot() -> dict:
    return {"cycle_id": "2026-07-10_DAY", "engine": "legacy_paper", "fills": [{"fill_id": "legacy-1"}]}


def test_shadow_input_is_stable_and_keeps_canonical_events() -> None:
    commands = [{"command_id": "cmd-1", "cycle_id": "2026-07-10_DAY", "command": {"side": "buy"}}]
    first = build_shadow_input(cycle_id="2026-07-10_DAY", authoritative_snapshot=_snapshot(), market_events=[_event()], commands=commands)
    second = build_shadow_input(cycle_id="2026-07-10_DAY", authoritative_snapshot=_snapshot(), market_events=[_event()], commands=commands)

    assert first["input_id"] == second["input_id"]
    assert first["authoritative_engine"] == "legacy_paper"
    assert first["market_events"][0]["schema_version"] == "dualtrack-market-event-v1"
    assert first["legacy_fills"] == [{"fill_id": "legacy-1"}]
    assert first["commands"] == commands


def test_shadow_input_rejects_duplicate_or_wrong_cycle_events() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        build_shadow_input(
            cycle_id="2026-07-10_DAY",
            authoritative_snapshot=_snapshot(),
            market_events=[_event(event_id="same"), _event(event_id="same")],
        )
    wrong_cycle = _event()
    wrong_cycle["cycle_id"] = "2026-07-10_NIGHT"
    with pytest.raises(ValueError, match="cycle_id mismatch"):
        build_shadow_input(cycle_id="2026-07-10_DAY", authoritative_snapshot=_snapshot(), market_events=[wrong_cycle])


def test_shadow_input_requires_execution_identity_on_every_event() -> None:
    event = _event()
    event.pop("instrument_id")
    with pytest.raises(ValueError, match="instrument_id is required"):
        build_shadow_input(cycle_id="2026-07-10_DAY", authoritative_snapshot=_snapshot(), market_events=[event])


def test_shadow_input_rejects_duplicate_command_ids() -> None:
    commands = [
        {"command_id": "same", "cycle_id": "2026-07-10_DAY"},
        {"command_id": "same", "cycle_id": "2026-07-10_DAY"},
    ]
    with pytest.raises(ValueError, match="duplicate command"):
        build_shadow_input(cycle_id="2026-07-10_DAY", authoritative_snapshot=_snapshot(), market_events=[_event()], commands=commands)
