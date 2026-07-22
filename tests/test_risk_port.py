from copy import deepcopy
from pathlib import Path

import pytest

from schemas.accounting import build_accounting_snapshot
from services.journal_store import load_json
from services.risk_decision_store import FileRiskDecisionStore as RiskDecisionStore
from services.risk_live_money_adapter import LiveMoneyRiskDecisionAdapter
from services.risk_policy_paper import (
    PaperGridRiskDecisionPort,
    grid_risk_evaluator,
    grid_risk_policy,
)
from services.risk_port import (
    action_class_for_command,
    assert_matching_risk_decision,
    build_grid_risk_request,
    build_manual_order_risk_request,
    canonical_account_risk_state,
    canonical_execution_risk_state,
    canonical_live_risk_allows_exposure,
    normalize_manual_order_command,
    require_exposure_permission,
)


CYCLE_ID = "2026-07-18_DAY"
CHECKED_AT = "2026-07-18T01:00:00+00:00"


def config(*, max_leverage: float = 10.0) -> dict:
    return {
        "capital_per_track_usd": 10_000.0,
        "max_leverage": max_leverage,
        "cost_per_side_bp": 0.5,
        "strategy_grid": {
            "required_leverage": 10.0,
            "min_net_profit_per_grid_usd": 10.0,
            "min_grid_count": 30,
            "max_grid_count": 70,
            "capital_utilization_cap": 1.0,
        },
    }


def accounting_context(*, equity: float = 10_000.0, reconciliation: str = "pass") -> dict:
    snapshot = build_accounting_snapshot(
        source_type="production_history",
        source_name="production_history",
        source_schema_version="dualtrack-execution-v1",
        scope={"cycle_id": CYCLE_ID},
        currency="USDT",
        orders=[],
        fills=[],
        positions=[],
        trades=[],
        counts={
            "order_count": 0,
            "open_order_count": 0,
            "fill_count": 0,
            "entry_fill_count": 0,
            "exit_fill_count": 0,
            "trade_count": 0,
            "open_trade_count": 0,
            "completed_trade_count": 0,
            "position_count": 0,
            "open_position_count": 0,
        },
        pnl={
            "gross_realized_pnl": 0.0,
            "fees": 0.0,
            "funding": 0.0,
            "net_realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
            "slippage": 0.0,
        },
        account={
            "starting_balance": equity,
            "ending_cash": equity,
            "equity": equity,
            "available_balance": equity,
            "margin": 0.0,
            "exposure": 0.0,
            "leverage": 0.0,
        },
        completeness={"status": "complete", "observed": {}, "limitations": [], "unknown_is_not_zero": True},
        reconciliation={"status": reconciliation, "issues": []},
    ).to_dict()
    return {"equity": equity, "ending_cash": equity, "accounting_snapshot": snapshot}


def market(*, price: float = 100.0, fresh: bool = True) -> dict:
    return {
        "status": "ready" if fresh else "stale",
        "fresh": fresh,
        "is_synthetic": False,
        "provider": "binance_usdm_futures",
        "source_mode": "binance_usdm_futures",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": price,
        "latest_timestamp": "2026-07-18T00:59:00+00:00",
    }


