from __future__ import annotations

from pathlib import Path

import pytest

from services.accounting_projection import project_execution_accounting
from services.dualtrack_nautilus_execution_adapter import NautilusExecutionAdapter
from services.grid_lifecycle_evidence import build_grid_lifecycle_evidence
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
            "strategy_plan_id": "plan-test", "strategy_plan_version": 1,
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


def _grid_command() -> dict:
    return {
        "cycle_id": CYCLE_ID,
        "ts": "2026-07-10T01:00:30+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 4000.0,
        "quantity": 1.0,
        "sl": 3990.0,
        "tp": 4010.0,
        "source": "strategy_production_console",
        "source_fill_id": "strategy-grid:plan-grid:preview-1-buy",
        "strategy_plan_id": "plan-grid",
        "strategy_plan_version": 1,
    }


def _grid_market_event(index: int, price: float) -> dict:
    return {
        "event_id": f"grid-event-{index}",
        "cycle_id": CYCLE_ID,
        "ts_event": f"2026-07-10T01:0{index}:00+00:00",
        "price": price,
        "open": price,
        "high": price,
        "low": price,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
    }


def _open_grid_replay(_preflight_path: Path, input_path: Path, output_path: Path) -> dict:
    bundle = load_json(input_path)[-1]
    row = bundle["commands"][0]
    command_id = row["command_id"]
    command = row["command"]
    event = bundle["market_events"][-1]
    fill = {
        "fill_id": f"fill-{command_id}",
        "order_id": command_id,
        "trade_id": command_id,
        "event": "entry",
        "side": "buy",
        "price": 4000.0,
        "quantity": 1.0,
        "cost": 0.0,
        "realized_pnl": 0.0,
        "ts": "2026-07-10T01:01:00+00:00",
        "strategy_plan_id": command["strategy_plan_id"],
        "strategy_plan_version": command["strategy_plan_version"],
    }
    snapshot = {
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_shadow",
        "cycle_id": bundle["cycle_id"],
        "orders": [{
            "order_id": command_id,
            "state": "accepted",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": 4000.0,
            "quantity": 1.0,
            "strategy_plan_id": command["strategy_plan_id"],
            "strategy_plan_version": command["strategy_plan_version"],
        }],
        "fills": [fill],
        "positions": [{
            "trade_id": command_id,
            "position_id": f"POS-{command_id}",
            "status": "open",
            "side": "long",
            "remaining_units": 1.0,
            "entry_price": 4000.0,
            "entry_ts": fill["ts"],
            "realized_pnl": 0.0,
            "unrealized_pnl": 1.0,
            "strategy_plan_id": command["strategy_plan_id"],
            "strategy_plan_version": command["strategy_plan_version"],
        }],
        "account": {
            "starting_cash": 10_000.0,
            "realized_pnl": 0.0,
            "ending_cash": 10_000.0,
            "equity": 10_001.0,
            "margin": 400.0,
            "exposure": 4_000.0,
            "slippage": 0.0,
            "fees": 0.0,
            "funding": 0.0,
        },
        "pnl": {"realized": 0.0, "unrealized": 1.0},
        "mark": {"price": event["price"], "fresh": True, "source": event["source"]},
        "capabilities": {"native_order_lifecycle": True},
        "reconciliation": {"status": "ok", "issues": []},
    }
    write_json(output_path, [snapshot])
    return snapshot


