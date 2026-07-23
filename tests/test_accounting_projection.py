from __future__ import annotations

import json

import pytest

from services.accounting_projection import (
    AccountingContractError,
    project_execution_accounting,
)


def _snapshot(
    *,
    fills: list[dict],
    positions: list[dict],
    realized: float,
    unrealized: float | None,
    fees: float,
    funding: float = 0.0,
) -> dict:
    starting = 10_000.0
    ending = starting + realized
    return {
        "schema_version": "dualtrack-execution-v1",
        "engine": "legacy_paper",
        "cycle_id": "2026-07-18_DAY",
        "orders": [
            {
                "order_id": f"order-{index}",
                "state": "filled",
                "side": fill["side"],
                "event": fill["event"],
                "order_type": "market",
                "price": fill["price"],
                "quantity": fill["quantity"],
                "ts": fill["ts"],
            }
            for index, fill in enumerate(fills, start=1)
        ],
        "fills": fills,
        "positions": positions,
        "account": {
            "currency": "USDT",
            "starting_cash": starting,
            "realized_pnl": realized,
            "ending_cash": ending,
            "equity": None if unrealized is None else ending + unrealized,
            "margin": 10.0 if any(row["status"] == "open" for row in positions) else 0.0,
            "exposure": 100.0 if any(row["status"] == "open" for row in positions) else 0.0,
            "slippage": 0.0,
            "fees": fees,
            "funding": funding,
        },
        "pnl": {"realized": realized, "unrealized": unrealized},
    }


def _entry(fill_id: str = "entry-1", *, quantity: float = 1.0, realized: float = -1.0) -> dict:
    return {
        "fill_id": fill_id,
        "order_id": fill_id,
        "trade_id": "trade-1",
        "event": "entry",
        "side": "buy",
        "price": 100.0,
        "quantity": quantity,
        "cost": 1.0,
        "gross_pnl": 0.0,
        "realized_pnl": realized,
        "ts": "2026-07-18T01:00:00+00:00",
        "strategy_plan_id": "plan-1",
        "strategy_plan_version": 1,
    }


def _position(*, status: str, remaining: float, realized: float, exit_price: float | None = None) -> dict:
    return {
        "position_id": "position-1",
        "trade_id": "trade-1",
        "status": status,
        "side": "long",
        "remaining_units": remaining,
        "entry_price": 100.0,
        "exit_price": exit_price,
        "entry_ts": "2026-07-18T01:00:00+00:00",
        "exit_ts": "2026-07-18T01:05:00+00:00" if status == "closed" else None,
        "realized_pnl": realized,
        "unrealized_pnl": 5.0 if status == "open" else 0.0,
        "strategy_plan_id": "plan-1",
        "strategy_plan_version": 1,
    }


def test_open_entry_is_one_trade_but_not_a_completed_trade() -> None:
    source = _snapshot(
        fills=[_entry()],
        positions=[_position(status="open", remaining=1.0, realized=-1.0)],
        realized=-1.0,
        unrealized=5.0,
        fees=1.0,
    )

    result = project_execution_accounting(source).to_dict()

    assert result["counts"] == {
        "order_count": 1,
        "open_order_count": 0,
        "fill_count": 1,
        "entry_fill_count": 1,
        "exit_fill_count": 0,
        "trade_count": 1,
        "open_trade_count": 1,
        "completed_trade_count": 0,
        "position_count": 1,
        "open_position_count": 1,
    }
    assert result["pnl"] == {
        "gross_realized_pnl": 0.0,
        "fees": 1.0,
        "funding": 0.0,
        "net_realized_pnl": -1.0,
        "unrealized_pnl": 5.0,
        "net_pnl": 4.0,
        "slippage": 0.0,
    }
    assert result["reconciliation"]["status"] == "pass"


