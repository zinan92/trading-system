from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from pipelines.dualtrack_cycle_runner import DualTrackCycleRunner
from schemas.accounting import build_accounting_snapshot
from schemas.market_data import Bar
from services.dualtrack_config import DEFAULT_DUALTRACK_CONFIG
from services.execution_plugin_composition import (
    build_configured_execution_engine_adapter,
)
from services.dca_plan import build_dca_strategy_plan
from services.journal_store import write_json
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

    # Any public start call spends its prepared capability, even a clean
    # rejection.  A corrected acknowledgement must bind a fresh preparation.
    prepared = plane.control(
        CYCLE_ID,
        "prepare_start",
        _payload(),
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:01:01+00:00",
    )
    start_payload = {
        **_payload(),
        "prepared_start_id": prepared["prepared_start_id"],
        "expected_preview_id": prepared["preview"]["preview_id"],
    }
    started = plane.control(
        CYCLE_ID,
        "start",
        {**start_payload, "risk_acknowledgements": _ack(prepared["preview"])},
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:01:02+00:00",
    )
    assert started["runtime"]["strategy_type"] == "dca"
    assert started["created_orders"] == 3
    assert started["accepted_orders"] == 3
    assert started["plan"]["dca"]["aggregate_take_profit"][
        "one_active_order_required"
    ] is True
    assert started["risk_decision"]["scope"] == "paper_only"
    with pytest.raises(
        ValueError,
        match="prepared_start_id_already_spent",
    ):
        plane.control(
            CYCLE_ID,
            "start",
            {
                **start_payload,
                "risk_acknowledgements": _ack(prepared["preview"]),
            },
            market=_market(),
            account={"equity": 10_000.0},
            now="2026-07-22T16:02:00+00:00",
        )


