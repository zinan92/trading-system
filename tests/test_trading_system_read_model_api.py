from __future__ import annotations

import json
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


def test_dashboard_polling_payload_compacts_duplicate_history_but_keeps_trend_and_review_facts() -> None:
    receipt = {
        "evaluation_id": "eval-1",
        "status": "success",
        "input": {"prompt": "x" * 100_000},
        "archive": {"relative_path": "evaluations/eval-1.json"},
        "effects": {"orders_created": 0},
    }
    proposal = {
        "proposal_id": "proposal-1",
        "source": "ai",
        "direction": "long",
        "style": "steady",
        "range": {"low": 4000.0, "high": 4100.0},
        "grid": {"count": 20},
        "rationale": "trend summary",
        "analysis": {"framework": {"strategy": {"recommended_strategy_type": "dca"}}},
        "evaluation_receipt": receipt,
        "prompt_contract": {"prompt": "x" * 100_000},
    }
    payload = {
        "strategy": {"proposals": [proposal], "proposal_diff": {"raw": "x" * 100_000}},
        "execution": {
            "accounting": {"completeness": {"status": "complete"}, "fills": [{"raw": "x" * 100_000}]},
            "current_accounting": {"reconciliation": {"status": "pass"}, "orders": [{"raw": "x" * 100_000}]},
            "orders": [{"is_open": True, "is_accepted": True, "order_id": "open"}] + [{"order_id": f"closed-{index}"} for index in range(100)],
            "positions": [{"status": "open", "trade_id": "trade-1"}] + [{"status": "closed", "trade_id": f"closed-{index}"} for index in range(100)],
            "trades": [{"trade_id": f"trade-{index}"} for index in range(100)],
            "fills": [{"fill_id": f"fill-{index}"} for index in range(100)],
        },
        "review": {
            "cycle_packages": [{"cycle_id": "cycle-1", "proposals": [proposal]}],
        },
    }

    compact = dashboard_server._compact_dashboard_read_model_payload(payload)
    compact_proposal = compact["strategy"]["proposals"][0]

    assert compact_proposal["rationale"] == "trend summary"
    assert compact_proposal["analysis"]["framework"]["strategy"]["recommended_strategy_type"] == "dca"
    assert compact_proposal["evaluation_receipt"] == {
        "evaluation_id": "eval-1",
        "status": "success",
        "archive": {"relative_path": "evaluations/eval-1.json"},
        "effects": {"orders_created": 0},
    }
    assert "prompt_contract" not in compact_proposal
    assert compact["strategy"]["proposal_diff"]["status"] == "available_on_demand"
    assert "fills" not in compact["execution"]["accounting"]
    assert "orders" not in compact["execution"]["current_accounting"]
    assert len(compact["execution"]["trades"]) == dashboard_server._DASHBOARD_RECENT_ACTIVITY_LIMIT
    assert compact["execution"]["open_orders"][0]["order_id"] == "open"
    assert compact["execution"]["open_positions"][0]["trade_id"] == "trade-1"
    assert compact["review"]["cycle_packages"][0]["proposals"][0]["evaluation_receipt"] == compact_proposal["evaluation_receipt"]
    assert len(json.dumps(compact)) < len(json.dumps(payload)) / 10


def test_ai_evaluation_receipt_is_loaded_on_demand_from_current_control_cycle(monkeypatch, tmp_path: Path) -> None:
    proposal = {
        "proposal_id": "proposal-1",
        "cycle_id": "2026-07-24_DAY",
        "direction": "long",
        "evaluation_receipt": {"evaluation_id": "eval-1", "input": {"prompt": "full"}},
        "prompt_contract": {"prompt": "full"},
    }
    monkeypatch.setattr(
        dashboard_server,
        "build_dualtrack_cycle_current_response",
        lambda **_kwargs: {"cycle_id": "2026-07-24_DAY"},
    )

    class FakeControl:
        def __init__(self, _output):
            pass

        def read_model(self, _cycle_id, *, as_of=None):
            return {"proposals": [proposal]}

    monkeypatch.setattr(dashboard_server, "StrategyControlPlane", FakeControl)

    response = dashboard_server.build_ai_evaluation_receipt_response("eval-1", output_root=tmp_path)

    assert response["schema_version"] == "dashboard-ai-evaluation-receipt-v1"
    assert response["proposal"]["evaluation_receipt"]["input"]["prompt"] == "full"
    assert response["proposal"]["prompt_contract"]["prompt"] == "full"


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
        "accepted_order_count": 25,
        "unknown_order_count": 0,
        "open_position_count": 1,
        "trade_count": 2,
        "open_trade_count": 0,
        "completed_trade_count": 2,
        "completed_round_trip_count": 2,
        "chronology_invalid_trade_count": 0,
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