def execution_snapshot(
    *,
    engine: str = "legacy_paper",
    orders: list[dict] | None = None,
    positions: list[dict] | None = None,
) -> dict:
    position_rows = positions or []
    fills = []
    realized = 0.0
    unrealized = 0.0
    for index, position in enumerate(position_rows, start=1):
        quantity = float(position["remaining_units"])
        cost = 0.0
        fills.append({
            "fill_id": f"fill-{index}",
            "order_id": f"filled-order-{index}",
            "trade_id": position["trade_id"],
            "event": "entry",
            "side": "buy" if position["side"] == "long" else "sell",
            "price": position["entry_price"],
            "quantity": quantity,
            "cost": cost,
            "gross_pnl": 0.0,
            "realized_pnl": 0.0,
            "slippage": 0.0,
            "ts": "2026-07-18T00:30:00+00:00",
            "strategy_plan_id": position.get("strategy_plan_id", "old-plan"),
            "strategy_plan_version": position.get("strategy_plan_version", 1),
        })
        unrealized += float(position.get("unrealized_pnl") or 0.0)
    equity = 10_000.0 + realized + unrealized
    return {
        "schema_version": "dualtrack-execution-v1",
        "engine": engine,
        "cycle_id": CYCLE_ID,
        "orders": list(orders or []),
        "fills": fills,
        "positions": position_rows,
        "account": {
            "starting_cash": 10_000.0,
            "realized_pnl": realized,
            "ending_cash": 10_000.0 + realized,
            "equity": equity,
            "margin": 0.0,
            "exposure": sum(float(row["entry_price"]) * float(row["remaining_units"]) for row in position_rows),
            "slippage": 0.0,
            "fees": 0.0,
            "funding": 0.0,
        },
        "pnl": {"realized": realized, "unrealized": unrealized},
        "mark": {"price": 100.0, "fresh": True, "source": "test"},
        "capabilities": {},
    }


def reconciliation(*, status: str = "ok") -> dict:
    return {"status": status, "issues": [] if status == "ok" else [{"code": "drift"}]}


def plan(*, notional: float = 1_000.0, leverage: float = 10.0, low: float = 90.0, high: float = 110.0) -> dict:
    return {
        "schema_version": "strategy-plan-v1",
        "strategy_plan_id": "plan-risk-1",
        "cycle_id": CYCLE_ID,
        "version": 2,
        "preview_id": "preview-risk-1",
        "direction": "neutral",
        "range": {"low": low, "high": high},
        "grid": {"mode": "arithmetic", "count": 30, "notional_per_grid": notional, "leverage": leverage},
        # These are deliberately not trusted by the risk adapter.
        "risk_budget": {"max_loss": 0.01, "estimated_margin": 0.01},
    }


def commands(*, notional: float = 1_000.0, low: float = 90.0, high: float = 110.0) -> list[dict]:
    rows = []
    for identity, side, price, stop, take_profit in (
        ("buy", "buy", 95.0, low, 100.0),
        ("sell", "sell", 105.0, high, 100.0),
    ):
        rows.append({
            "source_fill_id": f"grid:{identity}",
            "cycle_id": CYCLE_ID,
            "strategy_plan_id": "plan-risk-1",
            "strategy_plan_version": 2,
            "side": side,
            "event": "entry",
            "order_type": "limit",
            "price": price,
            "quantity": round(notional / price, 8),
            "notional": notional,
            "sl": stop,
            "tp": take_profit,
            "ts": CHECKED_AT,
        })
    return rows


def request(
    *,
    plan_value: dict | None = None,
    command_rows: list[dict] | None = None,
    account: dict | None = None,
    market_value: dict | None = None,
    snapshot: dict | None = None,
    recon: dict | None = None,
    action_class: str = "increase_exposure",
    intent: str = "start_grid",
    replaced_order_ids: list[str] | None = None,
    retained_order_ids: list[str] | None = None,
    config_value: dict | None = None,
):
    resolved_config = config_value or config()
    return build_grid_risk_request(
        checked_at=CHECKED_AT,
        action_class=action_class,
        intent=intent,
        plan=plan_value or plan(),
        commands=command_rows if command_rows is not None else commands(),
        account_context=account if account is not None else accounting_context(),
        market=market_value or market(),
        execution_snapshot=snapshot or execution_snapshot(),
        execution_reconciliation=recon or reconciliation(),
        policy=grid_risk_policy(resolved_config),
        evaluator=grid_risk_evaluator(),
        replaced_order_ids=replaced_order_ids,
        retained_order_ids=retained_order_ids,
    )


def manual_command(*, notional: float = 500.0, source: str = "browser-supplied") -> dict:
    return {
        "cycle_id": CYCLE_ID,
        "ts": CHECKED_AT,
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "notional": notional,
        "sl": 95.0,
        "tp": 105.0,
        "source": source,
    }


