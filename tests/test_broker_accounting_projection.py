from __future__ import annotations

import pytest

from services import accounting_projection
from services.accounting_projection import (
    AccountingContractError,
    broker_accounting_snapshot_payload,
    project_broker_accounting,
    project_broker_accounting_fail_honest,
)


def _binance_report() -> dict:
    return {
        "run_date": "2026-07-18",
        "provider": "binance_usdm",
        "reconciled": True,
        "confirmation_status": "confirmed_open",
        "error": "",
        "drift_count": 0,
        "drifts": [],
        "exchange_open_orders": [
            {
                "symbol": "XAUUSDT",
                "order_id": "order-3",
                "client_order_id": "grid-3",
                "type": "LIMIT",
                "side": "BUY",
                "orig_qty": 0.02,
                "status": "NEW",
                "source": "open_orders",
            }
        ],
        "exchange_fills": [
            {
                "symbol": "XAUUSDT",
                "id": "fill-1",
                "order_id": "order-1",
                "side": "BUY",
                "price": 4000.0,
                "qty": 0.02,
                "quote_qty": 80.0,
                "commission": 0.5,
                "commission_asset": "USDT",
                "realized_pnl": 0.0,
                "time": 1_752_806_400_000,
            },
            {
                "symbol": "XAUUSDT",
                "id": "fill-2",
                "order_id": "order-2",
                "side": "SELL",
                "price": 4050.0,
                "qty": 0.02,
                "quote_qty": 81.0,
                "commission": 0.5,
                "commission_asset": "USDT",
                "realized_pnl": 10.0,
                "time": 1_752_806_460_000,
            },
        ],
        "exchange_positions": [
            {
                "symbol": "XAUUSDT",
                "position_amt": 0.01,
                "entry_price": 4020.0,
                "unrealized_pnl": 2.0,
            }
        ],
        "exchange_balance": {
            "asset": "USDT",
            "balance": 10_008.5,
            "available": 9_000.0,
            "balance_present": True,
            "truth_source": "fapi/v2/balance",
        },
        "exchange_accounting": {
            "trade_count": 2,
            "income_count": 1,
            "realized_pnl_from_fills": 10.0,
            "commission_by_asset": {"USDT": 1.0},
            "funding_by_asset": {"USDT": -0.5},
            "net_realized_pnl_estimate": 8.5,
            "net_realized_pnl_asset": "USDT",
            "utc_trading_day": {
                "run_date": "2026-07-18",
                "start_time_ms": 1_752_806_400_000,
                "end_time_ms": 1_752_892_799_999,
            },
        },
        "account_observation": {
            "account_observed": True,
            "balance_present": True,
            "history_requested": True,
            "fills_observed": True,
            "income_observed": True,
            "fills_count": 2,
            "income_count": 1,
            "symbol": "XAUUSDT",
        },
        "checked_at": "2026-07-18T02:00:00+00:00",
    }


def test_binance_receipt_projects_account_economics_without_inventing_round_trips() -> None:
    snapshot = project_broker_accounting(_binance_report()).to_dict()

    assert snapshot["schema_version"] == "accounting-snapshot-v1"
    assert snapshot["source_type"] == "broker_reconciliation"
    assert snapshot["source_name"] == "binance_usdm"
    assert snapshot["currency"] == "USDT"
    assert snapshot["counts"] == {
        "order_count": 1,
        "open_order_count": 1,
        "fill_count": 2,
        "entry_fill_count": None,
        "exit_fill_count": None,
        "trade_count": None,
        "open_trade_count": None,
        "completed_trade_count": None,
        "position_count": 1,
        "open_position_count": 1,
    }
    assert snapshot["pnl"] == {
        "gross_realized_pnl": 10.0,
        "fees": 1.0,
        "funding": -0.5,
        "net_realized_pnl": 8.5,
        "unrealized_pnl": 2.0,
        "net_pnl": 10.5,
        "slippage": None,
    }
    assert snapshot["account"]["ending_cash"] == 10_008.5
    assert snapshot["account"]["equity"] == 10_010.5
    assert snapshot["reconciliation"]["status"] == "pass"
    assert snapshot["completeness"]["status"] == "partial"
    assert "trade_lifecycle_classification" in snapshot["completeness"]["limitations"]
    assert project_broker_accounting(_binance_report()).snapshot_id == project_broker_accounting(_binance_report()).snapshot_id