def test_dashboard_market_read_config_uses_one_short_datafeed_attempt(monkeypatch) -> None:
    monkeypatch.setattr(
        dashboard_server,
        "load_pipeline_config",
        lambda: {"datafeed": {"enabled": True, "timeout_seconds": 10}},
    )

    config = dashboard_server._dashboard_market_read_config()

    assert config["datafeed"]["timeout_seconds"] == 2.0
    assert config["datafeed"]["live_request_attempts"] == 1


def test_market_bars_handler_uses_bounded_dashboard_read_config(monkeypatch) -> None:
    handler = object.__new__(dashboard_server.DashboardHandler)
    writes: list[tuple[int, dict]] = []
    captured: dict = {}
    handler._write_json = lambda status, payload: writes.append((status, payload))
    monkeypatch.setattr(
        dashboard_server,
        "_dashboard_market_read_config",
        lambda: {"datafeed": {"timeout_seconds": 2.0, "live_request_attempts": 1}},
    )

    def market_bars(**kwargs):
        captured["kwargs"] = kwargs
        return {"status": "blocked"}

    monkeypatch.setattr(dashboard_server, "build_dualtrack_market_bars_response", market_bars)

    handler._handle_dualtrack_market_bars_get("symbol=GOLD&timeframe=30m&limit=240")

    assert captured["kwargs"]["config"] == {
        "datafeed": {"timeout_seconds": 2.0, "live_request_attempts": 1},
    }
    assert writes == [(200, {"status": "blocked"})]


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


def test_dashboard_market_identity_survives_missing_production_plan(monkeypatch) -> None:
    monkeypatch.setattr(
        dashboard_server,
        "dualtrack_config",
        lambda: {
            "market_data": {
                "symbol": "GOLD",
                "timeframe": "1m",
            }
        },
    )

    assert dashboard_server._dashboard_market_identity() == ("GOLD", "1m")


def test_review_cycle_selection_never_mixes_open_or_unreviewed_cycles() -> None:
    packages = [
        {"cycle_id": "2026-07-20_NIGHT", "status": "blocked"},
        {"cycle_id": "2026-07-20_DAY", "status": "closed"},
        {"cycle_id": "2026-07-19_NIGHT", "status": "closed"},
    ]
    ledger = {
        "recent_reviews": [
            {"cycle_id": "2026-07-20_NIGHT"},
            {"cycle_id": "2026-07-19_NIGHT"},
        ]
    }

    assert dashboard_server._latest_review_cycle_id(ledger, packages) == "2026-07-19_NIGHT"
    assert dashboard_server._latest_review_cycle_id({"recent_reviews": []}, packages) == "2026-07-20_DAY"


def test_review_polling_projection_omits_replay_events_and_full_execution_rows() -> None:
    shadow = {
        "status": "pass",
        "cycle_id": "2026-07-19_NIGHT",
        "variant_id": "production",
        "scenario_id": "scenario-production",
        "plan": {"strategy_plan_id": "plan-1", "version": 4},
        "metrics": {"net_pnl": 3.0},
        "scenario": {
            "plan_identity": {"strategy_plan_id": "plan-1", "strategy_plan_version": 4},
            "evaluation_window": {"started_at": "start", "ended_at": "end", "event_count": 1},
            "contracts": {"execution_contract_hash": "exec", "fee_contract_hash": "fee"},
            "hashes": {"market_event_hash": "events"},
            "market_events": [{"large": "raw-event"}],
            "commands": [{"large": "raw-command"}],
        },
        "orders": [{"order_id": "raw-order"}],
        "fills": [{"fill_id": "raw-fill"}],
    }
    packages = [{
        "cycle_id": "2026-07-19_NIGHT",
        "status": "closed",
        "package_hash": "package-hash",
        "strategy_plan": {"strategy_plan_id": "plan-1", "version": 4},
        "proposals": [],
        "execution": {
            "engine": "nautilus_paper",
            "orders": [{"order_id": "raw-order"}],
            "fills": [{"fill_id": "raw-fill"}],
            "positions": [],
            "pnl": {"realized": 3.0},
            "reconciliation": {"status": "ok", "issues": []},
        },
        "strategy_shadows": [shadow],
    }]

    projected = dashboard_server._compact_review_packages(packages, "2026-07-19_NIGHT")[0]
    compact_shadow = projected["strategy_shadows"][0]

    assert projected["execution"]["order_count"] == 1
    assert projected["execution"]["fill_count"] == 1
    assert "orders" not in projected["execution"]
    assert "fills" not in projected["execution"]
    assert "market_events" not in compact_shadow["scenario"]
    assert "commands" not in compact_shadow["scenario"]
    assert "orders" not in compact_shadow
    assert "fills" not in compact_shadow
    assert compact_shadow["scenario"]["hashes"]["market_event_hash"] == "events"


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