def test_cycle_handoff_preserves_order_position_and_lifecycle_identity(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    config = {
        "capital_per_track_usd": 10_000.0,
        "max_leverage": 10.0,
        "paper_fee_model": {
            "maker_fee_rate": 0.0,
            "taker_fee_rate": 0.0004,
            "real_money_eligible": False,
        },
    }
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python="/usr/bin/python3",
        storage_namespace="nautilus_authoritative",
        preflight_path=preflight,
        replay_executor=_open_grid_replay,
        config=config,
    )
    receipt = adapter.submit_order(_grid_command())
    adapter.process_market_event(_grid_market_event(1, 4001.0))
    previous = adapter.snapshot(CYCLE_ID)
    previous_lifecycle = load_json(
        output / "dualtrack" / "grid_lifecycle" / f"{CYCLE_ID}_nautilus.json"
    )

    current_cycle_id = "2026-07-10_NIGHT"
    handoff = adapter.handoff_cycle(
        CYCLE_ID,
        current_cycle_id,
        current_strategy_plan_id="plan-current",
        boundary_at="2026-07-10T13:00:00+00:00",
    )
    current = adapter.snapshot(current_cycle_id)
    current_lifecycle = load_json(
        output / "dualtrack" / "grid_lifecycle" / f"{current_cycle_id}_nautilus.json"
    )

    assert handoff["status"] == "verified"
    assert handoff["identity_preserved"] is True
    assert handoff["accepted_order_ids"] == [receipt["order_id"]]
    assert [row["order_id"] for row in current["orders"]] == [
        row["order_id"] for row in previous["orders"]
    ]
    assert [row["position_id"] for row in current["positions"]] == [
        row["position_id"] for row in previous["positions"]
    ]
    assert {row["line_id"] for row in current_lifecycle} == {
        row["line_id"] for row in previous_lifecycle
    }
    assert current["fills"] == []
    assert current["pnl"]["realized"] == 0.0
    assert current["pnl"]["unrealized"] == previous["pnl"]["unrealized"]
    assert current["account"]["starting_cash"] == previous["account"]["ending_cash"]
    assert adapter.reconcile(current_cycle_id)["status"] == "ok"


def _completed_grid_replay(input_path: Path, output_path: Path) -> dict:
    bundle = load_json(input_path)[-1]
    commands = [
        row for row in bundle["commands"]
        if str((row.get("command") or {}).get("event") or "entry") == "entry"
    ]
    orders = []
    fills = []
    positions = []
    fill_times = {
        1: ("2026-07-10T01:01:00+00:00", "2026-07-10T01:02:00+00:00"),
        2: ("2026-07-10T01:03:00+00:00", "2026-07-10T01:04:00+00:00"),
    }
    for row in commands:
        command_id = row["command_id"]
        command = row["command"]
        generation = int(command.get("grid_generation") or 1)
        completed = generation in fill_times
        orders.append({
            "order_id": command_id,
            "state": "filled" if completed else "accepted",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": 4000.0,
            "quantity": 1.0,
            "strategy_plan_id": "plan-grid",
            "strategy_plan_version": 1,
        })
        if not completed:
            continue
        entry_at, target_at = fill_times[generation]
        orders.append({
            "order_id": f"{command_id}-TP",
            "state": "filled",
            "side": "sell",
            "event": "target",
            "order_type": "limit",
            "price": 4010.0,
            "quantity": 1.0,
            "strategy_plan_id": "plan-grid",
            "strategy_plan_version": 1,
        })
        fills.extend([
            {
                "fill_id": f"nautilus-{command_id}",
                "order_id": command_id,
                "trade_id": command_id,
                "event": "entry",
                "side": "buy",
                "price": 4000.0,
                "quantity": 1.0,
                "cost": 0.0,
                "ts": entry_at,
            },
            {
                "fill_id": f"nautilus-{command_id}-TP",
                "order_id": f"{command_id}-TP",
                "trade_id": command_id,
                "event": "target",
                "side": "sell",
                "price": 4010.0,
                "quantity": 1.0,
                "cost": 0.0,
                "ts": target_at,
            },
        ])
        positions.append({
            "trade_id": command_id,
            "position_id": f"POS-{command_id}",
            "status": "closed",
            "side": "long",
            "remaining_units": 0.0,
            "entry_price": 4000.0,
            "exit_price": 4010.0,
            "entry_ts": entry_at,
            "exit_ts": target_at,
            "realized_pnl": 10.0,
            "strategy_plan_id": "plan-grid",
            "strategy_plan_version": 1,
        })
    snapshot = {
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_shadow",
        "cycle_id": CYCLE_ID,
        "orders": orders,
        "fills": fills,
        "positions": positions,
        "account": {
            "starting_cash": 10_000.0,
            "realized_pnl": 20.0,
            "ending_cash": 10_020.0,
            "equity": 10_020.0,
            "margin": 0.0,
            "exposure": 0.0,
            "slippage": 0.0,
            "fees": 0.0,
            "funding": 0.0,
        },
        "pnl": {"realized": 20.0, "unrealized": 0.0},
        "mark": {"price": 4010.0, "fresh": True, "source": "test"},
        "capabilities": {"native_order_lifecycle": True},
        "reconciliation": {"status": "ok", "issues": []},
    }
    write_json(output_path, [snapshot])
    return snapshot


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
        snapshot["orders"][0]["strategy_plan_id"] = "plan-1"
        snapshot["orders"][0]["strategy_plan_version"] = 1
        snapshot["fills"][0].update(
            {
                "order_id": first["order_id"],
                "trade_id": first["order_id"],
                "strategy_plan_id": "plan-1",
                "strategy_plan_version": 1,
            }
        )
        snapshot["positions"][0].update(
            {
                "trade_id": first["order_id"],
                "position_id": f"POS-{first['order_id']}",
                "strategy_plan_id": "plan-1",
                "strategy_plan_version": 1,
            }
        )
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
        "symbol": "GOLD",
        "side": "buy",
        "event": "entry",
        "order_type": "market",
        "price": 100.0,
        "quantity": 1.0,
        "notional": 100.0,
        "sl": 90.0,
        "tp": 110.0,
        "source_fill_id": "external-1",
        "strategy_plan_id": "plan-1",
        "strategy_plan_version": 1,
    }
    first = adapter.submit_order(command)
    second = adapter.submit_order(command)
    assert first == second
    assert {
        key: first[key]
        for key in (
            "symbol",
            "notional",
            "sl",
            "tp",
            "source_fill_id",
            "strategy_plan_id",
            "strategy_plan_version",
        )
    } == {
        key: command[key]
        for key in (
            "symbol",
            "notional",
            "sl",
            "tp",
            "source_fill_id",
            "strategy_plan_id",
            "strategy_plan_version",
        )
    }
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


