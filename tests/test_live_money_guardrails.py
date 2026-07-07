from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone

from services.journal_store import load_json, write_json
from services.live_money_guardrails import LiveHaltStore, LiveMoneyGuardrails

_RUN_DATE = "2026-06-09"
_DAY_START_MS = 1780963200000
_DAY_END_MS = 1781049599999


def _checked_at(seconds_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()


def _reconciliation(
    *,
    run_date: str = _RUN_DATE,
    balance: float = 1000.0,
    realized: float = 0.0,
    unrealized: float = 0.0,
    position_amt: float = 0.0,
    entry_price: float = 4500.0,
    checked_at: str | None = None,
    error: str = "",
    utc_trading_day: dict | None = None,
    account_observed: bool = True,
    balance_present: bool = True,
) -> dict:
    positions = []
    if position_amt:
        positions.append({"symbol": "XAUUSDT", "position_amt": position_amt, "entry_price": entry_price, "unrealized_pnl": unrealized})
    return {
        "run_date": run_date,
        "checked_at": checked_at or _checked_at(),
        "error": error,
        "confirmation_status": "confirmed_flat" if not positions else "confirmed_open",
        "exchange_balance": {"asset": "USDT", "balance": balance, "available": balance, "balance_present": balance_present},
        "account_observation": {
            "account_observed": account_observed,
            "balance_present": balance_present,
            "history_requested": True,
            "fills_observed": account_observed,
            "income_observed": account_observed,
            "fills_count": 0,
            "income_count": 0,
            "symbol": "XAUUSDT",
            "reason": "test account observation",
        },
        "exchange_positions": positions,
        "exchange_accounting": {
            "net_realized_pnl_estimate": realized,
            "utc_trading_day": utc_trading_day or {"run_date": run_date, "start_time_ms": _DAY_START_MS, "end_time_ms": _DAY_END_MS},
        },
    }


def _ticket() -> dict:
    return {"ticket_id": "ticket_guard", "asset": "GOLD", "action": "prepare_buy"}


def _evaluate(root: Path, reconciliation: dict, *, price: float = 4500.0, quantity: float = 0.002) -> dict:
    return LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket=_ticket(),
        symbol="XAUUSDT",
        side="BUY",
        requested_price=price,
        quantity=quantity,
        source="binance_usdm:testnet",
        reconciliation=reconciliation,
    )


def test_live_money_guardrails_block_daily_loss_with_realized_unrealized_costs(tmp_path: Path):
    root = tmp_path / "outputs"

    result = _evaluate(root, _reconciliation(balance=1000, realized=-8.0, unrealized=-5.0, position_amt=0.001))

    assert result["allows_new_order"] is False
    assert result["status"] == "BLOCKED_DAILY_LOSS_LIMIT"
    assert result["primary_blocker"]["code"] == "daily_loss_limit"
    assert result["daily_loss"]["loss_pct"] == 1.3
    saved = load_json(root / "live_money_guardrails" / "current.json")[0]
    assert saved["status"] == "BLOCKED_DAILY_LOSS_LIMIT"