def manual_request(
    command: dict,
    *,
    account: dict | None = None,
    market_value: dict | None = None,
    snapshot: dict | None = None,
):
    exact = normalize_manual_order_command(command, config=config())
    return build_manual_order_risk_request(
        checked_at=CHECKED_AT,
        command=exact,
        account_context=account if account is not None else accounting_context(),
        market=market_value or market(),
        execution_snapshot=snapshot or execution_snapshot(),
        execution_reconciliation=reconciliation(),
        config=config(),
        policy=grid_risk_policy(config()),
        evaluator=grid_risk_evaluator(),
    )


def test_safe_grid_is_allowed_from_exact_commands_and_canonical_account() -> None:
    decision = PaperGridRiskDecisionPort().evaluate(request())
    payload = decision.to_dict()

    assert decision.allow_exposure_increase is True
    assert payload["metrics"]["calculation_source"] == "exact_commands_plus_canonical_accounting"
    assert payload["metrics"]["preview_risk_fields_used"] is False
    assert payload["metrics"]["projected_max_loss"] == pytest.approx(52.631579)
    assert payload["recommendation"]["applied_automatically"] is False


def test_unknown_or_tampered_account_blocks_even_if_preview_top_level_equity_exists() -> None:
    port = PaperGridRiskDecisionPort()
    missing = port.evaluate(request(account={"equity": 10_000.0, "ending_cash": 10_000.0}))
    tampered_context = accounting_context()
    tampered_context["accounting_snapshot"]["account"]["equity"] = 1_000_000.0
    tampered = port.evaluate(request(account=tampered_context))

    assert missing.to_dict()["primary_blocker"]["code"] == "account_snapshot_unavailable"
    assert tampered.to_dict()["primary_blocker"]["code"] == "account_snapshot_unavailable"
    assert canonical_account_risk_state({"equity": 10_000.0})["status"] == "unknown"


def test_plan_loss_is_recomputed_as_advisory_without_mutating_requested_notional() -> None:
    candidate_plan = plan(notional=10_000.0, leverage=10.0, low=50.0, high=110.0)
    candidate_commands = commands(notional=10_000.0, low=50.0, high=110.0)
    before_plan = deepcopy(candidate_plan)
    before_commands = deepcopy(candidate_commands)

    decision = PaperGridRiskDecisionPort().evaluate(request(plan_value=candidate_plan, command_rows=candidate_commands))
    payload = decision.to_dict()

    assert decision.allow_exposure_increase is True
    assert not any(row["code"] == "plan_loss_budget_exceeded" for row in payload["blockers"])
    assert any(row["code"] == "plan_max_loss_advisory" for row in payload["warnings"])
    assert payload["metrics"]["projected_max_loss"] > 1_000.0
    assert payload["recommendation"]["applied_automatically"] is False
    assert candidate_plan == before_plan
    assert candidate_commands == before_commands


def test_market_range_and_execution_reconciliation_fail_closed() -> None:
    port = PaperGridRiskDecisionPort()
    outside = port.evaluate(request(market_value=market(price=120.0)))
    drifted = port.evaluate(request(recon=reconciliation(status="drift")))

    assert any(row["code"] == "market_price_outside_range" for row in outside.to_dict()["blockers"])
    assert any(row["code"] == "execution_reconciliation_drift" for row in drifted.to_dict()["blockers"])


@pytest.mark.parametrize(
    ("direction", "price", "blocked"),
    [
        ("long", 120.0, False),
        ("long", 80.0, True),
        ("short", 80.0, False),
        ("short", 120.0, True),
        ("neutral", 120.0, True),
    ],
)
def test_market_range_gate_is_direction_and_marketability_aware(
    direction: str,
    price: float,
    blocked: bool,
) -> None:
    candidate = plan()
    candidate["direction"] = direction
    command_rows = commands()
    if direction == "long":
        command_rows = [row for row in command_rows if row["side"] == "buy"]
    elif direction == "short":
        command_rows = [row for row in command_rows if row["side"] == "sell"]

    decision = PaperGridRiskDecisionPort().evaluate(
        request(
            plan_value=candidate,
            command_rows=command_rows,
            market_value=market(price=price),
        )
    )
    blocker_codes = {row["code"] for row in decision.to_dict()["blockers"]}

    assert ("market_price_outside_range" in blocker_codes) is blocked