def test_authoritative_grid_rearms_same_price_for_two_complete_cycles(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)

    def replay(_preflight: Path, input_path: Path, output_path: Path) -> dict:
        return _completed_grid_replay(input_path, output_path)

    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        storage_namespace="nautilus_authoritative",
        preflight_path=preflight,
        replay_executor=replay,
        defer_replay=True,
    )
    first_order = adapter.submit_order(_grid_command())
    for index, price in enumerate((4000.0, 4010.0, 4000.0, 4010.0), start=1):
        adapter.process_market_event(_grid_market_event(index, price))

    result = adapter.flush(CYCLE_ID)
    snapshot = result["snapshot"]
    commands = load_json(adapter.root / "commands" / f"{CYCLE_ID}.json")
    entry_commands = [
        row for row in commands
        if str((row.get("command") or {}).get("event") or "entry") == "entry"
    ]

    assert snapshot["rearms"] == 2
    assert snapshot["grid_lifecycle"]["rearms"] == 2
    assert [row["command"].get("grid_generation", 1) for row in entry_commands] == [1, 2, 3]
    assert [row["command"]["price"] for row in entry_commands] == [4000.0, 4000.0, 4000.0]
    assert [row["event"] for row in snapshot["fills"]] == ["entry", "target", "entry", "target"]
    assert len({row["trade_id"] for row in snapshot["fills"] if row["event"] == "entry"}) == 2
    assert not [row for row in snapshot["positions"] if row["status"] == "open"]
    accepted = [row for row in snapshot["orders"] if row["state"] == "accepted"]
    assert len(accepted) == 1
    assert accepted[0]["price"] == 4000.0
    assert accepted[0]["order_id"] != first_order["order_id"]
    lifecycle = load_json(
        output / "dualtrack" / "grid_lifecycle" / f"{CYCLE_ID}_nautilus.json"
    )
    assert [
        row["event"] for row in lifecycle if row["event"] != "armed"
    ] == [
        "entry_fill_confirmed",
        "close_fill_confirmed_rearm",
        "entry_fill_confirmed",
        "close_fill_confirmed_rearm",
    ]
    evidence = build_grid_lifecycle_evidence(
        output,
        cycle_id=CYCLE_ID,
        execution_snapshot=snapshot,
        reconciliation=adapter.reconcile(CYCLE_ID),
    )
    assert evidence["status"] == "verified"
    assert evidence["completed_rearmed_count"] == 2
    first_loop = evidence["lines"][0]
    assert first_loop["status"] == "completed_rearmed"
    assert first_loop["strategy_plan_id"] == "plan-grid"
    assert first_loop["entry"]["trade_id"] == first_loop["entry"]["order_id"]
    assert first_loop["target"]["fill_ids"]
    assert first_loop["reorder"]["price"] == first_loop["reorder"]["original_price"] == 4000.0

    # A missing transition cannot be turned into a claimed completed loop.
    write_json(
        output / "dualtrack" / "grid_lifecycle" / f"{CYCLE_ID}_nautilus.json",
        [row for row in lifecycle if row["event"] != "close_fill_confirmed_rearm"],
    )
    incomplete = build_grid_lifecycle_evidence(
        output,
        cycle_id=CYCLE_ID,
        execution_snapshot=snapshot,
        reconciliation=adapter.reconcile(CYCLE_ID),
    )
    assert incomplete["status"] == "unverified"
    assert incomplete["completed_rearmed_count"] == 0
    assert "target_rearm_transition" in incomplete["lines"][0]["evidence_missing"]

    restarted = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        storage_namespace="nautilus_authoritative",
        preflight_path=preflight,
        replay_executor=replay,
        defer_replay=True,
    )
    assert restarted.snapshot(CYCLE_ID) == snapshot
    assert restarted.flush(CYCLE_ID)["status"] == "idempotent"
    assert restarted.reconcile(CYCLE_ID)["status"] == "ok"