def test_live_money_guardrails_fail_closed_on_nan_realized_pnl(tmp_path: Path):
    root = tmp_path / "outputs"

    result = _evaluate(root, _reconciliation(balance=1000, realized=float("nan")))

    assert result["allows_new_order"] is False
    assert result["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"
    assert result["primary_blocker"]["code"] == "daily_loss_unknown"
    assert result["daily_loss"]["known"] is False
    assert "non-finite" in result["daily_loss"]["reason"]


def test_live_money_guardrails_fail_closed_on_fetch_error_stale_missing_and_utc_day_mismatch(tmp_path: Path):
    root = tmp_path / "outputs"

    cases = [
        (_reconciliation(error="URLError: timed out"), "fetch error"),
        (_reconciliation(checked_at=_checked_at(seconds_ago=601)), "stale"),
        ({}, "missing"),
        (_reconciliation(run_date="2026-06-08", utc_trading_day={"run_date": "2026-06-08", "start_time_ms": 1780876800000, "end_time_ms": 1780963199999}), "run_date does not match"),
        (_reconciliation(utc_trading_day={"run_date": _RUN_DATE, "start_time_ms": _DAY_START_MS + 1, "end_time_ms": _DAY_END_MS}), "window does not match"),
    ]

    for reconciliation, reason_fragment in cases:
        result = _evaluate(root, reconciliation)
        assert result["allows_new_order"] is False
        assert result["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"
        assert result["primary_blocker"]["code"] == "daily_loss_unknown"
        assert reason_fragment in result["daily_loss"]["reason"]


def test_live_money_guardrails_disk_fallback_does_not_treat_stale_or_error_artifact_as_zero_loss(tmp_path: Path):
    root = tmp_path / "outputs"
    write_json(root / "live_reconciliation" / "current.json", [_reconciliation(checked_at=_checked_at(seconds_ago=601), realized=0.0)])

    stale = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket=_ticket(),
        symbol="XAUUSDT",
        side="BUY",
        requested_price=4500.0,
        quantity=0.002,
        source="binance_usdm:testnet",
    )

    assert stale["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"
    assert stale["primary_blocker"]["code"] == "daily_loss_unknown"
    assert "stale" in stale["daily_loss"]["reason"]

    write_json(root / "live_reconciliation" / "current.json", [_reconciliation(error="URLError: timed out", realized=0.0)])
    fetch_error = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket=_ticket(),
        symbol="XAUUSDT",
        side="BUY",
        requested_price=4500.0,
        quantity=0.002,
        source="binance_usdm:testnet",
    )

    assert fetch_error["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"
    assert fetch_error["primary_blocker"]["code"] == "daily_loss_unknown"
    assert "fetch error" in fetch_error["daily_loss"]["reason"]


def test_live_money_guardrails_requires_account_history_and_real_balance_observed(tmp_path: Path):
    root = tmp_path / "outputs"

    missing_history = _evaluate(root, _reconciliation(account_observed=False, balance_present=True, realized=0.0))
    assert missing_history["allows_new_order"] is False
    assert missing_history["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"
    assert missing_history["primary_blocker"]["code"] == "daily_loss_unknown"
    assert "account history was not observed" in missing_history["daily_loss"]["reason"]

    missing_balance = _evaluate(root, _reconciliation(balance=0.0, account_observed=True, balance_present=False, realized=-50.0))
    assert missing_balance["allows_new_order"] is False
    assert missing_balance["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"
    assert missing_balance["primary_blocker"]["code"] == "daily_loss_unknown"
    assert "exchange balance was not observed" in missing_balance["daily_loss"]["reason"]


def test_live_money_guardrails_fail_closed_for_tiger_reconciliation_without_accounting(tmp_path: Path):
    root = tmp_path / "outputs"
    tiger_reconciliation = {
        "run_date": _RUN_DATE,
        "checked_at": _checked_at(),
        "provider": "tiger_openapi",
        "mode": "paper",
        "confirmation_status": "confirmed_flat",
        "can_open_new_orders": True,
        "exchange_positions": [],
        "exchange_open_orders": [],
        "account_observation": {
            "account_observed": True,
            "positions_observed": True,
            "open_orders_observed": True,
            "reason": "Tiger paper positions/open orders observed",
        },
    }

    result = LiveMoneyGuardrails(root, broker_config={"provider": "tiger_openapi", "request_dir": "tiger_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket={"ticket_id": "ticket_tiger_guard", "asset": "MGC2608", "action": "prepare_buy"},
        symbol="MGC2608",
        side="BUY",
        requested_price=4186.0,
        quantity=1,
        source="tiger_openapi:paper",
        reconciliation=tiger_reconciliation,
    )

    assert result["allows_new_order"] is False
    assert result["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"
    assert result["primary_blocker"]["code"] == "daily_loss_unknown"
    assert "exchange balance was not observed" in result["daily_loss"]["reason"]


def test_live_money_guardrails_can_use_tiger_account_sync_accounting(tmp_path: Path):
    root = tmp_path / "outputs"
    tiger_reconciliation = {
        "run_date": _RUN_DATE,
        "checked_at": _checked_at(),
        "provider": "tiger_openapi",
        "mode": "paper",
        "confirmation_status": "confirmed_flat",
        "can_open_new_orders": True,
        "exchange_positions": [],
        "exchange_open_orders": [],
        "account_observation": {
            "account_observed": True,
            "balance_present": True,
            "accounting_observed": True,
            "accounting_source": "tiger_openapi.get_prime_assets",
        },
        "exchange_balance": {"asset": "USD", "balance": 25000.0, "available": 24000.0, "balance_present": True},
        "exchange_accounting": {
            "net_realized_pnl_estimate": 0.0,
            "unrealized_pnl_estimate": 0.0,
            "utc_trading_day": {"run_date": _RUN_DATE, "start_time_ms": _DAY_START_MS, "end_time_ms": _DAY_END_MS},
        },
    }

    result = LiveMoneyGuardrails(root, broker_config={"provider": "tiger_openapi", "request_dir": "tiger_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket={"ticket_id": "ticket_tiger_guard", "asset": "MGC2608", "action": "prepare_buy"},
        symbol="MGC2608",
        side="BUY",
        requested_price=5.0,
        quantity=1,
        source="tiger_openapi:paper",
        reconciliation=tiger_reconciliation,
    )

    assert result["allows_new_order"] is True
    assert result["status"] == "READY"
    assert result["daily_loss"]["known"] is True
    assert result["daily_loss"]["reference_equity"] == 25000.0


def test_live_money_guardrails_allows_or_blocks_only_with_fresh_same_utc_day_finite_reconciliation(tmp_path: Path):
    root = tmp_path / "outputs"

    allowed = _evaluate(root, _reconciliation(balance=1000, realized=-1.0), price=4500, quantity=0.002)
    assert allowed["status"] == "READY"
    assert allowed["daily_loss"]["known"] is True
    assert allowed["daily_loss"]["utc_trading_day"]["run_date"] == _RUN_DATE

    blocked = _evaluate(root, _reconciliation(balance=1000, realized=-13.0), price=4500, quantity=0.002)
    assert blocked["status"] == "BLOCKED_DAILY_LOSS_LIMIT"
    assert blocked["daily_loss"]["loss_pct"] == 1.3


def test_live_money_guardrails_block_single_order_total_notional_and_daily_trade_limit(tmp_path: Path):
    root = tmp_path / "outputs"
    write_json(
        root / "testnet_order_requests" / "2026-06-09.json",
        [
            {"request": {"entry": {"newClientOrderId": "entry_1"}}, "receipt": {"status": "filled"}},
            {"request": {"entry": {"newClientOrderId": "entry_2"}}, "receipt": {"status": "filled"}},
        ],
    )

    result = _evaluate(root, _reconciliation(balance=1000, realized=0, position_amt=0.004, entry_price=4500), price=4500, quantity=0.01)

    assert result["allows_new_order"] is False
    codes = [item["code"] for item in result["blockers"]]
    assert "single_order_notional_limit" in codes
    assert "total_notional_limit" in codes
    assert "daily_trade_limit" in codes


def test_live_money_guardrails_persistent_halt_blocks_until_manual_clear(tmp_path: Path):
    root = tmp_path / "outputs"
    halt = LiveHaltStore(root)
    halt.activate("2026-06-09", reason="operator kill switch", source="test")

    blocked = _evaluate(root, _reconciliation())
    assert blocked["status"] == "BLOCKED_OPERATOR_HALT"
    assert blocked["allows_new_order"] is False

    halt.clear("2026-06-09", reason="manual verification complete", source="test")
    allowed = _evaluate(root, _reconciliation())
    assert allowed["status"] == "READY"
    assert allowed["allows_new_order"] is True