def test_failed_binance_observation_keeps_counts_and_money_unknown_not_zero() -> None:
    report = _binance_report()
    report.update({
        "reconciled": False,
        "confirmation_status": "cannot_confirm",
        "error": "URLError: upstream unavailable",
        "exchange_open_orders": [],
        "exchange_fills": [],
        "exchange_positions": [],
        "exchange_balance": {},
        "exchange_accounting": {},
        "account_observation": {
            "account_observed": False,
            "balance_present": False,
            "history_requested": True,
            "fills_observed": False,
            "income_observed": False,
        },
    })

    snapshot = project_broker_accounting(report).to_dict()

    assert all(value is None for value in snapshot["counts"].values())
    assert snapshot["pnl"]["net_realized_pnl"] is None
    assert snapshot["pnl"]["unrealized_pnl"] is None
    assert snapshot["account"]["equity"] is None
    assert snapshot["reconciliation"]["status"] == "blocked"
    assert snapshot["completeness"]["unknown_is_not_zero"] is True


def test_fail_honest_broker_projection_preserves_a_blocked_contract_for_malformed_rows() -> None:
    report = _binance_report()
    report["exchange_open_orders"][0]["order_id"] = ""

    with pytest.raises(AccountingContractError, match="broker order_id is required"):
        project_broker_accounting(report)

    snapshot = project_broker_accounting_fail_honest(report).to_dict()

    assert snapshot["schema_version"] == "accounting-snapshot-v1"
    assert all(value is None for value in snapshot["counts"].values())
    assert all(value is None for value in snapshot["pnl"].values())
    assert all(value is None for value in snapshot["account"].values())
    assert snapshot["completeness"]["status"] == "blocked"
    assert snapshot["completeness"]["unknown_is_not_zero"] is True
    assert snapshot["reconciliation"]["status"] == "blocked"
    assert snapshot["reconciliation"]["issues"] == [
        {
            "code": "accounting_projection_failed",
            "error_type": "AccountingContractError",
            "detail": "broker order_id is required",
        }
    ]


def test_broker_payload_has_a_versioned_emergency_receipt_if_snapshot_serialization_fails(monkeypatch) -> None:
    class BrokenSnapshot:
        def to_dict(self):
            raise RuntimeError("must not escape into reconciliation persistence")

    monkeypatch.setattr(
        accounting_projection,
        "project_broker_accounting_fail_honest",
        lambda _source: BrokenSnapshot(),
    )

    payload = broker_accounting_snapshot_payload(_binance_report())

    assert payload == {
        "schema_version": "accounting-projection-unavailable-v1",
        "status": "blocked",
        "error_type": "RuntimeError",
        "completeness": {
            "status": "blocked",
            "unknown_is_not_zero": True,
        },
        "reconciliation": {
            "status": "blocked",
            "issues": [{
                "code": "accounting_projection_unavailable",
                "error_type": "RuntimeError",
            }],
        },
    }
    assert "must not escape" not in str(payload)


def test_exact_duplicate_broker_fill_is_collapsed_and_reported() -> None:
    report = _binance_report()
    report["exchange_fills"].append(dict(report["exchange_fills"][0]))

    snapshot = project_broker_accounting(report).to_dict()

    assert snapshot["counts"]["fill_count"] == 2
    assert snapshot["reconciliation"]["status"] == "drift"
    assert {row["code"] for row in snapshot["reconciliation"]["issues"]} == {"duplicate_fill_id"}


def test_tiger_aggregate_accounting_uses_same_contract_but_keeps_lifecycle_unknown() -> None:
    report = {
        "run_date": "2026-07-18",
        "provider": "tiger_openapi",
        "mode": "paper",
        "sync_status": "synced",
        "error": "",
        "account_observation": {
            "account_observed": True,
            "balance_present": True,
            "accounting_observed": True,
            "base_currency": "USD",
        },
        "exchange_balance": {
            "asset": "USD",
            "balance": 10_000.0,
            "available": 8_500.0,
            "balance_present": True,
            "source": "tiger_openapi.get_prime_assets.selected_segment.net_liquidation",
        },
        "exchange_accounting": {
            "net_realized_pnl_estimate": -12.75,
            "unrealized_pnl_estimate": 3.5,
            "realized_pnl_present": True,
            "unrealized_pnl_present": True,
            "utc_trading_day": {"run_date": "2026-07-18"},
            "source": "tiger_openapi.get_prime_assets.selected_segment.realized_pl",
        },
    }

    snapshot = project_broker_accounting(report).to_dict()

    assert snapshot["source_name"] == "tiger_openapi"
    assert snapshot["currency"] == "USD"
    assert all(value is None for value in snapshot["counts"].values())
    assert snapshot["pnl"]["gross_realized_pnl"] is None
    assert snapshot["pnl"]["fees"] is None
    assert snapshot["pnl"]["funding"] is None
    assert snapshot["pnl"]["net_realized_pnl"] == -12.75
    assert snapshot["pnl"]["unrealized_pnl"] == 3.5
    assert snapshot["account"]["ending_cash"] is None
    assert snapshot["account"]["equity"] == 10_000.0
    assert snapshot["reconciliation"]["status"] == "pass"
    assert snapshot["completeness"]["status"] == "partial"