def test_market_event_identity_evidence_is_exact_and_fail_closed(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        storage_namespace="nautilus_authoritative",
        preflight_path=preflight,
        replay_executor=lambda *_args: {},
        defer_replay=True,
    )
    event_ids = ["event-1", "event-2", "event-3"]
    write_json(
        adapter.root / "events" / f"{CYCLE_ID}.json",
        [
            {"cycle_id": CYCLE_ID, "event_id": event_id}
            for event_id in event_ids
        ],
    )
    write_json(
        adapter.root / "processed_events" / f"{CYCLE_ID}.json",
        [
            {
                "cycle_id": CYCLE_ID,
                "event_id": "event-1",
                "disposition": "accepted",
            },
            {
                "cycle_id": CYCLE_ID,
                "event_id": "event-3",
                "disposition": "late_ignored",
            },
        ],
    )

    assert adapter.persisted_market_event_ids(CYCLE_ID) == frozenset(event_ids)
    assert adapter.processed_market_event_ids(CYCLE_ID) == frozenset(
        {"event-1", "event-3"}
    )
    assert "event-2" not in adapter.settled_market_event_ids(CYCLE_ID)

    write_json(
        adapter.root / "processed_events" / f"{CYCLE_ID}.json",
        [
            {
                "cycle_id": CYCLE_ID,
                "event_id": "event-1",
                "disposition": "unknown",
            }
        ],
    )
    with pytest.raises(
        ValueError,
        match="processed_market_event_disposition_invalid",
    ):
        adapter.processed_market_event_ids(CYCLE_ID)

    write_json(
        adapter.root / "processed_events" / f"{CYCLE_ID}.json",
        [
            {
                "cycle_id": CYCLE_ID,
                "event_id": "event-not-persisted",
                "disposition": "accepted",
            }
        ],
    )
    with pytest.raises(
        ValueError,
        match="processed_market_event_missing_persisted_event",
    ):
        adapter.processed_market_event_ids(CYCLE_ID)


def test_persisted_but_unprocessed_event_is_not_settled(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        storage_namespace="nautilus_authoritative",
        preflight_path=preflight,
        replay_executor=lambda *_args: {},
        defer_replay=True,
    )
    event = _grid_market_event(1, 4000.0)

    queued = adapter.process_market_event(event)

    assert queued["status"] == "queued"
    assert adapter.persisted_market_event_ids(CYCLE_ID) == frozenset(
        {event["event_id"]}
    )
    assert adapter.processed_market_event_ids(CYCLE_ID) == frozenset()
    assert adapter.settled_market_event_ids(CYCLE_ID) == frozenset()