def test_dca_prepared_start_freezes_risk_envelope_identity(
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
    plan = build_dca_strategy_plan(
        preview,
        strategy_plan_id="strategy-plan-dca-envelope-freeze",
        version=1,
        locked_at="2026-07-22T16:00:00+00:00",
    )
    envelope = plane.risk_envelopes.authorize_envelope(
        cycle_id=CYCLE_ID,
        plan=plan,
        payload={
            "authorization_kind": "human_explicit",
            "limits": {
                "max_actual_leverage": "20",
                "max_full_depth_loss": "100000",
                "max_notional_per_addition": "100000",
                "max_total_possible_notional": "1000000",
                "min_additions": "1",
                "max_additions": "20",
            },
        },
        actor={"email": "park@example.com"},
        now="2026-07-22T16:00:00+00:00",
    )
    plan["cycle_risk_envelope_id"] = envelope[
        "envelope_authorization_id"
    ]
    plane._write_plan(plan)
    prepared = plane.control(
        CYCLE_ID,
        "prepare_start",
        {
            **_payload(),
            "cycle_risk_envelope_id": envelope[
                "envelope_authorization_id"
            ],
        },
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:00:00+00:00",
    )

    assert prepared["cycle_risk_envelope_id"] == (
        envelope["envelope_authorization_id"]
    )
    with pytest.raises(ValueError, match="prepared_start_changed"):
        plane.control(
            CYCLE_ID,
            "start",
            {
                **_payload(),
                "cycle_risk_envelope_id": "candidate-envelope-b",
                "prepared_start_id": prepared["prepared_start_id"],
                "expected_preview_id": prepared["preview"][
                    "preview_id"
                ],
            },
            market=_market(),
            account={"equity": 10_000.0},
            now="2026-07-22T16:01:00+00:00",
        )

    adapter = build_configured_execution_engine_adapter(
        tmp_path / "outputs",
        config=plane.config,
    )
    assert adapter.snapshot(CYCLE_ID)["orders"] == []
    assert (
        plane.active_plan(CYCLE_ID)["strategy_plan_id"]
        == plan["strategy_plan_id"]
    )


def test_nautilus_paper_dca_start_requires_a_fresh_execution_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    real = build_configured_execution_engine_adapter(output, config=_config())

    class NautilusPaperFacade:
        name = "nautilus_paper"

        def __getattr__(self, name: str):
            return getattr(real, name)

    monkeypatch.setattr(
        control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: NautilusPaperFacade(),
    )
    plane = StrategyControlPlane(output)
    plane.config["execution_engine"] = {
        "authoritative": "nautilus_paper",
        "shadow": "none",
        "real_money_eligible": False,
    }

    with pytest.raises(ValueError, match="paper_execution_tick_unavailable:heartbeat_missing"):
        plane.control(
            CYCLE_ID,
            "start",
            _payload(),
            market=_market(),
            account={"equity": 10_000.0},
            now="2026-07-22T16:00:00+00:00",
        )

    write_json(
        output / "dualtrack" / "runner" / f"{CYCLE_ID}.json",
        [{
            "ts": "2026-07-22T15:59:00+00:00",
            "cycle_id": CYCLE_ID,
            "event": "live_tick_heartbeat",
            "detail": {"runner": "dualtrack-live-tick", "ledger_refreshed": True},
        }],
    )
    prepared = plane.control(
        CYCLE_ID,
        "prepare_start",
        _payload(),
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:00:00+00:00",
    )
    started = plane.control(
        CYCLE_ID,
        "start",
        {
            **_payload(),
            "prepared_start_id": prepared["prepared_start_id"],
            "expected_preview_id": prepared["preview"]["preview_id"],
            "risk_acknowledgements": _ack(prepared["preview"]),
        },
        market=_market(),
        account={"equity": 10_000.0},
        now="2026-07-22T16:00:01+00:00",
    )

    assert started["runtime"]["strategy_type"] == "dca"
    assert started["accepted_orders"] == 3


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
    observed = plane.read_model(CYCLE_ID)
    assert observed["dca_lifecycle"]["strategy_plan_id"] == started["plan"]["strategy_plan_id"]
    assert observed["dca_lifecycle"]["active_target"]["generation"] == 2
    assert observed["dca_lifecycle"]["active_target"]["quantity"] == second["state"]["open_quantity"]

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


def _grid_market(*, close: float = 110.0) -> dict:
    bars = []
    for index in range(40):
        bar_close = close - 2.0 + index * 0.05
        bars.append(
            {
                "timestamp": f"2026-07-22T15:{index:02d}:00+00:00",
                "open": round(bar_close - 0.1, 4),
                "high": round(bar_close + 0.4, 4),
                "low": round(bar_close - 0.4, 4),
                "close": round(bar_close, 4),
            }
        )

    def context_bars(timeframe: str, span: float) -> list[dict]:
        rows = []
        for index in range(20):
            bar_close = close - 1.0 + index * 0.05
            rows.append(
                {
                    "timestamp": (
                        f"2026-06-{index + 1:02d}T00:00:00+00:00"
                        if timeframe == "1d"
                        else f"2026-07-19T{(index % 6) * 4:02d}:00:00+00:00"
                    ),
                    "open": round(bar_close - 0.1, 4),
                    "high": round(bar_close + span / 2.0, 4),
                    "low": round(bar_close - span / 2.0, 4),
                    "close": round(bar_close, 4),
                }
            )
        return rows

    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "binance_usdm_futures",
        "source_mode": "binance_usdm_futures",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": close,
        "latest_timestamp": "2026-07-22T15:39:00+00:00",
        "bars": bars,
        "strategy_timeframes": {
            "1d": {
                "timeframe": "1d",
                "provider": "derived:binance_usdm",
                "is_synthetic": False,
                "bars": context_bars("1d", 10.0),
            },
            "4h": {
                "timeframe": "4h",
                "provider": "derived:binance_usdm",
                "is_synthetic": False,
                "bars": context_bars("4h", 4.0),
            },
        },
    }


def _grid_proposal() -> dict:
    return {
        "cycle_id": CYCLE_ID,
        "source": "ai",
        "direction": "long",
        "range": {"low": 100.0, "high": 120.0},
        "key_levels": [100.0, 110.0, 120.0],
        "grid": {"count": 4, "notional_per_grid": 250.0, "spacing": 5.0},
        "signal": {"name": "ema_trend", "confidence": 7},
        "tp_sl": {"tp": 118.0, "sl": 96.0, "r_multiple": 2.0},
        "risk_budget": {"max_loss": 100.0, "max_leverage": 3.0},
        "intraday_rules": [{"if": "range_break", "then": "stand_down"}],
    }