@pytest.mark.parametrize("grid_count", [29, 71])
def test_grid_count_outside_operating_band_fails_closed(grid_count: int) -> None:
    candidate = plan()
    candidate["grid"]["count"] = grid_count

    decision = PaperGridRiskDecisionPort().evaluate(request(plan_value=candidate))

    assert any(
        row["code"] == "candidate_grid_count_out_of_bounds"
        for row in decision.to_dict()["blockers"]
    )


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("grid_count", [15, 20, 35])
def test_single_side_grid_count_uses_executable_half_band(
    direction: str,
    grid_count: int,
) -> None:
    candidate = plan()
    candidate["direction"] = direction
    candidate["grid"]["count"] = grid_count

    decision = PaperGridRiskDecisionPort().evaluate(request(plan_value=candidate))

    assert not any(
        row["code"] == "candidate_grid_count_out_of_bounds"
        for row in decision.to_dict()["blockers"]
    )


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("grid_count", [14, 36])
def test_single_side_grid_count_outside_half_band_still_fails_closed(
    direction: str,
    grid_count: int,
) -> None:
    candidate = plan()
    candidate["direction"] = direction
    candidate["grid"]["count"] = grid_count

    decision = PaperGridRiskDecisionPort().evaluate(request(plan_value=candidate))

    blocker_row = next(
        row
        for row in decision.to_dict()["blockers"]
        if row["code"] == "candidate_grid_count_out_of_bounds"
    )
    assert blocker_row["evidence"] == {
        "count": grid_count,
        "direction": direction,
        "minimum": 15,
        "maximum": 35,
    }


def test_missing_policy_limit_blocks_instead_of_disabling_the_rule() -> None:
    invalid_config = config()
    invalid_config["strategy_grid"]["min_net_profit_per_grid_usd"] = -1

    decision = PaperGridRiskDecisionPort().evaluate(request(config_value=invalid_config))

    assert any(row["code"] == "risk_policy_invalid" for row in decision.to_dict()["blockers"])


def test_start_cannot_layer_existing_orders_or_positions_but_regrid_can_replace_exact_set() -> None:
    open_order = {
        "order_id": "old-order-1",
        "state": "accepted",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 94.0,
        "quantity": 1.0,
        "strategy_plan_id": "old-plan",
        "strategy_plan_version": 1,
    }
    position = {
        "trade_id": "old-trade-1",
        "position_id": "old-position-1",
        "status": "open",
        "side": "long",
        "remaining_units": 1.0,
        "entry_price": 96.0,
        "sl": 90.0,
        "tp": 100.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "strategy_plan_id": "old-plan",
        "strategy_plan_version": 1,
    }
    state = execution_snapshot(orders=[open_order], positions=[position])
    port = PaperGridRiskDecisionPort()

    start = port.evaluate(request(snapshot=state))
    regrid = port.evaluate(request(
        snapshot=state,
        action_class="replace_pending",
        intent="replace_grid",
        replaced_order_ids=["old-order-1"],
    ))

    assert any(row["code"] == "existing_entry_orders_present" for row in start.to_dict()["blockers"])
    assert any(row["code"] == "existing_positions_require_regrid" for row in start.to_dict()["blockers"])
    assert regrid.allow_exposure_increase is True
    assert regrid.to_dict()["metrics"]["open_position_count"] == 1
    assert regrid.to_dict()["metrics"]["projected_max_loss"] > start.to_dict()["metrics"]["candidate_loss_by_side"]["buy"]


