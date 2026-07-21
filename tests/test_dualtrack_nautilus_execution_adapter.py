from __future__ import annotations

from pathlib import Path

import pytest

from services.accounting_projection import project_execution_accounting
from services.dualtrack_nautilus_execution_adapter import NautilusExecutionAdapter
from services.journal_store import load_json, write_json


CYCLE_ID = "2026-07-10_DAY"


def _preflight(path: Path) -> None:
    write_json(path, [{
        "status": "ready_for_paper_shadow",
        "fee_model": {
            "mode": "account_observed",
            "maker_fee_rate": "0",
            "taker_fee_rate": "0.000400",
            "funding_rate": "0.0001",
            "funding_time": 1,
            "observed_at": "2026-07-10T10:00:00+00:00",
            "real_money_eligible": False,
        },
    }])


def _candidate(cycle_id: str) -> dict:
    return {
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_shadow",
        "cycle_id": cycle_id,
        "orders": [{
            "order_id": "n-1", "state": "filled", "side": "buy", "event": "entry",
            "order_type": "market", "price": 100.0, "quantity": 1.0,
            "ts": "2026-07-10T01:00:00+00:00",
        }],
        "fills": [{
            "fill_id": "n-fill-1", "order_id": "n-1", "trade_id": "n-entry-1",
            "event": "entry", "side": "buy", "price": 100.0, "quantity": 1.0,
            "cost": 0.005, "gross_pnl": 0.0, "realized_pnl": -0.005,
            "ts": "2026-07-10T01:00:00+00:00",
        }],
        "positions": [{
            "trade_id": "n-entry-1",
            "position_id": "POS-n-entry-1",
            "status": "open",
            "side": "long",
            "remaining_units": 1.0,
            "entry_price": 100.0,
            "entry_ts": "2026-07-10T01:00:00+00:00",
            "realized_pnl": -0.005,
            "unrealized_pnl": 0.0,
            "strategy_plan_id": "plan-test",
            "strategy_plan_version": 1,
        }],
        "account": {
            "starting_cash": 10_000.0,
            "realized_pnl": -0.005,
            "ending_cash": 9_999.995,
            "equity": 9_999.995,
            "margin": 10.0,
            "exposure": 100.0,
            "slippage": 0.0,
            "fees": 0.005,
            "funding": 0.0,
        },
        "pnl": {"realized": -0.005, "unrealized": 0.0},
        "mark": {"price": 100.0, "fresh": True, "source": "test"},
        "capabilities": {"native_order_lifecycle": True},
        "reconciliation": {"status": "ok", "issues": []},
    }


def test_nautilus_normalized_snapshot_projects_to_canonical_accounting() -> None:
    accounting = project_execution_accounting(_candidate(CYCLE_ID)).to_dict()

    assert accounting["source_name"] == "nautilus_shadow"
    assert accounting["counts"]["fill_count"] == 1
    assert accounting["counts"]["trade_count"] == 1
    assert accounting["counts"]["completed_trade_count"] == 0
    assert accounting["pnl"]["net_realized_pnl"] == -0.005
    assert accounting["reconciliation"]["status"] == "pass"


def test_persists_orders_fills_positions_and_restarts_idempotently(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)

    def replay(_preflight: Path, _input: Path, output_path: Path) -> dict:
        snapshot = _candidate(CYCLE_ID)
        snapshot["orders"][0]["order_id"] = first["order_id"]
        write_json(output_path, [snapshot])
        return snapshot

    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=replay,
    )
    command = {
        "cycle_id": CYCLE_ID,
        "ts": "2026-07-10T01:00:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "market",
        "price": 100.0,
        "quantity": 1.0,
        "source_fill_id": "external-1",
    }
    first = adapter.submit_order(command)
    second = adapter.submit_order(command)
    assert first == second
    assert len(load_json(adapter.root / "commands" / f"{CYCLE_ID}.json")) == 1

    event = {
        "cycle_id": CYCLE_ID,
        "ts_event": "2026-07-10T01:01:00+00:00",
        "event_started_at": "2026-07-10T01:00:00+00:00",
        "price": 100.0,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm",
        "provider": "binance_usdm",
        "instrument_id": "XAUUSDT",
    }
    result = adapter.process_market_event(event)
    assert result["status"] == "replayed"
    assert len(load_json(adapter.root / "orders" / f"{CYCLE_ID}.json")) == 1
    assert len(load_json(adapter.root / "fills" / f"{CYCLE_ID}.json")) == 1
    assert len(load_json(adapter.root / "positions" / f"{CYCLE_ID}.json")) == 1
    assert adapter.reconcile(CYCLE_ID)["status"] == "ok"

    pending = adapter.submit_order({
        **command,
        "ts": "2026-07-10T01:02:00+00:00",
        "source_fill_id": "external-2",
    })
    assert pending["state"] == "accepted"
    assert len(adapter.snapshot(CYCLE_ID)["orders"]) == 2
    assert adapter.reconcile(CYCLE_ID)["status"] == "ok"

    restarted = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=replay,
    )
    assert restarted.snapshot(CYCLE_ID) == adapter.snapshot(CYCLE_ID)
    duplicate = restarted.process_market_event(event)
    assert duplicate["status"] == "idempotent"
    assert len(load_json(adapter.root / "events" / f"{CYCLE_ID}.json")) == 1