def test_entry_and_exit_remain_one_trade_and_become_one_completed_trade() -> None:
    entry = _entry()
    exit_fill = {
        "fill_id": "exit-1",
        "order_id": "exit-1",
        "trade_id": "trade-1",
        "event": "target",
        "side": "sell",
        "price": 110.0,
        "quantity": 1.0,
        "cost": 1.0,
        "gross_pnl": 10.0,
        "realized_pnl": 9.0,
        "matched_entries": [{"trade_id": "trade-1", "units": 1.0}],
        "ts": "2026-07-18T01:05:00+00:00",
    }
    source = _snapshot(
        fills=[entry, exit_fill],
        positions=[_position(status="closed", remaining=0.0, realized=8.0, exit_price=110.0)],
        realized=8.0,
        unrealized=0.0,
        fees=2.0,
    )

    result = project_execution_accounting(source).to_dict()

    assert result["counts"]["fill_count"] == 2
    assert result["counts"]["entry_fill_count"] == 1
    assert result["counts"]["exit_fill_count"] == 1
    assert result["counts"]["trade_count"] == 1
    assert result["counts"]["open_trade_count"] == 0
    assert result["counts"]["completed_trade_count"] == 1
    assert result["trades"][0]["status"] == "closed"
    assert result["pnl"]["gross_realized_pnl"] == 10.0
    assert result["pnl"]["net_realized_pnl"] == 8.0


def test_closed_trade_with_inverted_timestamps_is_retained_as_a_diagnostic() -> None:
    entry = _entry()
    exit_fill = {
        **_entry("exit-1", realized=9.0),
        "event": "target",
        "side": "sell",
        "price": 110.0,
        "gross_pnl": 10.0,
        "ts": "2026-07-18T00:55:00-00:00",
    }
    position = _position(status="closed", remaining=0.0, realized=8.0, exit_price=110.0)
    position["exit_ts"] = "2026-07-18T08:55:00+08:00"
    source = _snapshot(
        fills=[entry, exit_fill],
        positions=[position],
        realized=8.0,
        unrealized=0.0,
        fees=2.0,
    )

    result = project_execution_accounting(source).to_dict()

    # #212: the identified anomaly is quarantined, permanently visible, and
    # excluded from gating issues — immutable history must not pin the
    # reconciliation status in drift and lock out unrelated new starts.
    assert result["reconciliation"]["status"] == "pass"
    assert result["reconciliation"]["issues"] == []
    assert result["reconciliation"]["quarantined"] == [{
        "code": "closed_trade_exit_before_entry",
        "trade_id": "trade-1",
        "entry_ts": "2026-07-18T01:00:00+00:00",
        "exit_ts": "2026-07-18T08:55:00+08:00",
    }]


def test_quarantined_chronology_anomaly_does_not_mask_a_real_account_mismatch() -> None:
    entry = _entry()
    exit_fill = {
        **_entry("exit-1", realized=9.0),
        "event": "target",
        "side": "sell",
        "price": 110.0,
        "gross_pnl": 10.0,
        "ts": "2026-07-18T00:55:00-00:00",
    }
    position = _position(status="closed", remaining=0.0, realized=8.0, exit_price=110.0)
    position["exit_ts"] = "2026-07-18T08:55:00+08:00"
    source = _snapshot(
        fills=[entry, exit_fill],
        positions=[position],
        realized=8.0,
        unrealized=0.0,
        fees=2.0,
    )
    source["account"]["equity"] = source["account"]["equity"] + 123.45

    result = project_execution_accounting(source).to_dict()

    codes = [issue["code"] for issue in result["reconciliation"]["issues"]]
    assert result["reconciliation"]["status"] == "drift"
    assert "closed_trade_exit_before_entry" not in codes
    assert codes  # the genuine mismatch still gates
    assert [row["code"] for row in result["reconciliation"]["quarantined"]] == [
        "closed_trade_exit_before_entry"
    ]