def test_regrid_blocks_unmanaged_order_and_unknown_existing_protection() -> None:
    order = {
        "order_id": "old-order-1",
        "state": "accepted",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 94.0,
        "quantity": 1.0,
    }
    position = {
        "trade_id": "old-trade-1",
        "position_id": "old-position-1",
        "status": "open",
        "side": "long",
        "remaining_units": 1.0,
        "entry_price": 96.0,
        "sl": None,
        "tp": 100.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "strategy_plan_id": "old-plan",
        "strategy_plan_version": 1,
    }
    decision = PaperGridRiskDecisionPort().evaluate(request(
        snapshot=execution_snapshot(orders=[order], positions=[position]),
        action_class="replace_pending",
        intent="replace_grid",
        replaced_order_ids=[],
    ))
    codes = {row["code"] for row in decision.to_dict()["blockers"]}

    assert "replacement_order_set_mismatch" in codes
    assert "open_position_protection_unknown" in codes


def test_partial_regrid_binds_retained_orders_and_replaces_only_declared_edge() -> None:
    open_orders = [
        {
            "order_id": "keep-order",
            "state": "accepted",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": 95.0,
            "quantity": round(1_000.0 / 95.0, 8),
        },
        {
            "order_id": "remove-order",
            "state": "accepted",
            "side": "sell",
            "event": "entry",
            "order_type": "limit",
            "price": 105.0,
            "quantity": round(1_000.0 / 105.0, 8),
        },
    ]
    candidate_commands = commands()
    candidate_commands[0]["existing_order_id"] = "keep-order"
    state = execution_snapshot(orders=open_orders)

    allowed = PaperGridRiskDecisionPort().evaluate(
        request(
            snapshot=state,
            action_class="replace_pending",
            intent="adjust_grid_edges",
            command_rows=candidate_commands,
            retained_order_ids=["keep-order"],
            replaced_order_ids=["remove-order"],
        )
    )
    unbound = PaperGridRiskDecisionPort().evaluate(
        request(
            snapshot=state,
            action_class="replace_pending",
            intent="adjust_grid_edges",
            command_rows=commands(),
            retained_order_ids=["keep-order"],
            replaced_order_ids=["remove-order"],
        )
    )
    tampered_commands = commands()
    tampered_commands[0]["existing_order_id"] = "keep-order"
    tampered_commands[0]["quantity"] = tampered_commands[0]["quantity"] / 10.0
    tampered_commands[0]["notional"] = tampered_commands[0]["notional"] / 10.0
    tampered = PaperGridRiskDecisionPort().evaluate(
        request(
            snapshot=state,
            action_class="replace_pending",
            intent="adjust_grid_edges",
            command_rows=tampered_commands,
            retained_order_ids=["keep-order"],
            replaced_order_ids=["remove-order"],
        )
    )

    assert allowed.allow_exposure_increase is True
    assert allowed.to_dict()["metrics"]["retained_entry_order_count"] == 1
    assert allowed.to_dict()["metrics"]["replaced_entry_order_count"] == 1
    assert any(
        row["code"] == "replacement_order_set_mismatch"
        for row in unbound.to_dict()["blockers"]
    )
    assert any(
        row["code"] == "replacement_order_set_mismatch"
        and row["evidence"]["retained_economics_mismatch_order_ids"] == ["keep-order"]
        for row in tampered.to_dict()["blockers"]
    )


def test_partial_regrid_allows_pure_contraction_to_zero_pending_entries() -> None:
    existing = {
        "order_id": "outside-order",
        "state": "accepted",
        "side": "sell",
        "event": "entry",
        "order_type": "limit",
        "price": 105.0,
        "quantity": 1.0,
    }
    decision = PaperGridRiskDecisionPort().evaluate(
        request(
            snapshot=execution_snapshot(orders=[existing]),
            action_class="replace_pending",
            intent="adjust_grid_edges",
            command_rows=[],
            replaced_order_ids=["outside-order"],
            retained_order_ids=[],
        )
    )

    assert decision.allow_exposure_increase is True
    assert decision.to_dict()["metrics"]["candidate_notional_by_side"] == {
        "buy": 0.0,
        "sell": 0.0,
    }


