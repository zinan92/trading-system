from __future__ import annotations

from pathlib import Path

import pytest

from services.dualtrack_execution_adapter import (
    ExecutionEngineAdapter,
    LegacyPaperExecutionAdapter,
    build_execution_engine_adapter,
)
from services.accounting_projection import project_execution_accounting
from services.journal_store import load_json
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


def _entry() -> dict:
    return {
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T01:02:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 95.0,
        "tp": 110.0,
        "source": "adapter_contract_test",
    }


def test_legacy_adapter_exposes_canonical_execution_snapshot(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)

    order = adapter.submit_order(_entry())
    snapshot = adapter.snapshot(
        "2026-07-05_DAY",
        mark_price=105.0,
        mark_fresh=True,
        mark_source="canonical_test_feed",
    )

    assert isinstance(adapter, ExecutionEngineAdapter)
    assert order["state"] == "accepted"
    assert snapshot["schema_version"] == "dualtrack-execution-v1"
    assert snapshot["engine"] == "legacy_paper"
    assert snapshot["cycle_id"] == "2026-07-05_DAY"
    assert snapshot["orders"] == [{
        "order_id": order["order_id"],
        "state": "accepted",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "quantity": 10.0,
        "notional": 1000.0,
        "sl": 95.0,
        "tp": 110.0,
        "ts": "2026-07-05T01:02:00+00:00",
        "source": "adapter_contract_test",
    }]
    assert snapshot["fills"] == []
    assert snapshot["positions"] == []
    assert snapshot["pnl"] == {"realized": 0, "unrealized": 0}
    assert snapshot["capabilities"]["native_order_lifecycle"] is False
    assert snapshot["capabilities"]["order_lifecycle"] == "derived_from_fill_ledger"
    command_rows = load_json(tmp_path / "outputs" / "dualtrack" / "shadow_commands" / "2026-07-05_DAY.json")
    assert command_rows[0]["command_id"] == order["order_id"]
    assert command_rows[0]["command"]["order_type"] == "limit"

    accounting = project_execution_accounting(snapshot).to_dict()
    assert accounting["source_name"] == "legacy_paper"
    assert accounting["counts"]["order_count"] == 1
    assert accounting["counts"]["trade_count"] == 0
    assert accounting["counts"]["completed_trade_count"] == 0
    assert accounting["pnl"]["net_realized_pnl"] == 0.0


def test_empty_snapshot_keeps_configured_paper_account_equity(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)

    snapshot = adapter.snapshot("2026-07-05_DAY")

    assert snapshot["account"]["starting_cash"] == TEST_CONFIG["capital_per_track_usd"]
    assert snapshot["account"]["ending_cash"] == TEST_CONFIG["capital_per_track_usd"]
    assert snapshot["account"]["equity"] == TEST_CONFIG["capital_per_track_usd"]
    assert snapshot["account"]["funding"] == 0.0


def test_protective_exit_inherits_strategy_plan_traceability(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)
    command = {
        **_entry(),
        "order_type": "market",
        "market_price": 100.0,
        "strategy_plan_id": "strategy-plan-2026-07-05_DAY-2-test",
        "strategy_plan_version": 2,
    }
    adapter.submit_order(command)

    result = adapter.process_market_event({
        "schema_version": "canonical-market-event-v1",
        "event_id": "bar-1",
        "cycle_id": "2026-07-05_DAY",
        "ts_event": "2026-07-05T01:03:00+00:00",
        "event_started_at": "2026-07-05T01:03:00+00:00",
        "source": "canonical_test_feed",
        "symbol": "GOLD",
        "timeframe": "1m",
        "open": 100.0,
        "high": 111.0,
        "low": 99.0,
        "close": 110.0,
        "price": 110.0,
        "is_synthetic": False,
        "fresh": True,
    })

    assert result["triggered"][0]["fill_id"]
    exit_fill = adapter.snapshot("2026-07-05_DAY")["fills"][-1]
    assert exit_fill["strategy_plan_id"] == command["strategy_plan_id"]
    assert exit_fill["strategy_plan_version"] == 2


def test_legacy_adapter_command_journal_is_idempotent_for_retried_fill(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)
    command = {**_entry(), "source_fill_id": "shadow-command-retry"}

    first = adapter.submit_order(command)
    second = adapter.submit_order(command)

    assert first["order_id"] == second["order_id"]
    rows = load_json(tmp_path / "outputs" / "dualtrack" / "shadow_commands" / "2026-07-05_DAY.json")
    assert len(rows) == 1