def test_closed_trade_with_same_second_timestamp_is_not_marked_inverted() -> None:
    entry = _entry()
    entry["ts"] = "2026-07-18T09:00:00+08:00"
    exit_fill = {
        **_entry("exit-1", realized=9.0),
        "event": "target",
        "side": "sell",
        "price": 110.0,
        "gross_pnl": 10.0,
        "ts": "2026-07-18T01:00:00Z",
    }
    position = _position(status="closed", remaining=0.0, realized=8.0, exit_price=110.0)
    position["entry_ts"] = "2026-07-18T09:00:00+08:00"
    position["exit_ts"] = "2026-07-18T01:00:00Z"
    source = _snapshot(
        fills=[entry, exit_fill],
        positions=[position],
        realized=8.0,
        unrealized=0.0,
        fees=2.0,
    )

    result = project_execution_accounting(source).to_dict()

    assert result["reconciliation"]["status"] == "pass"


def test_scale_in_and_partial_close_stay_one_open_trade() -> None:
    first = _entry("entry-1", quantity=1.0, realized=-1.0)
    second = {
        **_entry("entry-2", quantity=2.0, realized=-2.0),
        "price": 99.0,
        "cost": 2.0,
        "ts": "2026-07-18T01:01:00+00:00",
    }
    partial = {
        "fill_id": "exit-1",
        "order_id": "exit-1",
        "trade_id": "trade-1",
        "event": "exit",
        "side": "sell",
        "price": 105.0,
        "quantity": 1.5,
        "cost": 1.0,
        "gross_pnl": 8.5,
        "realized_pnl": 7.5,
        "matched_entries": [{"trade_id": "trade-1", "units": 1.5}],
        "ts": "2026-07-18T01:05:00+00:00",
    }
    source = _snapshot(
        fills=[first, second, partial],
        positions=[_position(status="open", remaining=1.5, realized=4.5)],
        realized=4.5,
        unrealized=7.5,
        fees=4.0,
    )

    result = project_execution_accounting(source).to_dict()

    assert result["counts"]["entry_fill_count"] == 2
    assert result["counts"]["exit_fill_count"] == 1
    assert result["counts"]["trade_count"] == 1
    assert result["counts"]["completed_trade_count"] == 0
    assert result["trades"][0]["remaining_quantity"] == 1.5


def test_exact_duplicate_fill_is_collapsed_and_reported_without_double_counting() -> None:
    entry = _entry()
    source = _snapshot(
        fills=[entry, dict(entry)],
        positions=[_position(status="open", remaining=1.0, realized=-1.0)],
        realized=-1.0,
        unrealized=5.0,
        fees=1.0,
    )

    result = project_execution_accounting(source).to_dict()

    assert result["counts"]["fill_count"] == 1
    assert result["pnl"]["net_realized_pnl"] == -1.0
    assert result["reconciliation"]["status"] == "drift"
    assert result["reconciliation"]["issues"] == [
        {"code": "duplicate_fill_id", "fill_id": "entry-1"}
    ]


def test_conflicting_duplicate_fill_identity_fails_closed() -> None:
    entry = _entry()
    conflicting = {**entry, "price": 101.0}
    source = _snapshot(
        fills=[entry, conflicting],
        positions=[_position(status="open", remaining=1.0, realized=-1.0)],
        realized=-1.0,
        unrealized=5.0,
        fees=1.0,
    )

    with pytest.raises(AccountingContractError, match="conflicting fill identity"):
        project_execution_accounting(source)


def test_orphan_exit_and_account_identity_drift_are_explicit() -> None:
    entry = _entry()
    orphan = {
        "fill_id": "orphan-exit",
        "trade_id": "missing-trade",
        "event": "exit",
        "side": "sell",
        "price": 100.0,
        "quantity": 1.0,
        "cost": 0.0,
        "gross_pnl": 0.0,
        "realized_pnl": 0.0,
        "ts": "2026-07-18T01:05:00+00:00",
    }
    source = _snapshot(
        fills=[entry, orphan],
        positions=[_position(status="open", remaining=1.0, realized=-1.0)],
        realized=-1.0,
        unrealized=5.0,
        fees=1.0,
    )
    source["account"]["equity"] = 99_999.0

    result = project_execution_accounting(source).to_dict()
    codes = {row["code"] for row in result["reconciliation"]["issues"]}

    assert result["reconciliation"]["status"] == "drift"
    assert "orphan_exit_fill" in codes
    assert "account_equity_mismatch" in codes


