from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipelines.dualtrack_nautilus_shadow_replay as replay_pipeline
from spikes.dualtrack_nautilus_shadow_replay import (
    _apply_paper_safe_action_settlements,
    _account,
    _fills_from_reports,
    _latest,
    _native_replay_commands,
    _orders_from_reports,
    _require_strict_json,
    run_replay,
)


def _safe_gate(action_class: str, *, price: float | None = None) -> dict:
    return {
        "schema_version": "paper-safe-action-market-gate-v1",
        "scope": "paper_only",
        "action_class": action_class,
        "entry_market_gate_applies": False,
        "market_status": "blocked",
        "market_fresh": False,
        "market_is_synthetic": False,
        "market_provider": "",
        "pricing_required": action_class == "reduce_only",
        "pricing_source": "last_known_execution_fill" if price is not None else "not_required",
        "pricing_price": price,
        "pricing_timestamp": "2026-07-10T01:01:00+00:00" if price is not None else "",
        "pricing_provider": "paper_execution_ledger" if price is not None else "",
    }


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


def test_paper_safe_action_overlay_cancels_and_reduces_after_last_market_event() -> None:
    orders = [
        {"order_id": "entry-filled", "state": "filled"},
        {"order_id": "entry-pending", "state": "accepted"},
        {"order_id": "close-1", "state": "denied"},
    ]
    fills = [{
        "fill_id": "nautilus-entry-filled",
        "order_id": "entry-filled",
        "trade_id": "entry-filled",
        "event": "entry",
        "price": 100.0,
        "quantity": 1.0,
        "cost": 0.04,
        "ts": "2026-07-10T01:01:00+00:00",
    }]
    positions = [{
        "trade_id": "entry-filled",
        "position_id": "POS-entry-filled",
        "status": "open",
        "side": "long",
        "remaining_units": 1.0,
        "entry_price": 100.0,
        "entry_ts": "2026-07-10T01:01:00+00:00",
        "realized_pnl": -0.04,
    }]
    commands = [
        {
            "command_id": "cancel-1",
            "command": {
                "event": "cancel",
                "cancel_order_id": "entry-pending",
                "ts": "2026-07-10T02:00:00+00:00",
                "safe_action_market_gate": _safe_gate("cancel"),
            },
        },
        {
            "command_id": "close-1",
            "command": {
                "cycle_id": "2026-07-10_DAY",
                "event": "exit",
                "side": "sell",
                "price": 100.0,
                "quantity": 0.4,
                "requested_at": "2026-07-10T02:01:00+00:00",
                "market_timestamp": "2026-07-10T01:01:00+00:00",
                "market_source": "paper_execution_ledger",
                "market_fresh": False,
                "target_position_id": "POS-entry-filled",
                "target_command_id": "entry-filled",
                "safe_action_market_gate": _safe_gate("reduce_only", price=100.0),
            },
        },
    ]

    settled = _apply_paper_safe_action_settlements(
        orders,
        fills,
        positions,
        commands,
        mark_price=100.0,
        taker_fee_rate=0.0004,
    )
    settled_orders, settled_fills, settled_positions, realized, unrealized, count = settled

    assert next(row for row in settled_orders if row["order_id"] == "entry-pending")["state"] == "canceled"
    assert next(row for row in settled_orders if row["order_id"] == "close-1")["state"] == "filled"
    assert settled_fills[-1]["order_id"] == "close-1"
    assert settled_positions[0]["remaining_units"] == pytest.approx(0.6)
    assert settled_positions[0]["realized_pnl"] == pytest.approx(-0.056)
    assert realized == pytest.approx(-0.056)
    assert unrealized == 0.0
    assert count == 2