def test_decision_recheck_rebuilds_state_and_rejects_stale_or_blocked_inputs() -> None:
    port = PaperGridRiskDecisionPort()
    original_request = request()
    original = port.evaluate(original_request)
    assert_matching_risk_decision(port, original, original_request)

    changed = request(market_value=market(price=101.0))
    with pytest.raises(ValueError, match="stale"):
        assert_matching_risk_decision(port, original, changed)
    with pytest.raises(ValueError, match="projected_leverage_exceeded"):
        require_exposure_permission(port.evaluate(request(
            plan_value=plan(notional=110_000.0, low=50.0),
            command_rows=commands(notional=110_000.0, low=50.0),
        )))


def test_legacy_and_nautilus_have_same_economic_risk_answer_for_independent_empty_states() -> None:
    port = PaperGridRiskDecisionPort()
    legacy = port.evaluate(request(snapshot=execution_snapshot(engine="legacy_paper"))).to_dict()
    nautilus = port.evaluate(request(snapshot=execution_snapshot(engine="nautilus_paper"))).to_dict()

    assert legacy["outcome"] == nautilus["outcome"] == "allow"
    assert legacy["metrics"] == nautilus["metrics"]
    assert legacy["blockers"] == nautilus["blockers"]
    assert legacy["limits"] == nautilus["limits"]


def test_risk_store_is_deduplicated_audit_evidence_only(tmp_path: Path) -> None:
    decision = PaperGridRiskDecisionPort().evaluate(request())
    store = RiskDecisionStore(tmp_path / "outputs")
    store.persist(decision)
    store.persist(decision)

    path = tmp_path / "outputs" / "dualtrack" / "risk_decisions" / f"{CYCLE_ID}.json"
    assert len(load_json(path)) == 1
    assert load_json(path)[0]["decision_id"] == decision.decision_id
    assert load_json(path)[0]["request"]["account"]["snapshot_id"].startswith("accounting-")


def test_action_class_ignores_caller_source_and_safe_actions_are_explicit() -> None:
    assert action_class_for_command({"event": "entry", "source": "not-production"}) == "increase_exposure"
    assert action_class_for_command({"event": "flatten", "source": "spoofed"}) == "reduce_only"
    assert action_class_for_command({"event": "cancel"}) == "cancel"

    port = PaperGridRiskDecisionPort()
    reduce_request = request(action_class="reduce_only")
    reduce_payload = reduce_request.to_dict()
    reduce_payload["candidate"] = {
        "event": "flatten",
        "trade_id": "trade-1",
        "cycle_id": CYCLE_ID,
    }
    # Rebuild because direct mutation correctly invalidates the request hash.
    from schemas.risk import build_risk_request

    reduce_request = build_risk_request(
        checked_at=reduce_payload["checked_at"],
        scope=reduce_payload["scope"],
        action_class="reduce_only",
        candidate=reduce_payload["candidate"],
        account=reduce_payload["account"],
        market=reduce_payload["market"],
        execution=reduce_payload["execution"],
        policy=reduce_payload["policy"],
        evaluator=reduce_payload["evaluator"],
    )
    assert port.evaluate(reduce_request).allow_reduce_only is True


def test_canonical_execution_projection_preserves_only_thin_sl_tp_join() -> None:
    position = {
        "trade_id": "trade-1",
        "position_id": "position-1",
        "status": "open",
        "side": "long",
        "remaining_units": 2.0,
        "entry_price": 95.0,
        "sl": 90.0,
        "tp": 100.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "strategy_plan_id": "old-plan",
        "strategy_plan_version": 1,
    }
    state = canonical_execution_risk_state(execution_snapshot(positions=[position]), reconciliation())

    assert state["status"] == "ready"
    assert state["open_positions"] == [{
        "position_id": "position-1",
        "trade_id": "trade-1",
        "side": "long",
        "remaining_quantity": 2.0,
        "entry_price": 95.0,
        "sl": 90.0,
        "tp": 100.0,
        "strategy_plan_id": "old-plan",
        "strategy_plan_version": 1,
    }]