def test_legacy_flatten_command_trade_id_is_rebound_by_unique_closed_position_evidence() -> None:
    flatten = {
        "fill_id": "flatten-fill",
        "order_id": "flatten-command",
        "trade_id": "flatten-command",
        "event": "flatten",
        "side": "sell",
        "price": 99.0,
        "quantity": 1.0,
        "cost": 0.0,
        "ts": "2026-07-18T01:05:00+00:00",
        "strategy_plan_id": "plan-1",
        "strategy_plan_version": 1,
    }
    source = _snapshot(
        fills=[_entry(), flatten],
        positions=[_position(status="closed", remaining=0.0, realized=-2.0, exit_price=99.0)],
        realized=-2.0,
        unrealized=0.0,
        fees=1.0,
    )
    source["engine"] = "nautilus_paper"

    result = project_execution_accounting(source).to_dict()
    projected = next(row for row in result["fills"] if row["fill_id"] == "flatten-fill")

    assert result["reconciliation"]["status"] == "pass"
    assert projected["trade_id"] == "trade-1"
    assert projected["source_trade_id"] == "flatten-command"
    assert projected["identity_resolution"] == "legacy_flatten_unique_closed_position"
    assert result["trades"][0]["exit_fill_ids"] == ["flatten-fill"]


def test_legacy_flatten_trade_id_remains_drift_when_closed_position_match_is_ambiguous() -> None:
    entry_two = {**_entry("entry-2"), "trade_id": "trade-2"}
    position_two = {
        **_position(status="closed", remaining=0.0, realized=-2.0, exit_price=99.0),
        "position_id": "position-2",
        "trade_id": "trade-2",
    }
    flatten = {
        "fill_id": "flatten-fill",
        "order_id": "flatten-command",
        "trade_id": "flatten-command",
        "event": "flatten",
        "side": "sell",
        "price": 99.0,
        "quantity": 1.0,
        "cost": 0.0,
        "ts": "2026-07-18T01:05:00+00:00",
        "strategy_plan_id": "plan-1",
        "strategy_plan_version": 1,
    }
    source = _snapshot(
        fills=[_entry(), entry_two, flatten],
        positions=[
            _position(status="closed", remaining=0.0, realized=-2.0, exit_price=99.0),
            position_two,
        ],
        realized=-4.0,
        unrealized=0.0,
        fees=2.0,
    )
    source["engine"] = "nautilus_paper"

    result = project_execution_accounting(source).to_dict()
    projected = next(row for row in result["fills"] if row["fill_id"] == "flatten-fill")
    codes = {row["code"] for row in result["reconciliation"]["issues"]}

    assert result["reconciliation"]["status"] == "drift"
    assert projected["trade_id"] == "flatten-command"
    assert "source_trade_id" not in projected
    assert "orphan_exit_fill" in codes


@pytest.mark.parametrize(
    ("engine", "order_id"),
    [
        ("legacy_paper", "flatten-command"),
        ("nautilus_paper", "different-order-id"),
    ],
)
def test_legacy_flatten_repair_rejects_other_engines_and_non_command_identity(
    engine: str,
    order_id: str,
) -> None:
    flatten = {
        "fill_id": "flatten-fill",
        "order_id": order_id,
        "trade_id": "flatten-command",
        "event": "flatten",
        "side": "sell",
        "price": 99.0,
        "quantity": 1.0,
        "cost": 0.0,
        "ts": "2026-07-18T01:05:00+00:00",
        "strategy_plan_id": "plan-1",
        "strategy_plan_version": 1,
    }
    source = _snapshot(
        fills=[_entry(), flatten],
        positions=[_position(status="closed", remaining=0.0, realized=-2.0, exit_price=99.0)],
        realized=-2.0,
        unrealized=0.0,
        fees=1.0,
    )
    source["engine"] = engine

    result = project_execution_accounting(source).to_dict()
    projected = next(row for row in result["fills"] if row["fill_id"] == "flatten-fill")

    assert result["reconciliation"]["status"] == "drift"
    assert projected["trade_id"] == "flatten-command"
    assert "source_trade_id" not in projected
    assert "orphan_exit_fill" in {
        row["code"] for row in result["reconciliation"]["issues"]
    }