def _grid_account(equity: float = 10_000.0) -> dict:
    snapshot = build_accounting_snapshot(
        source_type="production_history",
        source_name="production_history",
        source_schema_version="dualtrack-execution-v1",
        scope={"strategy_plan_scope": "test"},
        currency="USDT",
        orders=[],
        fills=[],
        positions=[],
        trades=[],
        counts={},
        pnl={"net_realized_pnl": 0.0, "unrealized_pnl": 0.0},
        account={"starting_balance": equity, "ending_cash": equity, "equity": equity},
        completeness={"status": "complete", "limitations": []},
        reconciliation={"status": "pass", "issues": []},
    ).to_dict()
    return {"equity": equity, "ending_cash": equity, "accounting_snapshot": snapshot}


def _start_running_grid(plane: StrategyControlPlane, tmp_path: Path) -> dict:
    saved = plane.upsert_proposal(_grid_proposal())
    plane.lock_production_plan(CYCLE_ID, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        CYCLE_ID,
        "start",
        {"direction": "neutral", "style": "steady"},
        market=_grid_market(),
        account=_grid_account(),
        now="2026-07-22T15:40:00+00:00",
    )
    assert started["runtime"]["actual_state"] == "running"
    assert started["created_orders"] > 0
    return started


def test_dca_start_is_rejected_while_a_grid_is_running_and_grid_orders_stay_untouched(
    tmp_path: Path,
) -> None:
    plane = _plane(tmp_path)
    _start_running_grid(plane, tmp_path)
    adapter = build_configured_execution_engine_adapter(
        tmp_path / "outputs",
        config=plane.config,
    )
    before = adapter.snapshot(CYCLE_ID)
    before_orders = [
        (row.get("order_id"), row.get("state"), row.get("price"))
        for row in before["orders"]
    ]
    assert [row for row in before["orders"] if row.get("state") == "accepted"]
    grid_plan = plane.active_plan(CYCLE_ID)
    assert grid_plan is not None
    assert grid_plan.get("strategy_type") != "dca"

    # Even the side-effect-free prepare step is rejected while the grid runs,
    # so a DCA start can never obtain a prepared candidate against it.
    with pytest.raises(ValueError, match="robot is already running"):
        plane.control(
            CYCLE_ID,
            "prepare_start",
            _payload(),
            market=_market(),
            account={"equity": 10_000.0},
            now="2026-07-22T16:00:00+00:00",
        )
    # Without a prepared candidate the start contract itself fails closed
    # before any mutation, so no path can slip past the running-grid guard.
    with pytest.raises(ValueError, match="strategy_preview_changed"):
        plane.control(
            CYCLE_ID,
            "start",
            {**_payload(), "expected_preview_id": ""},
            market=_market(),
            account={"equity": 10_000.0},
            now="2026-07-22T16:01:00+00:00",
        )

    after = adapter.snapshot(CYCLE_ID)
    after_orders = [
        (row.get("order_id"), row.get("state"), row.get("price"))
        for row in after["orders"]
    ]
    assert after_orders == before_orders
    assert after["positions"] == before["positions"]
    runtime = plane.runtime_state(CYCLE_ID)
    assert runtime["actual_state"] == "running"
    assert runtime.get("strategy_type") != "dca"
    assert plane.active_plan(CYCLE_ID)["strategy_plan_id"] == grid_plan["strategy_plan_id"]
    lifecycle_dir = tmp_path / "outputs" / "dualtrack" / "dca_lifecycle"
    assert not lifecycle_dir.exists()


def test_cycle_runner_keeps_grid_bars_on_the_grid_path_without_dca_lifecycle(
    tmp_path: Path,
) -> None:
    plane = _plane(tmp_path)
    _start_running_grid(plane, tmp_path)
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
        for index, price in enumerate((109.0, 110.0, 111.0), start=1)
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
        "close": 111.0,
        "quality_flags": ["execution_venue"],
    }
    runner._market_max_age_seconds = lambda: 120
    runner._filter_market_session_bars = lambda rows: rows

    result = runner._sweep_human_protective_exits(
        CYCLE_ID,
        now=datetime.fromisoformat("2026-07-22T16:04:00+00:00"),
    )

    assert result["processed_events"] == 3
    assert "dca_lifecycle" not in result
    assert result["last_event_ts"] == "2026-07-22T16:03:00+00:00"
    assert result["source"] == "market_db:binance_usdm_futures"
    lifecycle_dir = tmp_path / "outputs" / "dualtrack" / "dca_lifecycle"
    assert not lifecycle_dir.exists()
    runtime = plane.runtime_state(CYCLE_ID)
    assert runtime["actual_state"] == "running"
    assert runtime.get("strategy_type") != "dca"