def test_order_receipt_keeps_submission_time_and_strategy_traceability(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=lambda *_args: _candidate(CYCLE_ID),
        defer_replay=True,
    )

    receipt = adapter.submit_order({
        "cycle_id": CYCLE_ID,
        "ts": "2026-07-10T01:00:30+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "quantity": 1.0,
        "sl": 95.0,
        "tp": 105.0,
        "strategy_plan_id": "plan-2",
        "strategy_plan_version": 2,
        "source_fill_id": "traceable-order",
    })

    assert receipt["ts"] == "2026-07-10T01:00:30+00:00"
    assert receipt["strategy_plan_id"] == "plan-2"
    assert receipt["strategy_plan_version"] == 2
    assert receipt["sl"] == 95.0
    assert receipt["tp"] == 105.0


def test_replay_sorts_backfilled_market_events_before_live_commands(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    seen_event_times: list[list[str]] = []

    def replay(_preflight: Path, input_path: Path, _output: Path) -> dict:
        bundle = load_json(input_path)[-1]
        seen_event_times.append([row["ts_event"] for row in bundle["market_events"]])
        snapshot = _candidate(CYCLE_ID)
        snapshot["fills"] = []
        snapshot["positions"] = []
        snapshot["orders"] = []
        return snapshot

    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=replay,
        defer_replay=True,
    )
    adapter.submit_order({
        "cycle_id": CYCLE_ID,
        "ts": "2026-07-10T11:52:30+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "quantity": 1.0,
        "source_fill_id": "live-command",
    })
    base = {
        "cycle_id": CYCLE_ID,
        "price": 100.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
    }
    adapter.process_market_event({**base, "event_id": "live", "ts_event": "2026-07-10T11:52:33+00:00"})
    adapter.process_market_event({**base, "event_id": "backfill", "ts_event": "2026-07-10T01:01:00+00:00"})

    adapter.flush(CYCLE_ID)

    assert seen_event_times == [[
        "2026-07-10T01:01:00+00:00",
        "2026-07-10T11:52:33+00:00",
    ]]
    persisted = load_json(adapter.root / "events" / f"{CYCLE_ID}.json")
    assert [row["event_id"] for row in persisted] == ["backfill", "live"]


def test_persisted_fill_history_is_append_only(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=lambda *_args: _candidate(CYCLE_ID),
    )
    first = _candidate(CYCLE_ID)
    adapter._persist_snapshot(CYCLE_ID, first)
    regressed = _candidate(CYCLE_ID)
    regressed["fills"] = []

    with pytest.raises(RuntimeError, match="immutable fill history regressed"):
        adapter._persist_snapshot(CYCLE_ID, regressed)


