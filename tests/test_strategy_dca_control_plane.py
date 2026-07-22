from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from pipelines.dualtrack_cycle_runner import DualTrackCycleRunner
from schemas.market_data import Bar
from services.dualtrack_config import DEFAULT_DUALTRACK_CONFIG
from services.execution_plugin_composition import (
    build_configured_execution_engine_adapter,
)
from services.strategy_control_plane import StrategyControlPlane
import services.strategy_control_plane as control_plane_module


CYCLE_ID = "2026-07-22_NIGHT"


def _config() -> dict:
    config = deepcopy(DEFAULT_DUALTRACK_CONFIG)
    config["execution_engine"] = {
        "authoritative": "legacy_paper",
        "shadow": "none",
        "real_money_eligible": False,
    }
    return config


def _market(price: float = 4_010.0) -> dict:
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "binance_usdm_futures",
        "source_mode": "binance_usdm_futures",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": price,
        "latest_timestamp": "2026-07-22T16:00:00+00:00",
        "bars": [
            {
                "timestamp": f"2026-07-22T15:{index:02d}:00+00:00",
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
            }
            for index in range(20)
        ],
    }


def _payload() -> dict:
    return {
        "strategy_type": "dca",
        "direction": "long",
        "dca": {
            "entry_levels": [4_004.0, 3_996.0, 3_988.0],
            "target_price": 4_050.0,
            "stop_price": 3_970.0,
            "notional_per_addition": 2_000.0,
            "max_additions": 3,
            "loop_enabled": False,
        },
        "risk_budget": {"leverage": 10},
    }


def _plane(tmp_path: Path) -> StrategyControlPlane:
    plane = StrategyControlPlane(tmp_path / "outputs")
    plane.config = _config()
    return plane


def _ack(preview: dict) -> dict:
    contract = preview["manual_confirmation"]
    return {
        "schema_version": contract["schema_version"],
        "preview_id": preview["preview_id"],
        "facts_digest": contract["facts_digest"],
        "codes": [
            row["code"] for row in contract["required_acknowledgements"]
        ],
    }


def _event(sequence: int, price: float) -> dict:
    return {
        "schema_version": "dualtrack-market-event-v1",
        "event_id": f"dca-control-{sequence}-{price}",
        "cycle_id": CYCLE_ID,
        "ts_event": f"2026-07-22T16:{sequence:02d}:00+00:00",
        "event_started_at": f"2026-07-22T16:{sequence - 1:02d}:00+00:00",
        "source": "binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
        "symbol": "GOLD",
        "timeframe": "1m",
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "price": price,
        "fresh": True,
        "is_synthetic": False,
    }


def test_dca_prepare_is_read_only_and_start_requires_exact_risk_acknowledgement(
    tmp_path: Path,
) -> None:
    plane = _plane(tmp_path)
    preview = plane.control(
        CYCLE_ID,
        "preview",
        _payload(),
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:00:00+00:00",
    )["preview"]
    assert preview["strategy_type"] == "dca"
    assert preview["manual_confirmation"]["required"] is True

    prepared = plane.control(
        CYCLE_ID,
        "prepare_start",
        _payload(),
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:00:00+00:00",
    )
    adapter = build_configured_execution_engine_adapter(
        tmp_path / "outputs",
        config=plane.config,
    )
    assert adapter.snapshot(CYCLE_ID)["orders"] == []
    assert plane.active_plan(CYCLE_ID) is None

    start_payload = {
        **_payload(),
        "prepared_start_id": prepared["prepared_start_id"],
        "expected_preview_id": prepared["preview"]["preview_id"],
    }
    with pytest.raises(ValueError, match="dca_risk_acknowledgements_incomplete"):
        plane.control(
            CYCLE_ID,
            "start",
            start_payload,
            market=_market(),
            account={"equity": 10_000.0},
            now="2026-07-22T16:01:00+00:00",
        )

    started = plane.control(
        CYCLE_ID,
        "start",
        {**start_payload, "risk_acknowledgements": _ack(prepared["preview"])},
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:01:00+00:00",
    )
    assert started["runtime"]["strategy_type"] == "dca"
    assert started["created_orders"] == 3
    assert started["accepted_orders"] == 3
    assert started["plan"]["dca"]["aggregate_take_profit"][
        "one_active_order_required"
    ] is True
    assert started["risk_decision"]["scope"] == "paper_only"
    repeated = plane.control(
        CYCLE_ID,
        "start",
        {**start_payload, "risk_acknowledgements": _ack(prepared["preview"])},
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:02:00+00:00",
    )
    assert repeated["idempotent"] is True
    assert repeated["created_orders"] == 0


