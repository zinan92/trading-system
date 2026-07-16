from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipelines.dualtrack_nautilus_shadow_replay as replay_pipeline
from spikes.dualtrack_nautilus_shadow_replay import (
    _account,
    _fills_from_reports,
    _latest,
    _orders_from_reports,
    run_replay,
)


def test_shadow_replay_loader_requires_nonempty_artifact_array(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty shadow input"):
        _latest(path, "shadow input")


def test_shadow_replay_loader_uses_latest_artifact(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text(json.dumps([{"input_id": "old"}, {"input_id": "new"}]), encoding="utf-8")
    assert _latest(path, "shadow input") == {"input_id": "new"}


def test_shadow_replay_pipeline_fails_closed_without_prepared_artifacts(tmp_path: Path) -> None:
    result = replay_pipeline.main([
        "--cycle-id", "2026-07-10_DAY",
        "--nautilus-python", "/missing/nautilus-python",
        "--output-root", str(tmp_path / "outputs"),
    ])

    assert result == 2


def test_shadow_replay_refuses_command_without_immutable_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = tmp_path / "preflight.json"
    input_path = tmp_path / "input.json"
    preflight.write_text(json.dumps([{
        "status": "ready_for_paper_shadow",
        "instrument": {"symbol": "XAUUSDT"},
        "fee_model": {"maker_fee_rate": "0.00005", "taker_fee_rate": "0.00005", "real_money_eligible": False},
    }]), encoding="utf-8")
    input_path.write_text(json.dumps([{
        "schema_version": "dualtrack-shadow-input-v1",
        "cycle_id": "2026-07-10_DAY",
        "commands": [{"command_id": "cmd-1"}],
        "market_events": [{"instrument_id": "XAUUSDT"}],
    }]), encoding="utf-8")

    monkeypatch.setattr("spikes.dualtrack_nautilus_shadow_replay.build_nautilus_instrument", lambda *_args, **_kwargs: object())
    with pytest.raises(ValueError, match="missing command payload"):
        run_replay(preflight, input_path)


def test_shadow_account_uses_open_units_and_trusted_mark() -> None:
    account = _account(
        [{"status": "open", "remaining_units": 2.0, "entry_price": 98.0}],
        [],
        -0.01,
        4.0,
        mark_price=100.0,
    )

    assert account["exposure"] == 200.0
    assert account["margin"] == 20.0
    assert account["equity"] == 10_003.99


def test_order_reports_keep_pending_entries_and_only_materialized_protection() -> None:
    commands = [{
        "command_id": "cmd-1",
        "cycle_id": "2026-07-16_DAY",
        "command": {
            "cycle_id": "2026-07-16_DAY",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": 4000.0,
            "quantity": 0.123456,
            "sl": 3990.0,
            "tp": 4010.0,
            "strategy_plan_id": "plan-1",
            "strategy_plan_version": 3,
        },
    }]
    reports = [
        {
            "client_order_id": "cmd-1",
            "status": "ACCEPTED",
            "type": "LIMIT",
            "side": "BUY",
            "price": 4000.0,
            "quantity": 0.123,
            "filled_qty": 0.0,
            "tags": ["ENTRY"],
        },
        {
            "client_order_id": "cmd-1-SL",
            "parent_order_id": "cmd-1",
            "status": "SUBMITTED",
            "type": "STOP_MARKET",
            "side": "SELL",
            "trigger_price": 3990.0,
            "quantity": 0.123,
            "filled_qty": 0.0,
            "tags": ["STOP_LOSS"],
        },
        {
            "client_order_id": "cmd-1-TP",
            "parent_order_id": "cmd-1",
            "status": "FILLED",
            "type": "LIMIT",
            "side": "SELL",
            "price": 4010.0,
            "quantity": 0.123,
            "filled_qty": 0.123,
            "tags": ["TAKE_PROFIT"],
        },
    ]

    orders = _orders_from_reports(reports, commands)

    assert orders == [
        {
            "order_id": "cmd-1",
            "state": "accepted",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": 4000.0,
            "quantity": 0.123,
            "requested_price": 4000.0,
            "requested_quantity": 0.123456,
            "strategy_plan_id": "plan-1",
            "strategy_plan_version": 3,
        },
        {
            "order_id": "cmd-1-TP",
            "state": "filled",
            "side": "sell",
            "event": "target",
            "order_type": "limit",
            "price": 4010.0,
            "quantity": 0.123,
            "strategy_plan_id": "plan-1",
            "strategy_plan_version": 3,
        },
    ]


def test_fill_reports_map_client_order_identity_back_to_plan_and_event() -> None:
    commands = [{
        "command_id": "cmd-1",
        "cycle_id": "2026-07-16_DAY",
        "command": {
            "cycle_id": "2026-07-16_DAY",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": 4000.0,
            "quantity": 0.123456,
            "strategy_plan_id": "plan-1",
            "strategy_plan_version": 3,
        },
    }]
    reports = [{
        "client_order_id": "cmd-1-TP",
        "side": "SELL",
        "avg_px": 4010.0,
        "filled_qty": 0.123,
        "ts_last": "2026-07-16T01:05:00+00:00",
        "tags": ["TAKE_PROFIT"],
    }]

    fills = _fills_from_reports(reports, commands)

    assert fills == [{
        "fill_id": "nautilus-cmd-1-TP",
        "order_id": "cmd-1-TP",
        "trade_id": "cmd-1",
        "cycle_id": "2026-07-16_DAY",
        "ts": "2026-07-16T01:05:00+00:00",
        "side": "sell",
        "event": "target",
        "price": 4010.0,
        "quantity": 0.123,
        "requested_price": 4010.0,
        "slippage": 0.0,
        "cost": 0.0,
        "liquidity": "",
        "strategy_plan_id": "plan-1",
        "strategy_plan_version": 3,
    }]