def test_retries_persisted_event_until_replay_is_acknowledged(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    attempts = []

    def replay(_preflight: Path, _input: Path, _output: Path) -> dict:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("injected replay failure")
        return _candidate(CYCLE_ID)

    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=replay,
    )
    event = {
        "cycle_id": CYCLE_ID,
        "ts_event": "2026-07-10T01:01:00+00:00",
        "price": 100.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm",
        "provider": "binance_usdm",
        "instrument_id": "XAUUSDT",
    }

    try:
        adapter.process_market_event(event)
    except RuntimeError as exc:
        assert "injected" in str(exc)
    else:
        raise AssertionError("first replay should fail")
    assert len(load_json(adapter.root / "events" / f"{CYCLE_ID}.json")) == 1
    assert load_json(adapter.root / "processed_events" / f"{CYCLE_ID}.json") == []

    assert adapter.process_market_event(event)["status"] == "replayed"
    assert adapter.process_market_event(event)["status"] == "idempotent"
    assert len(attempts) == 2
    assert len(load_json(adapter.root / "processed_events" / f"{CYCLE_ID}.json")) == 1


def test_later_success_acknowledges_every_event_in_the_replayed_prefix(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    attempts = []

    def replay(_preflight: Path, _input: Path, _output: Path) -> dict:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("injected first-event failure")
        return _candidate(CYCLE_ID)

    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=replay,
    )
    first = {
        "event_id": "event-1",
        "cycle_id": CYCLE_ID,
        "ts_event": "2026-07-10T01:01:00+00:00",
        "price": 100.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
    }
    second = {**first, "event_id": "event-2", "ts_event": "2026-07-10T01:02:00+00:00", "price": 101.0}

    try:
        adapter.process_market_event(first)
    except RuntimeError:
        pass
    else:
        raise AssertionError("first replay should fail")
    assert adapter.process_market_event(second)["status"] == "replayed"

    processed = load_json(adapter.root / "processed_events" / f"{CYCLE_ID}.json")
    assert [row["event_id"] for row in processed] == ["event-1", "event-2"]
    assert adapter.process_market_event(first)["status"] == "idempotent"


def test_deferred_mode_queues_many_events_and_replays_the_batch_once(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    attempts = []

    def replay(_preflight: Path, _input: Path, _output: Path) -> dict:
        attempts.append(1)
        return _candidate(CYCLE_ID)

    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=replay,
        defer_replay=True,
    )
    adapter.submit_order({
        "cycle_id": CYCLE_ID,
        "ts": "2026-07-10T01:00:30+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "quantity": 1.0,
        "source_fill_id": "deferred-command-1",
    })
    base = {
        "cycle_id": CYCLE_ID,
        "price": 100.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
    }

    assert adapter.process_market_event({**base, "event_id": "event-1", "ts_event": "2026-07-10T01:01:00+00:00"})["status"] == "queued"
    assert adapter.process_market_event({**base, "event_id": "event-2", "ts_event": "2026-07-10T01:02:00+00:00"})["status"] == "queued"
    assert attempts == []

    result = adapter.flush(CYCLE_ID)

    assert result["status"] == "replayed"
    assert result["processed_event_count"] == 2
    assert result["processed_command_count"] == 1
    assert len(attempts) == 1
    assert [row["event_id"] for row in load_json(adapter.root / "processed_events" / f"{CYCLE_ID}.json")] == [
        "event-1",
        "event-2",
    ]
    assert adapter.flush(CYCLE_ID)["status"] == "idempotent"
    assert len(attempts) == 1
    assert len(load_json(adapter.root / "processed_commands" / f"{CYCLE_ID}.json")) == 1


def test_persists_idempotent_cancel_commands_for_authoritative_order_ids(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=lambda *_args: _candidate(CYCLE_ID),
        defer_replay=True,
    )
    order = adapter.submit_order({
        "cycle_id": CYCLE_ID,
        "ts": "2026-07-10T01:00:30+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "quantity": 1.0,
        "source_fill_id": "strategy-grid:plan-1:order-1",
        "authoritative_order_id": "legacy-order-1",
        "strategy_plan_id": "plan-1",
        "strategy_plan_version": 1,
    })

    first = adapter.cancel_orders(
        CYCLE_ID,
        order_ids=["legacy-order-1"],
        ts="2026-07-10T01:05:00+00:00",
        reason="regrid",
    )
    second = adapter.cancel_orders(
        CYCLE_ID,
        order_ids=["legacy-order-1"],
        ts="2026-07-10T01:05:00+00:00",
        reason="regrid",
    )

    assert order["state"] == "accepted"
    assert first == second
    assert first["cancelled_order_ids"] == ["legacy-order-1"]
    commands = load_json(adapter.root / "commands" / f"{CYCLE_ID}.json")
    assert len(commands) == 2
    assert commands[-1]["command"]["event"] == "cancel"
    assert commands[-1]["command"]["cancel_order_id"] == commands[0]["command_id"]
    assert commands[-1]["command"]["strategy_plan_id"] == "plan-1"