def test_manual_entry_uses_exact_quantity_and_cannot_bypass_risk_with_source() -> None:
    command = manual_command(source="spoofed-non-production")
    exact = normalize_manual_order_command(command, config=config())
    decision = PaperGridRiskDecisionPort().evaluate(manual_request(command)).to_dict()

    assert exact["quantity"] == 5.0
    assert decision["request"]["action_class"] == "increase_exposure"
    assert decision["request"]["candidate"]["kind"] == "manual_order"
    assert decision["request"]["candidate"]["commands"][0]["quantity"] == 5.0
    assert decision["outcome"] == "allow"


def test_manual_entry_blocks_budget_breach_and_existing_pending_entry() -> None:
    expensive = PaperGridRiskDecisionPort().evaluate(manual_request(manual_command(notional=150_000.0))).to_dict()
    existing_order = {
        "order_id": "working-grid-order",
        "state": "accepted",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 90.0,
        "quantity": 1.0,
    }
    layered = PaperGridRiskDecisionPort().evaluate(manual_request(
        manual_command(),
        snapshot=execution_snapshot(orders=[existing_order]),
    )).to_dict()

    assert "projected_leverage_exceeded" in {row["code"] for row in expensive["blockers"]}
    assert "existing_entry_orders_present" in {row["code"] for row in layered["blockers"]}
    assert expensive["recommendation"]["applied_automatically"] is False


def test_manual_reduce_only_requires_position_identity_but_not_entry_risk_inputs() -> None:
    close = {
        "cycle_id": CYCLE_ID,
        "ts": CHECKED_AT,
        "event": "flatten",
        "side": "sell",
        "trade_id": "trade-to-close",
    }
    allowed = PaperGridRiskDecisionPort().evaluate(build_manual_order_risk_request(
        checked_at=CHECKED_AT,
        command=close,
        account_context={},
        market=market(fresh=False),
        execution_snapshot={},
        execution_reconciliation={},
        config=config(),
        policy=grid_risk_policy(config()),
        evaluator=grid_risk_evaluator(),
    ))
    missing_identity = PaperGridRiskDecisionPort().evaluate(build_manual_order_risk_request(
        checked_at=CHECKED_AT,
        command={**close, "trade_id": ""},
        account_context={},
        market=market(fresh=False),
        execution_snapshot={},
        execution_reconciliation={},
        config=config(),
        policy=grid_risk_policy(config()),
        evaluator=grid_risk_evaluator(),
    ))

    assert allowed.allow_reduce_only is True
    assert missing_identity.to_dict()["primary_blocker"]["code"] == "close_identity_invalid"


def test_live_money_bridge_preserves_blocker_order_and_never_grants_more_permission(tmp_path: Path) -> None:
    class FakeLegacyGuardrails:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate_order(self, *_args, **_kwargs) -> dict:
            self.calls += 1
            return {
                "checked_at": CHECKED_AT,
                "status": "BLOCKED_DAILY_LOSS_LIMIT",
                "allows_new_order": True,  # contradictory legacy flag cannot loosen blockers
                "blockers": [
                    {
                        "status": "BLOCKED_DAILY_LOSS_LIMIT",
                        "code": "daily_loss_limit",
                        "source": "live_money_guardrails.daily_loss",
                        "message": "daily loss reached limit",
                        "evidence": {"loss_pct": 2.0},
                    },
                    {
                        "status": "BLOCKED_DAILY_TRADE_LIMIT",
                        "code": "daily_trade_limit",
                        "source": "live_money_guardrails.daily_entry_orders",
                        "message": "daily entries reached limit",
                        "evidence": {"count": 2},
                    },
                ],
                "limits": {"daily_loss_limit_pct": 1.25},
                "candidate": {"notional": 10.0},
                "daily_loss": {"known": True, "loss_pct": 2.0},
                "exposure": {"projected_total_notional": 10.0},
                "daily_entry_orders": {"count": 2},
                "halt": {},
            }

    legacy = FakeLegacyGuardrails()
    result = LiveMoneyRiskDecisionAdapter(
        tmp_path / "outputs",
        legacy_guardrails=legacy,
        store=RiskDecisionStore(tmp_path / "outputs"),
    ).evaluate_order(
        "2026-07-18",
        ticket={"ticket_id": "ticket-live-1"},
        symbol="XAUUSDT",
        side="BUY",
        requested_price=100.0,
        quantity=0.1,
        source="binance_usdm:testnet",
        reconciliation={"status": "pass"},
        checked_at=CHECKED_AT,
    )

    assert legacy.calls == 1
    assert canonical_live_risk_allows_exposure(result) is False
    assert canonical_live_risk_allows_exposure({}) is False
    assert [row["code"] for row in result["risk_decision"]["blockers"]] == [
        "daily_loss_limit",
        "daily_trade_limit",
    ]