def test_legacy_adapter_fills_marketable_limit_immediately_at_trusted_mark(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)

    fill = adapter.submit_order({
        **_entry(),
        "market_price": 99.0,
        "market_timestamp": "2026-07-05T01:01:59+00:00",
        "market_source": "canonical_test_feed",
    })
    snapshot = adapter.snapshot("2026-07-05_DAY", mark_price=99.0, mark_fresh=True)

    assert fill["fill_id"]
    assert fill["price"] == 99.0
    assert fill["requested_price"] == 100.0
    assert fill["liquidity"] == "taker"
    assert snapshot["orders"][0]["state"] == "filled"
    assert snapshot["positions"][0]["status"] == "open"


def test_legacy_adapter_market_event_executes_protection_and_reconciles(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)
    adapter.submit_order(_entry())

    event = adapter.process_market_event({
        "cycle_id": "2026-07-05_DAY",
        "ts_event": "2026-07-05T01:03:00+00:00",
        "price": 99.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "canonical_test_feed",
    })
    assert event["accepted_limit_fill_count"] == 1
    event = adapter.process_market_event({
        "cycle_id": "2026-07-05_DAY",
        "ts_event": "2026-07-05T01:04:00+00:00",
        "price": 94.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "canonical_test_feed",
    })
    reconciliation = adapter.reconcile("2026-07-05_DAY")
    snapshot = adapter.snapshot("2026-07-05_DAY", mark_price=94.0, mark_fresh=True)

    assert event["status"] == "triggered"
    assert event["triggered"][0]["event"] == "stop"
    assert snapshot["positions"][0]["status"] == "closed"
    assert snapshot["positions"][0]["remaining_units"] == 0.0
    assert reconciliation["status"] == "ok"
    assert reconciliation["issues"] == []


def test_legacy_adapter_snapshot_exposes_margin_exposure_and_explicit_zero_slippage(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)
    adapter.submit_order({**_entry(), "order_type": "market", "notional": 100.0})

    snapshot = adapter.snapshot("2026-07-05_DAY", mark_price=100.0, mark_fresh=True)

    assert snapshot["account"]["exposure"] == pytest.approx(100.0)
    assert snapshot["account"]["margin"] == pytest.approx(100.0)
    assert snapshot["account"]["slippage"] == 0.0


def test_legacy_adapter_rejects_untrusted_market_event(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)

    with pytest.raises(ValueError, match="synthetic market data is forbidden"):
        adapter.process_market_event({
            "cycle_id": "2026-07-05_DAY",
            "ts_event": "2026-07-05T01:03:00+00:00",
            "price": 94.0,
            "fresh": True,
            "is_synthetic": True,
            "source": "synthetic_seed",
        })


def test_legacy_adapter_cancels_selected_pending_orders_through_execution_contract(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)
    first = adapter.submit_order({**_entry(), "source_fill_id": "cancel-1"})
    second = adapter.submit_order({**_entry(), "price": 101.0, "source_fill_id": "cancel-2"})

    result = adapter.cancel_orders(
        "2026-07-05_DAY",
        order_ids=[first["order_id"]],
        ts="2026-07-05T01:03:00+00:00",
        reason="regrid",
    )

    assert result["status"] == "cancelled"
    assert result["cancelled_order_ids"] == [first["order_id"]]
    states = {row["order_id"]: row["state"] for row in adapter.snapshot("2026-07-05_DAY")["orders"]}
    assert states[first["order_id"]] == "cancelled"
    assert states[second["order_id"]] == "accepted"


def test_adapter_factory_fails_closed_without_attended_nautilus_approval(tmp_path: Path) -> None:
    assert build_execution_engine_adapter(tmp_path / "outputs", engine="legacy_paper").name == "legacy_paper"

    with pytest.raises(RuntimeError, match="attended approval"):
        build_execution_engine_adapter(tmp_path / "outputs", engine="nautilus")


def test_adapter_factory_requires_passed_gate_before_nautilus_switch(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    with pytest.raises(RuntimeError, match="evidence gate is not ready"):
        build_execution_engine_adapter(
            output,
            engine="nautilus_paper",
            nautilus_python=tmp_path / "python",
            allow_paper_switch=True,
        )
