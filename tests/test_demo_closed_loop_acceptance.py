import json
from pathlib import Path

import pytest

from services.journal_store import load_json, write_json
from services.live_reconciliation import LiveBrokerReconciliation
from services.paper_executor import PaperExecutor
from services.strategy_daily_review import StrategyDailyReview


class _FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _flat_exchange_opener(request, timeout):
    url = request.full_url
    if "/fapi/v2/positionRisk" in url:
        return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
    if "/fapi/v2/balance" in url:
        return _FakeResponse([{"asset": "USDT", "balance": "100.0", "availableBalance": "95.0"}])
    if "/fapi/v1/openOrders" in url:
        return _FakeResponse([])
    if "/fapi/v1/userTrades" in url:
        return _FakeResponse([])
    if "/fapi/v1/income" in url:
        return _FakeResponse([])
    raise AssertionError(url)


def test_a0_clean_signal_runs_to_reconciled_and_daily_review_consumes_trade(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    base = tmp_path / "outputs"
    namespace = base / "strategies" / "alpha"
    run_date = "2026-06-30"
    ticket = {
        "ticket_id": "ticket_alpha_a0",
        "signal_id": "sig_alpha_a0",
        "asset": "GOLD",
        "action": "prepare_buy",
        "entry_zone": "2800-2820",
        "stop_loss": 2750,
        "targets": [2920],
        "position_size_pct": 8,
        "max_loss_pct": 0.5,
        "order_type": "market",
        "time_in_force": "day",
    }
    write_json(namespace / "signals" / f"{run_date}.json", [{"signal_id": "sig_alpha_a0", "direction": "long"}])
    write_json(namespace / "trade_tickets" / f"{run_date}.json", [ticket])
    write_json(
        namespace / "clean_bars" / run_date / "GOLD_5m.json",
        [{"timestamp": "2026-06-30T10:00:00+00:00", "high": 2930, "low": 2805, "close": 2925}],
    )

    executor = PaperExecutor(namespace)
    order = executor.execute_ticket(run_date, ticket, latest_price=2810)
    position_before_close = load_json(namespace / "paper_positions" / "current.json")["GOLD"]
    closed = executor.evaluate_exits(run_date)

    assert len(closed) == 1
    closed_trade = closed[0]
    assert closed_trade["order_id"] == order.order_id
    assert closed_trade["exit_reason"] == "target"
    assert order.quantity == pytest.approx(position_before_close["quantity"])
    assert order.quantity == pytest.approx(closed_trade["quantity"])
    expected_gross = round((float(closed_trade["exit_price"]) - float(order.fill_price)) * float(order.quantity), 4)
    assert closed_trade["gross_realized_pnl"] == pytest.approx(expected_gross)
    assert closed_trade["realized_pnl"] == pytest.approx(round(float(closed_trade["gross_realized_pnl"]) - float(closed_trade["total_cost"]), 4))

    report = LiveBrokerReconciliation(
        namespace,
        {
            "provider": "binance_usdm",
            "environment": "testnet",
            "base_url": "https://testnet.binancefuture.com",
            "api_key_env": "BINANCE_API_KEY",
            "api_secret_env": "BINANCE_API_SECRET",
            "instrument_map": {"GOLD": "XAUUSDT"},
        },
        opener=_flat_exchange_opener,
    ).run(run_date)

    assert report["reconciled"] is True
    lifecycle = load_json(namespace / "order_lifecycle" / f"{run_date}.json")[0]
    assert lifecycle["order_id"] == order.order_id
    assert lifecycle["state"] == "reconciled"
    assert [item["to"] for item in lifecycle["transitions"]] == [
        "entry",
        "submitting",
        "accepted",
        "filled",
        "protective_attached",
        "closed",
        "reconciled",
    ]

    reviewer = StrategyDailyReview(base)
    reviewer.strategy_config = {
        "alpha": {
            "timeframe": "5m",
            "classification": {"family": "acceptance", "style": "a0", "expected_trades_per_day_min": 1, "expected_trades_per_day_max": 1},
        }
    }
    review = reviewer.build(
        run_date,
        leaderboard={"strategies": [{"strategy_id": "alpha", "daily_execution": {"executed_trade_count": 1}, "closed_trades": 1}]},
        frequency={"strategies": []},
    )
    row = review["strategies"][0]
    assert row["pnl"]["realized_today"] == pytest.approx(closed_trade["realized_pnl"])
    assert row["tp_sl"]["take_profit_hits"] == 1
    assert "take_profit_hit" in row["attribution"]["reasons"]
    assert row["replay_context"]["review_trade"]["trade_id"] == closed_trade["trade_id"]