def test_native_replay_excludes_post_settled_safe_actions_and_cancelled_targets() -> None:
    cancel_gate = _safe_gate("cancel")
    reduce_gate = _safe_gate("reduce_only", price=100.0)
    fresh_gate = {
        **reduce_gate,
        "market_status": "ready",
        "market_fresh": True,
        "pricing_source": "fresh_server_mark",
    }
    rejected_provider_gate = {
        **reduce_gate,
        "market_status": "ready",
        "market_fresh": True,
        "market_provider": "unexpected-provider",
    }
    commands = [
        {"command_id": "cancel-target", "command": {"event": "entry"}},
        {"command_id": "unrelated-entry", "command": {"event": "entry"}},
        {
            "command_id": "cancel-command",
            "command": {
                "event": "cancel",
                "cancel_order_id": "cancel-target",
                "safe_action_market_gate": cancel_gate,
            },
        },
        {
            "command_id": "stale-close",
            "command": {
                "event": "exit",
                "market_fresh": False,
                "safe_action_market_gate": reduce_gate,
            },
        },
        {
            "command_id": "wrong-provider-close",
            "command": {
                "event": "exit",
                "market_fresh": False,
                "safe_action_market_gate": rejected_provider_gate,
            },
        },
        {
            "command_id": "fresh-close",
            "command": {"event": "exit", "safe_action_market_gate": fresh_gate},
        },
    ]

    native = _native_replay_commands(commands)

    assert [row["command_id"] for row in native] == ["unrelated-entry", "fresh-close"]


@pytest.mark.parametrize(
    ("gate_price", "side", "message"),
    [
        (101.0, "sell", "price evidence mismatch"),
        (100.0, "buy", "side does not reduce"),
    ],
)
def test_paper_safe_action_overlay_rejects_tampered_execution_evidence(
    gate_price: float,
    side: str,
    message: str,
) -> None:
    gate = _safe_gate("reduce_only", price=gate_price)
    command = {
        "command_id": "close-1",
        "command": {
            "cycle_id": "2026-07-10_DAY",
            "event": "flatten",
            "side": side,
            "price": 100.0,
            "quantity": 1.0,
            "requested_at": "2026-07-10T02:01:00+00:00",
            "market_timestamp": gate["pricing_timestamp"],
            "market_source": gate["pricing_provider"],
            "market_fresh": False,
            "target_position_id": "POS-entry-filled",
            "safe_action_market_gate": gate,
        },
    }

    with pytest.raises(ValueError, match=message):
        _apply_paper_safe_action_settlements(
            [{"order_id": "close-1", "state": "denied"}],
            [],
            [{
                "trade_id": "entry-filled",
                "position_id": "POS-entry-filled",
                "status": "open",
                "side": "long",
                "remaining_units": 1.0,
                "entry_price": 100.0,
                "entry_ts": "2026-07-10T01:01:00+00:00",
                "realized_pnl": 0.0,
            }],
            [command],
            mark_price=100.0,
            taker_fee_rate=0.0004,
        )


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


def test_market_order_report_keeps_requested_price_separate_when_nautilus_price_is_nan() -> None:
    commands = [{
        "command_id": "flatten-1",
        "cycle_id": "2026-07-22_DAY",
        "command": {
            "cycle_id": "2026-07-22_DAY",
            "side": "sell",
            "event": "flatten",
            "order_type": "market",
            "price": 4121.57,
            "quantity": 1.182,
        },
    }]
    reports = [{
        "client_order_id": "flatten-1",
        "status": "FILLED",
        "type": "MARKET",
        "side": "SELL",
        "price": float("nan"),
        "quantity": 1.182,
    }]

    orders = _orders_from_reports(reports, commands)

    assert orders == [{
        "order_id": "flatten-1",
        "state": "filled",
        "side": "sell",
        "event": "flatten",
        "order_type": "market",
        "quantity": 1.182,
        "requested_price": 4121.57,
        "requested_quantity": 1.182,
        "strategy_plan_id": None,
        "strategy_plan_version": None,
    }]


def test_replay_result_rejects_non_finite_values_before_persistence() -> None:
    with pytest.raises(ValueError, match="strict JSON values"):
        _require_strict_json({"orders": [{"price": 100.0}], "pnl": {"realized": float("nan")}})


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
