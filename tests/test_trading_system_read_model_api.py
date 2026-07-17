from __future__ import annotations

from pathlib import Path
from copy import deepcopy

import pipelines.dashboard_server as dashboard_server
import services.strategy_control_plane as strategy_control_plane_module
from services.dualtrack_config import dualtrack_config as load_dualtrack_test_config
from services.journal_store import write_json
from services.strategy_control_plane import StrategyControlPlane
from services.trading_system_read_model import project_market_read_model
from tests.test_strategy_control_plane import account_context, market, proposal, safe_grid
from tests.test_trading_system_read_model import _risk, _source


def test_stable_and_legacy_gets_delegate_to_the_same_named_assembler(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "outputs"
    source = _source()
    calls: list[tuple[Path | None, str | None]] = []

    def assemble(*, output_root=None, as_of=None):
        calls.append((output_root, as_of))
        return source

    monkeypatch.setattr(dashboard_server, "_assemble_strategy_console_snapshot", assemble)
    write_json(output / "dualtrack" / "risk_decisions" / "current.json", [_risk()])

    legacy = dashboard_server.build_strategy_console_current_response(
        output_root=output,
        as_of="2026-07-18T01:02:04+00:00",
    )
    stable = dashboard_server.build_trading_system_read_model_response(
        output_root=output,
        as_of="2026-07-18T01:02:04+00:00",
    )

    assert legacy is source
    assert stable["contract"]["schema_version"] == "trading-system-read-model-v1"
    assert stable["strategy"]["summary"]["plan_id"] == "plan-7"
    assert stable["execution"]["counts"]["open_order_count"] == 25
    assert stable["risk"]["status"] == "current"
    assert calls == [
        (output, "2026-07-18T01:02:04+00:00"),
        (output, "2026-07-18T01:02:04+00:00"),
    ]


def test_new_route_and_handler_use_no_store_json_boundary(monkeypatch) -> None:
    source = Path(dashboard_server.__file__).read_text(encoding="utf-8")
    handler = object.__new__(dashboard_server.DashboardHandler)
    writes: list[tuple[int, dict]] = []
    handler._write_json = lambda status, payload: writes.append((status, payload))
    handler._write_error = lambda *_args: None
    monkeypatch.setattr(
        dashboard_server,
        "build_trading_system_read_model_response",
        lambda **_kwargs: {"contract": {"schema_version": "trading-system-read-model-v1"}},
    )

    handler._handle_trading_system_read_model("as_of=2026-07-18T01%3A02%3A04%2B00%3A00")

    assert 'if parsed.path == "/api/trading-system/read-model":' in source
    assert writes == [(200, {"contract": {"schema_version": "trading-system-read-model-v1"}})]
    assert 'self.send_header("Cache-Control", "no-store")' in source


def test_market_bars_response_projects_trust_and_display_label(monkeypatch) -> None:
    monkeypatch.setattr(
        dashboard_server.DualTrackMarketFeed,
        "snapshot",
        lambda *_args, **_kwargs: {
            "status": "ready",
            "fresh": True,
            "is_synthetic": False,
            "provider": "venue-a",
            "latest_close": 4004.0,
            "bars": [],
        },
    )

    response = dashboard_server.build_dualtrack_market_bars_response()

    assert response["trusted"] is True
    assert response["provider_label"] == "Venue A"


def test_safe_start_post_is_observable_through_new_get(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    as_of = "2026-07-05T01:40:00+00:00"
    config = load_dualtrack_test_config()
    config["execution_engine"] = {
        "authoritative": "legacy_paper",
        "shadow": "none",
        "real_money_eligible": False,
    }
    def config_factory(*_args, **_kwargs):
        return deepcopy(config)

    market_snapshot = market()
    projected_market = project_market_read_model(market_snapshot)
    monkeypatch.setattr(strategy_control_plane_module, "dualtrack_config", config_factory)
    monkeypatch.setattr("services.dualtrack_config.dualtrack_config", config_factory)
    monkeypatch.setattr(dashboard_server, "dualtrack_config", config_factory)
    monkeypatch.setattr(
        dashboard_server,
        "build_dualtrack_cycle_current_response",
        lambda **_kwargs: {"cycle_id": cycle_id},
    )
    monkeypatch.setattr(
        dashboard_server,
        "build_dualtrack_market_bars_response",
        lambda **_kwargs: projected_market,
    )
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "human", "neutral"), now=as_of)
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"], now=as_of)

    posted = dashboard_server.build_strategy_console_control_response(
        {"cycle_id": cycle_id, "action": "start", "as_of": as_of, **safe_grid()},
        output_root=output,
        market=market_snapshot,
        account=account_context(),
        actor={"email": "acceptance@example.test", "transport": "local"},
    )
    observed = dashboard_server.build_trading_system_read_model_response(
        output_root=output,
        as_of=as_of,
    )

    assert posted["runtime"]["actual_state"] == "running"
    assert observed["runtime"]["status"] == "running"
    assert observed["strategy"]["summary"]["plan_id"] == posted["plan"]["strategy_plan_id"]
    assert observed["execution"]["counts"]["open_order_count"] == posted["accepted_orders"]
    assert observed["execution"]["counts"]["open_order_count"] > 0
    assert observed["risk"]["status"] == "current"
