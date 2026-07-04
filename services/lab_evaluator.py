"""Deterministic Strategy Lab evaluator.

Simulation semantics are intentionally conservative:

- Historical signals are regenerated from bars; no live-runner signal history is
  required.
- A signal computed on bar ``t`` close can execute no earlier than bar
  ``t+1`` open.
- Research sizing is fixed notional per trade. Production leverage and
  ``position_size_pct`` are not used.
- Stop/target checks start on the execution bar. If both are touched in one
  bar, the stop wins.
- Missing/duplicate/non-finite bars and zero-trade results are invalid.
- Cost overrides are per-side basis points and are charged on both entry and
  exit notional.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime
from statistics import mean, pstdev

from schemas.market_data import Bar
from services.macd_signal_engine import MacdSignalEngine


@dataclass(frozen=True)
class LabSimulationConfig:
    stop_pct: float = 0.006
    target_pct: float = 0.012
    max_hold_bars: int = 24
    starting_equity: float = 10_000.0
    fixed_notional: float = 1_000.0
    cost_bp_per_side: float = 0.0
    maker_limit_entry: bool = False


def regenerate_macd_signals(bars: list[Bar], params: dict | None = None) -> list[dict]:
    engine = MacdSignalEngine(params or {"signal": {}})
    signals = []
    for item in engine.historical_signals(bars):
        index = int(item["index"])
        if 0 <= index < len(bars):
            signals.append({
                "index": index,
                "direction": str(item["direction"]),
                "timestamp": bars[index].timestamp,
                "close": float(bars[index].close),
            })
    return signals


def random_direction_signals(bars: list[Bar], every_n: int = 12, seed: int = 1) -> list[dict]:
    rng = random.Random(seed)
    out: list[dict] = []
    for index in range(5, max(5, len(bars) - 2), max(1, every_n)):
        out.append({
            "index": index,
            "direction": "long" if rng.random() >= 0.5 else "short",
            "timestamp": bars[index].timestamp,
            "close": float(bars[index].close),
        })
    return out


def validate_1m_bars(bars: list[Bar]) -> dict:
    if not bars:
        return {"valid": False, "reason": "no_bars", "gaps": [], "duplicates": 0}
    ordered = sorted(bars, key=lambda item: item.timestamp)
    seen: set[str] = set()
    duplicates = 0
    gaps: list[dict] = []
    previous_ts: datetime | None = None
    for bar in ordered:
        if bar.timestamp in seen:
            duplicates += 1
        seen.add(bar.timestamp)
        values = [bar.open, bar.high, bar.low, bar.close, bar.volume]
        if any(not math.isfinite(float(value)) for value in values):
            return {"valid": False, "reason": "non_finite_bar", "gaps": gaps, "duplicates": duplicates}
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            return {"valid": False, "reason": "non_positive_price", "gaps": gaps, "duplicates": duplicates}
        current_ts = _parse_ts(bar.timestamp)
        if previous_ts is not None:
            delta = int((current_ts - previous_ts).total_seconds())
            if delta > 90:
                gaps.append({
                    "start": previous_ts.isoformat(),
                    "end": current_ts.isoformat(),
                    "missing_minutes": max(0, delta // 60 - 1),
                })
        previous_ts = current_ts
    if duplicates:
        return {"valid": False, "reason": "duplicate_bars", "gaps": gaps, "duplicates": duplicates}
    if gaps:
        return {"valid": False, "reason": "missing_bars", "gaps": gaps, "duplicates": duplicates}
    return {"valid": True, "reason": "pass", "gaps": [], "duplicates": 0}


def evaluate_signals(bars: list[Bar], signals: list[dict], config: LabSimulationConfig | None = None) -> dict:
    cfg = config or LabSimulationConfig()
    quality = validate_1m_bars(bars)
    if not quality["valid"]:
        return _invalid_result(quality["reason"], bars, signals, quality)
    trades = simulate_trades(bars, signals, cfg)
    if not trades:
        return _invalid_result("zero_trades", bars, signals, quality)
    equity = equity_curve(trades, cfg.starting_equity)
    metrics = metrics_from_trades(trades, equity, cfg)
    if any(_bad_number(value) for value in metrics.values() if isinstance(value, (int, float))):
        return _invalid_result("non_finite_metrics", bars, signals, quality)
    return {
        "status": "valid",
        "reason": "pass",
        "bars": len(bars),
        "signals": len(signals),
        "trades": trades,
        "equity": equity,
        "metrics": metrics,
        "quality": quality,
        "config": _config_dict(cfg),
    }


def simulate_trades(bars: list[Bar], signals: list[dict], config: LabSimulationConfig) -> list[dict]:
    trades: list[dict] = []
    next_signal_index = 0
    for raw in sorted(signals, key=lambda item: int(item["index"])):
        signal_index = int(raw["index"])
        if signal_index < next_signal_index or signal_index >= len(bars) - 2:
            continue
        direction = str(raw["direction"])
        if direction not in {"long", "short"}:
            continue
        entry_index = signal_index + 1
        entry_price = _entry_price(bars, signal_index, entry_index, direction, config)
        if entry_price is None or entry_price <= 0:
            continue
        side = 1 if direction == "long" else -1
        quantity = config.fixed_notional / entry_price
        stop = entry_price * (1 - config.stop_pct) if side == 1 else entry_price * (1 + config.stop_pct)
        target = entry_price * (1 + config.target_pct) if side == 1 else entry_price * (1 - config.target_pct)
        exit_index, exit_price, reason = _exit(bars, entry_index, entry_price, stop, target, side, config.max_hold_bars)
        gross = (exit_price - entry_price) * quantity * side
        entry_cost = config.fixed_notional * config.cost_bp_per_side / 10_000
        exit_cost = abs(exit_price * quantity) * config.cost_bp_per_side / 10_000
        costs = entry_cost + exit_cost
        risk = abs(entry_price - stop) * quantity
        net = gross - costs
        trades.append({
            "direction": direction,
            "signal_index": signal_index,
            "signal_timestamp": bars[signal_index].timestamp,
            "entry_index": entry_index,
            "entry_timestamp": bars[entry_index].timestamp,
            "exit_index": exit_index,
            "exit_timestamp": bars[exit_index].timestamp,
            "entry_price": round(entry_price, 6),
            "exit_price": round(exit_price, 6),
            "exit_reason": reason,
            "quantity": round(quantity, 8),
            "fixed_notional": round(config.fixed_notional, 4),
            "gross_pnl": round(gross, 6),
            "costs": round(costs, 6),
            "net_pnl": round(net, 6),
            "r_multiple": round(net / risk, 6) if risk else 0.0,
            "bars_held": exit_index - entry_index + 1,
        })
        next_signal_index = exit_index + 1
    return trades


def metrics_from_trades(trades: list[dict], equity: list[dict], config: LabSimulationConfig) -> dict:
    net = [float(item["net_pnl"]) for item in trades]
    wins = [value for value in net if value > 0]
    losses = [value for value in net if value < 0]
    total = sum(net)
    returns = [value / config.starting_equity for value in net]
    downside = [value for value in returns if value < 0]
    sharpe = _ratio(returns, returns)
    sortino = _ratio(returns, downside)
    return {
        "trade_count": len(trades),
        "total_net_pnl": round(total, 6),
        "expectancy_per_trade": round(total / len(trades), 6),
        "expectancy_bp_on_notional": round((total / len(trades)) / config.fixed_notional * 10_000, 6),
        "win_rate": round(len(wins) / len(trades), 6),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 6) if losses else (99.0 if wins else 0.0),
        "max_drawdown_pct": round(_max_drawdown_pct(equity), 6),
        "sharpe": round(sharpe, 6),
        "sortino": round(sortino, 6),
        "turnover_notional": round(len(trades) * config.fixed_notional, 2),
        "final_equity": round(float(equity[-1]["equity"]), 6),
    }


def _entry_price(bars: list[Bar], signal_index: int, entry_index: int, direction: str, config: LabSimulationConfig) -> float | None:
    if not config.maker_limit_entry:
        return float(bars[entry_index].open)
    limit_price = float(bars[signal_index].close)
    bar = bars[entry_index]
    if direction == "long" and float(bar.low) <= limit_price:
        return limit_price
    if direction == "short" and float(bar.high) >= limit_price:
        return limit_price
    return None


def _exit(bars: list[Bar], entry_index: int, entry_price: float, stop: float, target: float, side: int, max_hold_bars: int) -> tuple[int, float, str]:
    last_index = min(len(bars) - 1, entry_index + max(1, max_hold_bars) - 1)
    for index in range(entry_index, last_index + 1):
        bar = bars[index]
        if side == 1:
            if float(bar.low) <= stop:
                return index, stop, "stop"
            if float(bar.high) >= target:
                return index, target, "target"
        else:
            if float(bar.high) >= stop:
                return index, stop, "stop"
            if float(bar.low) <= target:
                return index, target, "target"
    return last_index, float(bars[last_index].close), "timeout"


def equity_curve(trades: list[dict], starting_equity: float) -> list[dict]:
    equity = starting_equity
    points = [{"index": -1, "timestamp": "", "equity": round(equity, 6)}]
    for idx, trade in enumerate(trades):
        equity += float(trade["net_pnl"])
        points.append({"index": idx, "timestamp": trade["exit_timestamp"], "equity": round(equity, 6)})
    return points


def _ratio(returns: list[float], risk_values: list[float]) -> float:
    if len(returns) < 2 or len(risk_values) < 2:
        return 0.0
    risk = pstdev(risk_values)
    return mean(returns) / risk if risk else 0.0


def _max_drawdown_pct(equity: list[dict]) -> float:
    peak = float(equity[0]["equity"]) if equity else 0.0
    max_dd = 0.0
    for point in equity:
        value = float(point["equity"])
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100)
    return max_dd


def _invalid_result(reason: str, bars: list[Bar], signals: list[dict], quality: dict) -> dict:
    return {"status": "invalid", "reason": reason, "bars": len(bars), "signals": len(signals), "trades": [], "equity": [], "metrics": {}, "quality": quality}


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _bad_number(value: float) -> bool:
    return not math.isfinite(float(value))


def _config_dict(config: LabSimulationConfig) -> dict:
    return {
        "stop_pct": config.stop_pct,
        "target_pct": config.target_pct,
        "max_hold_bars": config.max_hold_bars,
        "starting_equity": config.starting_equity,
        "fixed_notional": config.fixed_notional,
        "cost_bp_per_side": config.cost_bp_per_side,
        "maker_limit_entry": config.maker_limit_entry,
    }
