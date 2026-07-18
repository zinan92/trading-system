from __future__ import annotations

import hashlib
from pathlib import Path

import pipelines.dashboard_server as dashboard_server
from services.dashboard_state import DashboardState
from services.journal_store import write_json


def _fingerprint(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_all_dashboard_snapshot_get_contracts_are_durable_state_read_only(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    output.mkdir()
    state = DashboardState(output_root=output, market_db=tmp_path / "missing-market.db")
    builders = {
        "/api/dashboard": lambda payload: payload,
        "/api/system/status": dashboard_server.build_system_status_contract,
        "/api/trader/overview": dashboard_server.build_trader_overview_contract,
        "/api/ops/status": dashboard_server.build_ops_status_contract,
    }
    before = _fingerprint(output)

    for endpoint, builder in builders.items():
        payload = state.snapshot("2026-07-18")
        response = builder(payload)

        assert isinstance(response, dict), endpoint
        assert _fingerprint(output) == before, endpoint


def test_unmigrated_console_get_is_pure_and_threads_one_market_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-18_DAY"
    legacy_plan = {
        "cycle_id": cycle_id,
        "author": "human",
        "locked_at": "2026-07-18T01:00:00+00:00",
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 3900.0, "high": 4100.0},
        "grid": {"count": 50, "mode": "arithmetic", "notional_per_grid": 2800.0},
    }
    write_json(output / "dualtrack" / "plans" / f"{cycle_id}_human.json", [legacy_plan])
    market = {
        "schema_version": "dualtrack-market-bars-v1",
        "provider": "venue-a",
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "latest_close": 4004.0,
        "bars": [],
    }
    received_market_ids: list[int] = []

    monkeypatch.setattr(
        dashboard_server,
        "build_dualtrack_cycle_current_response",
        lambda **_kwargs: {"cycle_id": cycle_id},
    )
    monkeypatch.setattr(
        dashboard_server,
        "build_dualtrack_market_bars_response",
        lambda **_kwargs: market,
    )

    def trades(*_args, **kwargs):
        received_market_ids.append(id(kwargs["market_snapshot"]))
        return {"trades": []}

    def execution(*_args, **kwargs):
        received_market_ids.append(id(kwargs["market_snapshot"]))
        return {
            "schema_version": "dualtrack-execution-v1",
            "engine": "engine-a",
            "orders": [],
            "fills": [],
            "positions": [],
            "account": {},
            "accounting_snapshot": {"snapshot_id": "current-accounting"},
            "shadow_cutover": {},
        }

    monkeypatch.setattr(dashboard_server, "build_dualtrack_trades_response", trades)
    monkeypatch.setattr(dashboard_server, "build_dualtrack_execution_response", execution)
    monkeypatch.setattr(
        dashboard_server,
        "build_strategy_console_production_history",
        lambda **_kwargs: {
            "account": {},
            "pnl": {},
            "trades": [],
            "fills": [],
            "summary": {},
            "accounting_snapshot": {"snapshot_id": "history-accounting"},
            "history_contract": {},
        },
    )
    monkeypatch.setattr(dashboard_server, "build_dualtrack_ledger_response", lambda **_kwargs: {})
    monkeypatch.setattr(dashboard_server, "load_strategy_shadow_runs", lambda *_args: [])
    before = _fingerprint(output)

    first = dashboard_server.build_strategy_console_current_response(
        output_root=output,
        as_of="2026-07-18T01:02:00+00:00",
    )
    second = dashboard_server.build_strategy_console_current_response(
        output_root=output,
        as_of="2026-07-18T01:02:00+00:00",
    )
    stable = dashboard_server.build_trading_system_read_model_response(
        output_root=output,
        as_of="2026-07-18T01:02:00+00:00",
    )

    assert first["production_plan"] is None
    assert second["migration"]["legacy_migration_required"] is True
    assert first["production_execution"]["accounting_snapshot"]["snapshot_id"] == "current-accounting"
    assert (
        first["production_execution"]["production_history_accounting_snapshot"]["snapshot_id"]
        == "history-accounting"
    )
    assert stable["contract"]["schema_version"] == "trading-system-read-model-v1"
    assert stable["contract"]["source_identities"]["accounting_snapshot_id"] == "history-accounting"
    assert stable["contract"]["source_identities"]["current_accounting_snapshot_id"] == "current-accounting"
    assert received_market_ids == [id(market), id(market), id(market), id(market), id(market), id(market)]
    assert not (output / "dualtrack" / "strategy_control" / "plans" / f"{cycle_id}.json").exists()
    assert _fingerprint(output) == before