def test_authoritative_grid_does_not_rearm_after_plan_cancel(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)

    def replay(_preflight: Path, input_path: Path, output_path: Path) -> dict:
        return _completed_grid_replay(input_path, output_path)

    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        storage_namespace="nautilus_authoritative",
        preflight_path=preflight,
        replay_executor=replay,
        defer_replay=True,
    )
    order = adapter.submit_order(_grid_command())
    adapter.cancel_orders(
        CYCLE_ID,
        order_ids=[order["order_id"]],
        ts="2026-07-10T01:02:30+00:00",
        reason="stop",
    )
    adapter.process_market_event(_grid_market_event(1, 4000.0))
    adapter.process_market_event(_grid_market_event(2, 4010.0))

    snapshot = adapter.flush(CYCLE_ID)["snapshot"]
    commands = load_json(adapter.root / "commands" / f"{CYCLE_ID}.json")

    assert len([row for row in commands if (row.get("command") or {}).get("event") == "entry"]) == 1
    assert snapshot["rearms"] == 0
    assert not [
        row for row in load_json(
            output / "dualtrack" / "grid_lifecycle" / f"{CYCLE_ID}_nautilus.json"
        )
        if row["event"] == "close_fill_confirmed_rearm"
    ]


def test_manual_bracket_is_not_silently_promoted_to_rearming_grid(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)

    def replay(_preflight: Path, input_path: Path, output_path: Path) -> dict:
        return _completed_grid_replay(input_path, output_path)

    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        storage_namespace="nautilus_authoritative",
        preflight_path=preflight,
        replay_executor=replay,
        defer_replay=True,
    )
    adapter.submit_order({
        **_grid_command(),
        "source": "manual_ticket",
        "source_fill_id": "manual-bracket",
    })
    adapter.process_market_event(_grid_market_event(1, 4000.0))
    adapter.process_market_event(_grid_market_event(2, 4010.0))

    snapshot = adapter.flush(CYCLE_ID)["snapshot"]

    assert len(load_json(adapter.root / "commands" / f"{CYCLE_ID}.json")) == 1
    assert snapshot["rearms"] == 0
    assert snapshot["grid_lifecycle"]["line_count"] == 0


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


def test_late_event_is_audited_without_revising_an_immutable_fill(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    replayed_timestamps: list[list[str]] = []

    def replay(_preflight: Path, input_path: Path, _output: Path) -> dict:
        event_timestamps = [
            str(row["ts_event"])
            for row in load_json(input_path)[-1]["market_events"]
        ]
        replayed_timestamps.append(event_timestamps)
        candidate = _candidate(CYCLE_ID)
        candidate["fills"][0]["ts"] = min(event_timestamps)
        candidate["orders"][0]["ts"] = min(event_timestamps)
        candidate["positions"][0]["entry_ts"] = min(event_timestamps)
        return candidate

    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=replay,
    )
    base = {
        "cycle_id": CYCLE_ID,
        "price": 100.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
    }
    current = {
        **base,
        "event_id": "event-current",
        "ts_event": "2026-07-10T01:41:00+00:00",
    }
    late = {
        **base,
        "event_id": "event-late",
        "ts_event": "2026-07-10T01:31:00+00:00",
    }

    assert adapter.process_market_event(current)["status"] == "replayed"
    persisted_before = load_json(adapter.root / "fills" / f"{CYCLE_ID}.json")
    assert adapter.process_market_event(late)["status"] == "replayed"

    assert replayed_timestamps == [
        ["2026-07-10T01:41:00+00:00"],
        ["2026-07-10T01:41:00+00:00"],
    ]
    assert load_json(adapter.root / "fills" / f"{CYCLE_ID}.json") == persisted_before
    assert {row["event_id"] for row in load_json(adapter.root / "events" / f"{CYCLE_ID}.json")} == {
        "event-current",
        "event-late",
    }
    processed = load_json(adapter.root / "processed_events" / f"{CYCLE_ID}.json")
    assert processed[-1] == {
        "cycle_id": CYCLE_ID,
        "event_id": "event-late",
        "disposition": "late_ignored",
        "ts_event": "2026-07-10T01:31:00+00:00",
        "reason": "ts_event_not_after_execution_watermark",
        "execution_watermark": "2026-07-10T01:41:00+00:00",
    }


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