def test_negative_remaining_quantity_is_never_silently_normalized_to_zero() -> None:
    source = _snapshot(
        fills=[_entry()],
        positions=[_position(status="open", remaining=-0.25, realized=-1.0)],
        realized=-1.0,
        unrealized=5.0,
        fees=1.0,
    )

    result = project_execution_accounting(source).to_dict()
    codes = {row["code"] for row in result["reconciliation"]["issues"]}

    assert result["positions"][0]["remaining_quantity"] == -0.25
    assert "negative_position_quantity" in codes
    assert result["reconciliation"]["status"] == "drift"


def test_same_execution_facts_reproject_to_same_snapshot_id() -> None:
    source = _snapshot(
        fills=[_entry()],
        positions=[_position(status="open", remaining=1.0, realized=-1.0)],
        realized=-1.0,
        unrealized=5.0,
        fees=1.0,
    )

    assert project_execution_accounting(source).snapshot_id == project_execution_accounting(source).snapshot_id


def test_market_order_non_finite_price_is_omitted_without_hiding_accounting() -> None:
    source = _snapshot(
        fills=[_entry()],
        positions=[_position(status="open", remaining=1.0, realized=-1.0)],
        realized=-1.0,
        unrealized=5.0,
        fees=1.0,
    )
    source["orders"][0]["price"] = float("nan")
    source["orders"][0]["requested_price"] = 100.0

    result = project_execution_accounting(source).to_dict()

    assert "price" not in result["orders"][0]
    assert result["orders"][0]["requested_price"] == 100.0
    assert result["reconciliation"]["status"] == "pass"
    assert result["reconciliation"]["issues"] == []
    assert {row["code"] for row in result["reconciliation"]["warnings"]} == {
        "market_order_non_finite_price_omitted"
    }
    json.dumps(result, allow_nan=False)


def test_limit_order_non_finite_price_still_fails_closed() -> None:
    source = _snapshot(
        fills=[_entry()],
        positions=[_position(status="open", remaining=1.0, realized=-1.0)],
        realized=-1.0,
        unrealized=5.0,
        fees=1.0,
    )
    source["orders"][0].update({"order_type": "limit", "price": float("nan")})

    with pytest.raises(AccountingContractError, match="order price must be finite"):
        project_execution_accounting(source)


def test_unobserved_slippage_remains_unknown_instead_of_becoming_zero() -> None:
    source = _snapshot(
        fills=[_entry()],
        positions=[_position(status="open", remaining=1.0, realized=-1.0)],
        realized=-1.0,
        unrealized=5.0,
        fees=1.0,
    )
    del source["account"]["slippage"]

    result = project_execution_accounting(source).to_dict()

    assert result["pnl"]["slippage"] is None
    assert result["completeness"]["observed"]["slippage_observed"] is False
    assert "slippage" in result["completeness"]["limitations"]


def test_legacy_and_nautilus_sources_share_one_economic_contract() -> None:
    legacy = _snapshot(
        fills=[_entry()],
        positions=[_position(status="open", remaining=1.0, realized=-1.0)],
        realized=-1.0,
        unrealized=5.0,
        fees=1.0,
    )
    nautilus = {**legacy, "engine": "nautilus_paper"}

    legacy_view = project_execution_accounting(legacy).to_dict()
    nautilus_view = project_execution_accounting(nautilus).to_dict()

    assert legacy_view["source_name"] == "legacy_paper"
    assert nautilus_view["source_name"] == "nautilus_paper"
    for field in ("counts", "pnl", "account", "orders", "fills", "positions", "trades"):
        assert legacy_view[field] == nautilus_view[field]
