from __future__ import annotations

from pathlib import Path
from copy import deepcopy

import pipelines.dashboard_server as dashboard_server
import services.strategy_control_plane as strategy_control_plane_module
from schemas.accounting import build_accounting_snapshot
from services.dualtrack_config import dualtrack_config as load_dualtrack_test_config
from services.journal_store import write_json
from services.order_lifecycle import OrderLifecycleStore
from services.strategy_control_plane import StrategyControlPlane
from services.trading_system_read_model import project_market_read_model
from tests.test_strategy_control_plane import account_context, market, proposal, safe_grid
from tests.test_trading_system_read_model import _accounting, _risk, _source


def _two_cycle_history_accounting() -> dict:
    trades = [
        {
            "trade_id": f"history-trade-{index}",
            "status": "closed",
            "side": "long",
            "entry_price": 4000.0 + index,
            "remaining_units": 0.0,
            "strategy_plan_id": f"plan-{index}",
            "strategy_plan_version": index,
        }
        for index in (6, 7)
    ]
    fills = [
        {
            "fill_id": f"history-fill-{index}-{event}",
            "trade_id": trade["trade_id"],
            "event": event,
            "price": trade["entry_price"] + (3.0 if event == "target" else 0.0),
        }
        for index, trade in enumerate(trades, start=1)
        for event in ("entry", "target")
    ]
    return build_accounting_snapshot(
        source_type="production_history",
        source_name="versioned_strategy_plan_fills_only",
        source_schema_version="dualtrack-execution-v1",
        scope={"strategy_plan_scope": "all_versioned_production_plans"},
        currency="USD",
        orders=[],
        fills=fills,
        positions=trades,
        trades=trades,
        counts={
            "order_count": 0,
            "open_order_count": 0,
            "fill_count": 4,
            "entry_fill_count": 2,
            "exit_fill_count": 2,
            "position_count": 2,
            "open_position_count": 0,
            "trade_count": 2,
            "open_trade_count": 0,
            "completed_trade_count": 2,
        },
        pnl={
            "gross_realized_pnl": 6.0,
            "commission": 0.4,
            "funding": 0.0,
            "net_realized_pnl": 5.6,
            "unrealized_pnl": 0.0,
            "total_pnl": 5.6,
        },
        account={
            "starting_balance": 10_000.0,
            "ending_cash": 10_005.6,
            "equity": 10_005.6,
        },
        completeness={"status": "complete", "limitations": []},
        reconciliation={"status": "pass", "issues": []},
    ).to_dict()


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

    def fail_preflight(*_args, **_kwargs):
        raise AssertionError("stable GET must not preflight a broker")

    monkeypatch.setattr(dashboard_server, "_assemble_strategy_console_snapshot", assemble)
    monkeypatch.setattr(dashboard_server.PaperBrokerAdapter, "preflight", fail_preflight)
    write_json(output / "dualtrack" / "risk_decisions" / "current.json", [_risk()])

    legacy = dashboard_server.build_strategy_console_current_response(
        output_root=output,
        as_of="2026-07-18T01:02:04+00:00",
    )
    stable = dashboard_server.build_trading_system_read_model_response(
        output_root=output,
        as_of="2026-07-18T01:02:04+00:00",
    )
    repeated = dashboard_server.build_trading_system_read_model_response(
        output_root=output,
        as_of="2026-07-18T01:02:04+00:00",
    )

    assert legacy is source
    assert stable["contract"]["schema_version"] == "trading-system-read-model-v1"
    assert stable["strategy"]["summary"]["plan_id"] == "plan-7"
    assert stable["execution"]["counts"]["open_order_count"] == 25
    assert stable["risk"]["status"] == "current"
    assert stable["contract"]["snapshot_id"] == repeated["contract"]["snapshot_id"]
    assert calls == [
        (output, "2026-07-18T01:02:04+00:00"),
        (output, "2026-07-18T01:02:04+00:00"),
        (output, "2026-07-18T01:02:04+00:00"),
    ]


