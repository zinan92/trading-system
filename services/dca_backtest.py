"""定投回测 (#1335): buy a fixed USDT amount every N hours, over the same windows the grid uses.

Deterministic and replay-free: a DCA has no order lifecycle to replay. Each buy is a market buy
at the close of the first 1-minute bar at or after its scheduled time, paying Paper's assumed
taker fee (``paper_fee_model.taker_fee_rate``, the same assumption the grid backtest states).
The position is valued at the window's last close. A long perpetual pays funding when the rate is
positive; funding is counted only for windows that start after the venue's funding history does,
exactly as the grid backtest counts it.

Nothing here reaches a ledger, a venue or a gate; the numbers are history shown on a page.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
import statistics
from typing import Any

from services.grid_backtest import STEP, BacktestUnavailable, Bar, windows

SCHEMA = "dca-backtest-v1"
AMOUNT_RANGE = (10.0, 100_000.0)
INTERVAL_RANGE = (1, 168)


def validate(params: Mapping[str, Any]) -> dict[str, Any]:
    """Only the fields a DCA has, inside sane bounds. Raises ValueError with the field named."""

    try:
        amount = float(params["amount_usdt"])
        interval = int(params["interval_hours"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("定投参数要有每次金额 amount_usdt 和间隔 interval_hours") from None
    if not AMOUNT_RANGE[0] <= amount <= AMOUNT_RANGE[1]:
        raise ValueError(f"每次金额要在 {AMOUNT_RANGE[0]:g}–{AMOUNT_RANGE[1]:g} USDT 之间")
    if not INTERVAL_RANGE[0] <= interval <= INTERVAL_RANGE[1] or float(params["interval_hours"]) != interval:
        raise ValueError(f"间隔要是 {INTERVAL_RANGE[0]}–{INTERVAL_RANGE[1]} 之间的整数小时")
    return {"amount_usdt": amount, "interval_hours": interval}


def buys(params: Mapping[str, Any], bars: Sequence[Bar], *, fee_rate: float) -> list[dict[str, Any]]:
    """Each scheduled buy: due at a bar's open, filled at that bar's close, so stamped when the bar ended."""

    amount, every = float(params["amount_usdt"]), timedelta(hours=int(params["interval_hours"]))
    due = bars[0].opened_at if bars else None
    out: list[dict[str, Any]] = []
    for bar in bars:
        if bar.opened_at >= due:
            fee = amount * fee_rate
            out.append({"at": (bar.opened_at + timedelta(minutes=1)).isoformat(), "bar": bar.opened_at.isoformat(),
                        "price": bar.close, "quantity": (amount - fee) / bar.close, "amount": amount, "fee": fee})
            while due <= bar.opened_at:
                due += every
    return out


def run_window(
    params: Mapping[str, Any],
    bars: Sequence[Bar],
    *,
    fee_rate: float,
    funding: Sequence[Mapping[str, Any]] = (),
    funding_from: str | None = None,
) -> dict[str, Any]:
    started = bars[0].opened_at
    bought = buys(params, bars, fee_rate=fee_rate)
    by_bar = {row["bar"]: row for row in bought}
    quantity = invested = fees = 0.0
    held: list[tuple[datetime, float]] = []  # (fill time, quantity held after it), one row per buy
    worst = 0.0  # lowest (value − invested) / invested seen at any minute, in percent
    for bar in bars:
        row = by_bar.get(bar.opened_at.isoformat())
        if row:
            quantity += row["quantity"]
            invested += row["amount"]
            fees += row["fee"]
            held.append((datetime.fromisoformat(row["at"]), quantity))
        if invested:
            worst = min(worst, (quantity * bar.close - invested) / invested * 100)
    last = bars[-1]
    window_end = last.opened_at.isoformat()
    prices = {bar.opened_at.isoformat(): bar.close for bar in bars}

    def held_at(moment: str) -> float:
        # Strictly before: a settlement at the very moment a buy fills does not count that buy (#1369).
        at = datetime.fromisoformat(moment)
        return next((qty for filled, qty in reversed(held) if filled < at), 0.0)

    settlements = [row for row in funding if started.isoformat() <= str(row["funding_time"]) <= window_end and str(row["funding_time"]) in prices]
    paid = sum(
        held_at(str(row["funding_time"])) * prices[str(row["funding_time"])]
        * float(row["realized_rate"] if row.get("realized_rate") is not None else row["funding_rate"])
        for row in settlements
    )
    funded = [str(row["funding_time"]) for row in settlements if held_at(str(row["funding_time"]))]
    funding_complete = funding_from is not None and started.isoformat() >= funding_from
    value = quantity * last.close
    net = value - invested - (paid if funding_complete else 0.0)
    return {
        "started_at": started.isoformat(),
        "ended_at": window_end,
        "buys": len(held),
        "invested": round(invested, 4),
        "fees": round(fees, 4),
        "funding": round(paid, 4) if funding_complete else None,
        "value": round(value, 4),
        "net": round(net, 4),
        "return_pct": round(net / invested * 100, 4) if invested else 0.0,
        "average_cost": round((invested - fees) / quantity, 6) if quantity else None,
        "max_drawdown_pct": round(worst, 4),
        # What the future-data checks read (#1369).
        "last_bar_at": window_end,
        "closed_at": (last.opened_at + timedelta(minutes=1)).isoformat(),
        "first_fill_at": bought[0]["at"] if bought else None,
        "last_fill_at": bought[-1]["at"] if bought else None,
        "fill_times_complete": True,
        "funding_first_at": min(str(row["funding_time"]) for row in settlements) if settlements else None,
        "funding_last_at": max(str(row["funding_time"]) for row in settlements) if settlements else None,
        "first_funded_at": min(funded) if funded else None,
    }


def summarize(results: Sequence[Mapping[str, Any]], *, days: float) -> dict[str, Any]:
    returns = sorted(float(row["return_pct"]) for row in results)
    if not returns:
        raise BacktestUnavailable("没有可用的窗口")
    width = max((returns[-1] - returns[0]) / 12, 1e-9)
    counts = [0] * 12
    for value in returns:
        counts[min(11, int((value - returns[0]) / width))] += 1
    drawdowns = [float(row["max_drawdown_pct"]) for row in results]
    return {
        "runs": len(results),
        "independent_windows": int(days // 7),
        "median_return_pct": round(statistics.median(returns), 2),
        "worst_return_pct": round(returns[0], 2),
        "best_return_pct": round(returns[-1], 2),
        "median_drawdown_pct": round(statistics.median(drawdowns), 2),
        "worst_drawdown_pct": round(min(drawdowns), 2),
        "mean_buys": round(statistics.mean(int(row["buys"]) for row in results), 1),
        "invested_per_run": round(statistics.mean(float(row["invested"]) for row in results), 2),
        "funded_runs": sum(1 for row in results if row["funding"] is not None),
        "histogram": {"low": round(returns[0], 2), "width": round(width, 4), "counts": counts},
    }


def run_backtest(
    params: Mapping[str, Any],
    bars: Sequence[Bar],
    *,
    fee_rate: float,
    funding: Sequence[Mapping[str, Any]] = (),
    funding_from: str | None = None,
    step: timedelta = STEP,
) -> dict[str, Any]:
    checked = validate(params)
    spans = windows(bars, step=step)
    if not spans:
        raise BacktestUnavailable("历史不够一个窗口")
    results = [run_window(checked, span, fee_rate=fee_rate, funding=funding, funding_from=funding_from) for span in spans]
    days = (bars[-1].opened_at - bars[0].opened_at).total_seconds() / 86400
    return {"params": checked, "summary": summarize(results, days=days), "windows": results}