def test_close_command_resolves_one_hedged_position_and_persists_target_identity(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=lambda *_args: _candidate(CYCLE_ID),
        defer_replay=True,
    )
    snapshot = _candidate(CYCLE_ID)
    snapshot["positions"] = [{
        "trade_id": "nautilus-entry-1",
        "position_id": "POS-nautilus-entry-1",
        "status": "open",
        "side": "long",
        "remaining_units": 0.25,
        "entry_price": 4000.0,
        "strategy_plan_id": "plan-1",
        "strategy_plan_version": 1,
    }]
    adapter._persist_snapshot(CYCLE_ID, snapshot)

    with pytest.raises(ValueError, match="side does not reduce"):
        adapter.submit_order({
            "cycle_id": CYCLE_ID,
            "ts": "2026-07-10T01:04:00+00:00",
            "side": "buy",
            "event": "flatten",
            "order_type": "market",
            "price": 4010.0,
            "target_position_side": "long",
            "target_entry_price": 4000.0,
            "strategy_plan_id": "plan-1",
            "source_fill_id": "wrong-side-close",
        })

    receipt = adapter.submit_order({
        "cycle_id": CYCLE_ID,
        "ts": "2026-07-10T01:05:00+00:00",
        "side": "sell",
        "event": "flatten",
        "order_type": "market",
        "price": 4010.0,
        "trade_id": "legacy-trade-1",
        "target_position_side": "long",
        "target_entry_price": 4000.0,
        "strategy_plan_id": "plan-1",
        "strategy_plan_version": 1,
        "source_fill_id": "strategy-stop:legacy-trade-1",
    })

    command = load_json(adapter.root / "commands" / f"{CYCLE_ID}.json")[-1]["command"]
    assert receipt["state"] == "accepted"
    assert command["target_position_id"] == "POS-nautilus-entry-1"
    assert command["target_command_id"] == "nautilus-entry-1"
    assert command["quantity"] == 0.25


def test_reconciliation_detects_account_identity_and_traceability_drift(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=lambda *_args: _candidate(CYCLE_ID),
    )
    snapshot = _candidate(CYCLE_ID)
    snapshot["account"] = {
        "starting_cash": 10_000.0,
        "realized_pnl": 10.0,
        "ending_cash": 9_999.0,
        "equity": 9_999.0,
        "fees": 1.0,
        "margin": 99.0,
        "exposure": 100.0,
        "slippage": 0.0,
    }
    snapshot["fills"][0]["cost"] = 0.5
    snapshot["positions"][0]["strategy_plan_id"] = None
    adapter._persist_snapshot(CYCLE_ID, snapshot)

    report = adapter.reconcile(CYCLE_ID)

    assert report["status"] == "drift"
    codes = {row["code"] for row in report["issues"]}
    assert {
        "account_ending_cash_mismatch",
        "account_fees_mismatch",
        "account_margin_mismatch",
        "open_position_strategy_plan_missing",
    } <= codes


def test_rejects_market_event_without_execution_identity(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=lambda *_args: _candidate(CYCLE_ID),
    )
    try:
        adapter.process_market_event({
            "cycle_id": CYCLE_ID,
            "ts_event": "2026-07-10T01:01:00+00:00",
            "price": 100.0,
            "fresh": True,
            "is_synthetic": False,
            "source": "test",
        })
    except ValueError as exc:
        assert "provider" in str(exc)
    else:
        raise AssertionError("missing provider should fail closed")


def test_rejects_persistent_adapter_with_assumed_fees(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    rows = load_json(preflight)
    rows[-1]["fee_model"]["mode"] = "paper_assumption"
    write_json(preflight, rows)

    try:
        NautilusExecutionAdapter(
            output,
            nautilus_python=tmp_path / "unused-python",
            preflight_path=preflight,
            replay_executor=lambda *_args: _candidate(CYCLE_ID),
        )
    except RuntimeError as exc:
        assert "account-observed" in str(exc)
    else:
        raise AssertionError("assumed fees must not enable the persistent adapter")