def test_new_endpoint_uses_history_for_lifecycle_and_pnl_but_current_cycle_for_positions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "outputs"
    source = _source(open_trade=True)
    current_accounting = _accounting(open_trade=True)
    history_accounting = _two_cycle_history_accounting()
    source["production_execution"]["accounting_snapshot"] = current_accounting
    source["production_execution"]["production_history_accounting_snapshot"] = history_accounting
    monkeypatch.setattr(
        dashboard_server,
        "_assemble_strategy_console_snapshot",
        lambda **_kwargs: source,
    )
    write_json(output / "dualtrack" / "risk_decisions" / "current.json", [_risk()])

    response = dashboard_server.build_trading_system_read_model_response(
        output_root=output,
        as_of="2026-07-18T01:02:04+00:00",
    )

    assert response["execution"]["counts"] == {
        "order_count": 25,
        "open_order_count": 25,
        "unknown_order_count": 0,
        "open_position_count": 1,
        "trade_count": 2,
        "open_trade_count": 0,
        "completed_trade_count": 2,
        "completed_round_trip_count": 2,
        "fill_count": 4,
        "entry_fill_count": 2,
        "exit_fill_count": 2,
    }
    assert len(response["execution"]["positions"]) == 1
    assert len(response["execution"]["trades"]) == 2
    assert response["execution"]["pnl"]["total"] == 5.6
    assert response["execution"]["pnl"]["return_pct"] == 0.056
    assert response["execution"]["scopes"]["orders_and_positions"]["kind"] == "current_execution_cycle"
    assert response["execution"]["scopes"]["trades_fills_and_pnl"]["kind"] == "all_versioned_production_plans"
    assert response["contract"]["source_identities"]["accounting_snapshot_id"] == history_accounting["snapshot_id"]
    assert (
        response["contract"]["source_identities"]["current_accounting_snapshot_id"]
        == current_accounting["snapshot_id"]
    )


def test_order_lifecycle_store_advances_through_stable_api_without_row_loss(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "outputs"
    run_date = "2026-07-18"
    order_id = "order-lifecycle-api-1"
    store = OrderLifecycleStore(output)
    store.write_intent(
        run_date,
        order_id=order_id,
        ticket_id="ticket-1",
        idempotency_key="order-lifecycle-api-1",
        requested_quantity=1.5,
        requested_price=3999.0,
        source="read_model_acceptance",
    )
    store.transition(run_date, order_id, "submitting", reason="safe_test_submit")
    store.transition(run_date, order_id, "accepted", reason="safe_test_accept")

    def assemble(**_kwargs):
        source = _source()
        lifecycle = store.current(run_date, order_id)
        source["production_execution"]["orders"] = [{
            **lifecycle,
            "quantity": lifecycle["requested_quantity"],
            "price": lifecycle["requested_price"],
            "side": "buy",
            "order_type": "limit",
        }]
        return source

    monkeypatch.setattr(dashboard_server, "_assemble_strategy_console_snapshot", assemble)
    write_json(output / "dualtrack" / "risk_decisions" / "current.json", [_risk()])

    observed = []
    for state in ("accepted", "partially_filled", "cancelled"):
        if state == "partially_filled":
            store.transition(
                run_date,
                order_id,
                state,
                reason="safe_test_partial_fill",
                filled_quantity=0.5,
            )
        elif state == "cancelled":
            store.transition(run_date, order_id, state, reason="safe_test_cancel")
        response = dashboard_server.build_trading_system_read_model_response(
            output_root=output,
            as_of="2026-07-18T01:02:04+00:00",
        )
        order = response["execution"]["orders"][0]
        observed.append({
            "order_id": order["order_id"],
            "state": order["state"],
            "label": order["state_label"],
            "rank": order["state_rank"],
            "revision": order["state_revision"],
            "order_count": response["execution"]["counts"]["order_count"],
            "open_count": response["execution"]["counts"]["open_order_count"],
        })

    assert [row["order_id"] for row in observed] == [order_id, order_id, order_id]
    assert [row["state"] for row in observed] == [
        "accepted",
        "partially_filled",
        "cancelled",
    ]
    assert [row["label"] for row in observed] == ["已接受", "部分成交", "已撤单"]
    assert [row["rank"] for row in observed] == sorted(row["rank"] for row in observed)
    assert [row["revision"] for row in observed] == [3, 4, 5]
    assert [row["order_count"] for row in observed] == [1, 1, 1]
    assert [row["open_count"] for row in observed] == [1, 1, 0]


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