def test_command_flush_replays_pending_commands_without_market_events(tmp_path: Path) -> None:
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
    adapter.process_market_event({
        "cycle_id": CYCLE_ID,
        "event_id": "processed-baseline",
        "ts_event": "2026-07-10T01:00:00+00:00",
        "price": 101.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
    })
    adapter.flush(CYCLE_ID)
    attempts.clear()
    adapter.submit_order({
        "cycle_id": CYCLE_ID,
        "ts": "2026-07-10T01:00:30+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "quantity": 1.0,
        "source_fill_id": "command-only-1",
    })

    result = adapter.flush_commands(CYCLE_ID)

    assert result["status"] == "replayed"
    assert result["processed_event_count"] == 0
    assert result["processed_command_count"] == 1
    assert attempts == [1]
    assert [
        row["event_id"]
        for row in load_json(adapter.root / "processed_events" / f"{CYCLE_ID}.json")
    ] == ["processed-baseline"]


def test_command_flush_fails_closed_when_market_event_is_pending(tmp_path: Path) -> None:
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
    queued = adapter.process_market_event({
        "cycle_id": CYCLE_ID,
        "event_id": "pending-event",
        "ts_event": "2026-07-10T01:01:00+00:00",
        "price": 100.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
    })
    assert queued["status"] == "queued"

    with pytest.raises(RuntimeError, match="pending market events"):
        adapter.flush_commands(CYCLE_ID)

    assert attempts == []
    assert load_json(adapter.root / "processed_events" / f"{CYCLE_ID}.json") == []


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


def test_reconciliation_detects_open_position_entry_fill_lineage_drift(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    preflight = (
        output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    )
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=lambda *_args: _candidate(CYCLE_ID),
    )
    snapshot = _candidate(CYCLE_ID)
    snapshot["fills"][0]["trade_id"] = "different-trade"
    adapter._persist_snapshot(CYCLE_ID, snapshot)

    report = adapter.reconcile(CYCLE_ID)

    assert report["status"] == "drift"
    assert "open_position_entry_fill_missing" in {
        row["code"] for row in report["issues"]
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: snapshot["positions"][0].update(
            {"side": "short"}
        ),
        lambda snapshot: (
            snapshot["positions"][0].update({"side": "short"}),
            snapshot["fills"][0].update({"side": "sell"}),
        ),
        lambda snapshot: snapshot["positions"][0].update(
            {"entry_price": 99.0}
        ),
        lambda snapshot: snapshot["fills"][0].update(
            {"strategy_plan_id": None}
        ),
    ],
    ids=[
        "position-vs-fill-side",
        "command-vs-fill-side",
        "entry-price",
        "fill-plan",
    ],
)
def test_reconciliation_detects_open_position_economic_identity_drift(
    tmp_path: Path,
    mutate,
) -> None:
    output = tmp_path / "outputs"
    preflight = (
        output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    )
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=lambda *_args: _candidate(CYCLE_ID),
    )
    snapshot = _candidate(CYCLE_ID)
    mutate(snapshot)
    adapter._persist_snapshot(CYCLE_ID, snapshot)

    report = adapter.reconcile(CYCLE_ID)

    assert report["status"] == "drift"
    assert "open_position_entry_fill_invalid" in {
        row["code"] for row in report["issues"]
    }


def test_reconciliation_detects_entry_quantity_above_authorized_order(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    preflight = (
        output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    )
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=lambda *_args: _candidate(CYCLE_ID),
    )
    snapshot = _candidate(CYCLE_ID)
    snapshot["fills"][0]["quantity"] = 3.0
    snapshot["positions"][0]["remaining_units"] = 3.0
    adapter._persist_snapshot(CYCLE_ID, snapshot)

    report = adapter.reconcile(CYCLE_ID)

    assert report["status"] == "drift"
    assert "open_position_entry_fill_invalid" in {
        row["code"] for row in report["issues"]
    }


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