def test_live_money_bridge_fails_closed_when_legacy_status_is_not_ready(tmp_path: Path) -> None:
    class FakeLegacyGuardrails:
        def evaluate_order(self, *_args, **_kwargs) -> dict:
            return {
                "checked_at": CHECKED_AT,
                "status": "ERROR",
                "allows_new_order": True,
                "blockers": [],
                "limits": {},
                "candidate": {"notional": 10.0},
                "daily_loss": {"known": False},
                "exposure": {},
                "daily_entry_orders": {},
                "halt": {},
            }

    result = LiveMoneyRiskDecisionAdapter(
        tmp_path / "outputs",
        legacy_guardrails=FakeLegacyGuardrails(),
        store=RiskDecisionStore(tmp_path / "outputs"),
    ).evaluate_order(
        "2026-07-18",
        ticket={"ticket_id": "ticket-live-invalid-status"},
        symbol="XAUUSDT",
        side="BUY",
        requested_price=100.0,
        quantity=0.1,
        source="binance_usdm:testnet",
        checked_at=CHECKED_AT,
    )

    assert canonical_live_risk_allows_exposure(result) is False
    assert [row["code"] for row in result["risk_decision"]["blockers"]] == [
        "legacy_guardrail_status_invalid"
    ]


def test_live_money_bridge_allows_clean_entry_and_skips_entry_guardrails_for_reduce_only(tmp_path: Path) -> None:
    class FakeLegacyGuardrails:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate_order(self, *_args, **_kwargs) -> dict:
            self.calls += 1
            return {
                "checked_at": CHECKED_AT,
                "status": "READY",
                "allows_new_order": True,
                "blockers": [],
                "limits": {},
                "candidate": {"notional": 10.0},
                "daily_loss": {"known": True, "loss_pct": 0.0},
                "exposure": {"projected_total_notional": 10.0},
                "daily_entry_orders": {"count": 0},
                "halt": {},
            }

    legacy = FakeLegacyGuardrails()
    bridge = LiveMoneyRiskDecisionAdapter(
        tmp_path / "outputs",
        legacy_guardrails=legacy,
        store=RiskDecisionStore(tmp_path / "outputs"),
    )
    allowed = bridge.evaluate_order(
        "2026-07-18",
        ticket={"ticket_id": "ticket-live-2"},
        symbol="XAUUSDT",
        side="BUY",
        requested_price=100.0,
        quantity=0.1,
        source="binance_usdm:testnet",
        checked_at=CHECKED_AT,
    )
    reduced = bridge.evaluate_order(
        "2026-07-18",
        ticket={"event": "flatten", "trade_id": "venue-trade-1"},
        symbol="XAUUSDT",
        side="SELL",
        requested_price=100.0,
        quantity=0.1,
        source="binance_usdm:testnet",
        action_class="reduce_only",
        checked_at=CHECKED_AT,
    )

    assert canonical_live_risk_allows_exposure(allowed) is True
    assert reduced["risk_decision"]["allow_reduce_only"] is True
    assert legacy.calls == 1