def test_running_dca_accumulates_then_stops_after_one_aggregate_target(
    tmp_path: Path,
) -> None:
    plane = _plane(tmp_path)
    prepared = plane.control(
        CYCLE_ID,
        "prepare_start",
        _payload(),
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:00:00+00:00",
    )
    preview = prepared["preview"]
    started = plane.control(
        CYCLE_ID,
        "start",
        {
            **_payload(),
            "prepared_start_id": prepared["prepared_start_id"],
            "expected_preview_id": preview["preview_id"],
            "risk_acknowledgements": _ack(preview),
        },
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:01:00+00:00",
    )
    adapter = build_configured_execution_engine_adapter(
        tmp_path / "outputs",
        config=plane.config,
    )

    first = plane.advance_dca_market_event(
        CYCLE_ID,
        _event(2, 4_004.0),
        adapter=adapter,
    )
    second = plane.advance_dca_market_event(
        CYCLE_ID,
        _event(3, 3_996.0),
        adapter=adapter,
    )
    assert first["state"]["additions_filled"] == 1
    assert second["state"]["additions_filled"] == 2
    assert second["state"]["target_generations"][0]["status"] == "cancelled"
    assert second["state"]["active_target"]["quantity"] > first["state"][
        "active_target"
    ]["quantity"]

    closed = plane.advance_dca_market_event(
        CYCLE_ID,
        _event(4, 4_050.0),
        adapter=adapter,
    )
    snapshot = adapter.snapshot(CYCLE_ID)
    target_fills = [
        row for row in snapshot["fills"] if row.get("event") == "target"
    ]
    assert closed["state"]["status"] == "target_closed"
    assert closed["runtime"]["actual_state"] == "stopped"
    assert len(target_fills) == 1
    assert len(target_fills[0]["matched_entries"]) == 2
    assert not [row for row in snapshot["orders"] if row.get("state") == "accepted"]

    with pytest.raises(ValueError, match="active running DCA"):
        plane.advance_dca_market_event(
            CYCLE_ID,
            _event(5, 4_060.0),
            adapter=adapter,
        )

    assert started["plan"]["strategy_plan_id"] == plane.active_plan(CYCLE_ID)[
        "strategy_plan_id"
    ]


def test_cycle_runner_routes_completed_bars_through_dca_lifecycle(
    tmp_path: Path,
) -> None:
    plane = _plane(tmp_path)
    prepared = plane.control(
        CYCLE_ID,
        "prepare_start",
        _payload(),
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:00:00+00:00",
    )
    preview = prepared["preview"]
    plane.control(
        CYCLE_ID,
        "start",
        {
            **_payload(),
            "prepared_start_id": prepared["prepared_start_id"],
            "expected_preview_id": preview["preview_id"],
            "risk_acknowledgements": _ack(preview),
        },
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:00:30+00:00",
    )
    adapter = build_configured_execution_engine_adapter(
        tmp_path / "outputs",
        config=plane.config,
    )
    bars = [
        Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp=f"2026-07-22T16:0{index}:00+00:00",
            open=price,
            high=price,
            low=price,
            close=price,
            volume=1.0,
            provider="binance_usdm_futures",
        )
        for index, price in enumerate((4_004.0, 3_996.0, 4_050.0), start=1)
    ]

    class Market:
        def load_bars_between(self, *_args):
            return bars

    runner = object.__new__(DualTrackCycleRunner)
    runner.output_root = tmp_path / "outputs"
    runner.execution = adapter
    runner.market = Market()
    runner.symbol = "GOLD"
    runner.timeframe = "1m"
    runner.config = plane.config
    runner._latest_market_record = lambda: {
        "provider": "binance_usdm_futures",
        "timestamp": "2026-07-22T16:03:00+00:00",
        "close": 4_050.0,
        "quality_flags": ["execution_venue"],
    }
    runner._market_max_age_seconds = lambda: 120
    runner._filter_market_session_bars = lambda rows: rows

    result = runner._sweep_human_protective_exits(
        CYCLE_ID,
        now=datetime.fromisoformat("2026-07-22T16:04:00+00:00"),
    )

    assert result["processed_events"] == 3
    assert result["dca_lifecycle"]["status"] == "target_closed"
    assert plane.runtime_state(CYCLE_ID)["actual_state"] == "stopped"


