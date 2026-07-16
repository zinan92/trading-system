from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from pipelines import dashboard_server
from services.dualtrack_config import dualtrack_config
from services.dualtrack_execution_adapter import build_configured_execution_engine_adapter
from services.journal_store import load_json, write_json
from services.strategy_control_plane import StrategyControlPlane
from tests.test_strategy_control_plane import market, proposal


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get(
    "TRADING_ORCHESTRATOR_NAUTILUS_TEST_PYTHON",
    "/Users/wendy/.local/share/trading-orchestrator/nautilus-1.230.0/bin/python",
))
PREFLIGHT = ROOT / "outputs" / "dualtrack" / "nautilus" / "instrument_preflight.json"


pytestmark = pytest.mark.skipif(
    not RUNTIME.exists() or not PREFLIGHT.exists(),
    reason="isolated Nautilus paper runtime evidence is not installed",
)


def test_attended_nautilus_control_plane_start_regrid_cancel_and_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    legacy_history_path = output / "dualtrack" / "fills" / "2026-07-04_NIGHT_human.json"
    write_json(legacy_history_path, [{
        "fill_id": "legacy-history-proof",
        "cycle_id": "2026-07-04_NIGHT",
        "trade_id": "legacy-history-proof",
        "event": "entry",
        "side": "buy",
        "price": 100.0,
        "pnl_units": 1.0,
        "strategy_plan_id": "historical-plan",
        "strategy_plan_version": 1,
    }])
    legacy_history_before = legacy_history_path.read_bytes()
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    preflight.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(PREFLIGHT, preflight)
    write_json(
        output / "dualtrack" / "cutover" / "shadow_gate_current.json",
        [{"status": "ready_for_attended_paper_switch"}],
    )
    monkeypatch.setenv("TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON", str(RUNTIME))
    config = dualtrack_config()
    config["execution_engine"] = {
        **dict(config.get("execution_engine") or {}),
        "authoritative": "nautilus_paper",
        "shadow": "none",
    }
    plane = StrategyControlPlane(output)
    plane.config = config
    cycle_id = "2026-07-05_DAY"
    shadow_path = output / "dualtrack" / "nautilus_paper" / "commands" / f"{cycle_id}.json"
    write_json(shadow_path, [{
        "schema_version": "dualtrack-shadow-command-v1",
        "command_id": "historical-shadow-order",
        "cycle_id": cycle_id,
        "command": {
            "cycle_id": cycle_id,
            "ts": "2026-07-05T01:00:00+00:00",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": 90.0,
            "quantity": 1.0,
        },
    }])
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    started = plane.control(
        cycle_id,
        "start",
        {"direction": "neutral", "style": "steady"},
        market=market(),
        account={"ending_cash": 10_000.0},
        now="2026-07-05T01:40:00+00:00",
    )
    first_ids = {row["order_id"] for row in plane._accepted_orders(cycle_id)}
    assert started["runtime"]["actual_state"] == "running"
    assert len(first_ids) == started["accepted_orders"] > 0
    assert "historical-shadow-order" not in first_ids
    assert [row["command_id"] for row in load_json(shadow_path)] == ["historical-shadow-order"]
    assert (output / "dualtrack" / "nautilus_authoritative" / "commands" / f"{cycle_id}.json").exists()

    regridded = plane.control(
        cycle_id,
        "adjust_plan",
        {"direction": "neutral", "style": "aggressive"},
        market=market(close=110.2),
        account={"ending_cash": 10_000.0},
        now="2026-07-05T01:41:00+00:00",
    )
    second_ids = {row["order_id"] for row in plane._accepted_orders(cycle_id)}
    assert regridded["cancelled_orders"] == len(first_ids)
    assert len(second_ids) == regridded["created_orders"] > 0
    assert first_ids.isdisjoint(second_ids)

    cancelled = plane.control(
        cycle_id,
        "cancel_all",
        market=market(close=110.2),
        now="2026-07-05T01:42:00+00:00",
    )
    assert cancelled["cancelled_orders"] == len(second_ids)
    assert plane._accepted_orders(cycle_id) == []

    active_adapter = build_configured_execution_engine_adapter(
        output,
        config=config,
        environ=dict(os.environ),
    )
    monkeypatch.setattr(
        dashboard_server,
        "build_configured_execution_engine_adapter",
        lambda _output: active_adapter,
    )
    monkeypatch.setattr(dashboard_server, "dualtrack_config", lambda: config)
    market_fields = {
        "market_price": 110.2,
        "market_timestamp": "2026-07-05T01:42:00+00:00",
        "market_source": "binance_usdm_futures",
    }
    opened = dashboard_server.build_dualtrack_order_post_response(
        {
            "cycle_id": cycle_id,
            "ts": "2026-07-05T01:42:10+00:00",
            "side": "buy",
            "event": "entry",
            "order_type": "market",
            "price": 110.2,
            "quantity": 1.0,
            "source": "manual_cutover_rehearsal",
            **market_fields,
        },
        output_root=output,
    )
    assert opened["status"] == "filled"
    opened_trade_id = str(opened["fill"]["trade_id"])
    closed = dashboard_server.build_dualtrack_order_post_response(
        {
            "cycle_id": cycle_id,
            "ts": "2026-07-05T01:42:20+00:00",
            "side": "sell",
            "event": "exit",
            "order_type": "market",
            "price": 110.2,
            "trade_id": opened_trade_id,
            "source": "manual_cutover_rehearsal",
            **market_fields,
        },
        output_root=output,
    )
    assert closed["status"] == "filled"
    assert closed["fill"]["event"] == "exit"
    assert not [
        row for row in active_adapter.snapshot(cycle_id).get("positions") or []
        if row.get("status") == "open"
    ]

    stopped = plane.control(
        cycle_id,
        "stop",
        market=market(close=110.2),
        now="2026-07-05T01:43:00+00:00",
    )
    assert stopped["runtime"]["actual_state"] == "stopped"
    assert stopped["reconciliation"]["status"] == "ok"
    assert stopped["historical_records_preserved"] is True

    rollback_config = {
        **config,
        "execution_engine": {
            **dict(config.get("execution_engine") or {}),
            "authoritative": "legacy_paper",
            "shadow": "none",
        },
    }
    rolled_back = build_configured_execution_engine_adapter(
        output,
        config=rollback_config,
        environ={},
    )
    assert rolled_back.name == "legacy_paper"
    assert legacy_history_path.read_bytes() == legacy_history_before
    assert (output / "dualtrack" / "nautilus_authoritative" / "snapshots" / f"{cycle_id}.json").exists()