def test_operator_stop_retires_dca_target_and_flattens_round(tmp_path: Path) -> None:
    plane = _plane(tmp_path)
    prepared = plane.control(
        CYCLE_ID,
        "prepare_start",
        _payload(),
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:00:00+00:00",
    )
    preview = prepared["preview"]
    plane.control(
        CYCLE_ID,
        "start",
        {
            **_payload(),
            "prepared_start_id": prepared["prepared_start_id"],
            "expected_preview_id": preview["preview_id"],
            "risk_acknowledgements": _ack(preview),
        },
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:00:30+00:00",
    )
    adapter = build_configured_execution_engine_adapter(
        tmp_path / "outputs",
        config=plane.config,
    )
    opened = plane.advance_dca_market_event(
        CYCLE_ID,
        _event(2, 4_004.0),
        adapter=adapter,
    )
    assert opened["state"]["active_target"]["status"] == "accepted"

    stopped = plane.control(
        CYCLE_ID,
        "stop",
        {},
        market=_market(4_000.0),
        now="2026-07-22T16:03:00+00:00",
    )
    snapshot = adapter.snapshot(CYCLE_ID)
    assert stopped["runtime"]["actual_state"] == "stopped"
    assert stopped["dca_lifecycle"]["status"] == "flattened"
    assert stopped["dca_lifecycle"]["active_target"] is None
    assert not [row for row in snapshot["orders"] if row.get("state") == "accepted"]
    assert not [row for row in snapshot["positions"] if row.get("status") == "open"]


def test_dca_start_failure_cancels_and_flattens_partial_paper_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane = _plane(tmp_path)
    prepared = plane.control(
        CYCLE_ID,
        "prepare_start",
        _payload(),
        market=_market(4_004.0),
        account={"equity": 10_000.0},
        now="2026-07-22T16:00:00+00:00",
    )
    preview = prepared["preview"]
    original = control_plane_module.DcaPaperLifecycle.process_market_event

    def fail_after_execution(self, plan, event):
        original(self, plan, event)
        raise RuntimeError("injected DCA start failure")

    monkeypatch.setattr(
        control_plane_module.DcaPaperLifecycle,
        "process_market_event",
        fail_after_execution,
    )
    with pytest.raises(RuntimeError, match="injected DCA start failure"):
        plane.control(
            CYCLE_ID,
            "start",
            {
                **_payload(),
                "prepared_start_id": prepared["prepared_start_id"],
                "expected_preview_id": preview["preview_id"],
                "risk_acknowledgements": _ack(preview),
            },
            market=_market(4_004.0),
            account={"equity": 10_000.0},
            now="2026-07-22T16:00:30+00:00",
        )

    adapter = build_configured_execution_engine_adapter(
        tmp_path / "outputs",
        config=plane.config,
    )
    snapshot = adapter.snapshot(CYCLE_ID)
    assert not [row for row in snapshot["orders"] if row.get("state") == "accepted"]
    assert not [row for row in snapshot["positions"] if row.get("status") == "open"]
    assert plane.runtime_state(CYCLE_ID)["actual_state"] == "error"
