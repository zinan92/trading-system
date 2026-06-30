"""Event-driven historical backtest for any signal engine.

The engine enumerates ALL its signals over a bar series in one pass
(`historical_signals`), then this module replays trades off them with a fixed
stop / target / timeout exit and the SAME paper-execution cost model (spread /
slippage / commission) as forward paper — so a backtest row is directly
comparable to a forward-paper row. One position at a time (no overlap): signals
arriving while a trade is open are skipped.

Backtest != forward paper: this ranks a strategy on months of history in seconds;
forward paper is the live truth. Keep the two leaderboards separate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from services.config_loader import load_risk_rules


@dataclass(frozen=True)
class BacktestConfig:
    stop_pct: float = 0.6          # percent, e.g. 0.6 == 0.6%
    target_pct: float = 1.2
    max_hold_bars: int = 24
    starting_equity: float = 10_000.0
    position_size_pct: float = 8.0

    @classmethod
    def from_strategy(cls, strategy) -> "BacktestConfig":
        backtest = strategy.params.get("backtest") or {}

        def as_pct(key: str, default: float) -> float:
            # strategy.yaml stores stop/target as fractions (0.006); this engine
            # works in percent, so scale a sub-1 fraction up to percent.
            value = float(backtest.get(key, default))
            return value * 100 if 0 < value < 1 else value

        return cls(
            stop_pct=as_pct("stop_pct", 0.6),
            target_pct=as_pct("target_pct", 1.2),
            max_hold_bars=int(backtest.get("max_hold_bars", 24)),
            starting_equity=float(getattr(strategy, "starting_equity", 10_000.0)),
        )


class StrategyBacktester:
    def __init__(self, config: BacktestConfig | None = None, cost_rules: dict | None = None) -> None:
        self.config = config or BacktestConfig()
        self.cost_rules = cost_rules if cost_rules is not None else load_risk_rules().get("default", {}).get("paper_execution_costs", {})

    def run(self, bars: list, signals: list[dict]) -> dict:
        trades = self._simulate(bars, signals)
        return self._metrics(trades)

    # ------------------------------------------------------------------ sim
    def _simulate(self, bars: list, signals: list[dict]) -> list[dict]:
        cfg = self.config
        spread_pct = float(self.cost_rules.get("spread_pct", 0) or 0)
        slippage_pct = float(self.cost_rules.get("slippage_pct", 0) or 0)
        adverse = (spread_pct / 2 + slippage_pct) / 100
        trades: list[dict] = []
        next_allowed = 0
        for sig in sorted(signals, key=lambda s: s["index"]):
            entry_idx = int(sig["index"])
            if entry_idx < next_allowed or entry_idx >= len(bars) - 1:
                continue
            long = sig["direction"] == "long"
            d = 1 if long else -1
            entry = float(bars[entry_idx].close) * (1 + d * adverse)  # adverse fill
            stop = entry * (1 - cfg.stop_pct / 100) if long else entry * (1 + cfg.stop_pct / 100)
            target = entry * (1 + cfg.target_pct / 100) if long else entry * (1 - cfg.target_pct / 100)
            quantity = (cfg.starting_equity * cfg.position_size_pct / 100) / entry if entry else 0.0

            exit_idx, exit_price, reason = None, None, ""
            last = min(entry_idx + cfg.max_hold_bars, len(bars) - 1)
            for j in range(entry_idx + 1, last + 1):
                bar = bars[j]
                if long:
                    if float(bar.low) <= stop:   # stop checked first (conservative)
                        exit_idx, exit_price, reason = j, stop, "stop"; break
                    if float(bar.high) >= target:
                        exit_idx, exit_price, reason = j, target, "target"; break
                else:
                    if float(bar.high) >= stop:
                        exit_idx, exit_price, reason = j, stop, "stop"; break
                    if float(bar.low) <= target:
                        exit_idx, exit_price, reason = j, target, "target"; break
            if exit_idx is None:
                exit_idx, exit_price, reason = last, float(bars[last].close), "timeout"

            gross = (exit_price - entry) * quantity * d
            costs = self._side_cost(entry, quantity) + self._side_cost(exit_price, quantity)
            risk = abs(entry - stop) * quantity
            trades.append({
                "direction": sig["direction"],
                "entry_index": entry_idx,
                "exit_index": exit_idx,
                "entry_price": round(entry, 4),
                "exit_price": round(exit_price, 4),
                "exit_reason": reason,
                "bars_held": exit_idx - entry_idx,
                "gross_pnl": round(gross, 4),
                "costs": round(costs, 4),
                "net_pnl": round(gross - costs, 4),
                "r_multiple": round((gross - costs) / risk, 4) if risk else 0.0,
            })
            next_allowed = exit_idx + 1
        return trades

    def _side_cost(self, price: float, quantity: float) -> float:
        spread_pct = float(self.cost_rules.get("spread_pct", 0) or 0)
        slippage_pct = float(self.cost_rules.get("slippage_pct", 0) or 0)
        spread_cost = abs(price * (spread_pct / 2) / 100 * quantity)
        slippage_cost = abs(price * slippage_pct / 100 * quantity)
        notional = abs(price * quantity)
        commission = max(
            float(self.cost_rules.get("min_commission", 0) or 0),
            float(self.cost_rules.get("commission_per_order", 0) or 0) + notional * (float(self.cost_rules.get("commission_pct_notional", 0) or 0) / 100),
        )
        return spread_cost + slippage_cost + commission

    # ------------------------------------------------------------------ metrics
    def _metrics(self, trades: list[dict]) -> dict:
        start = self.config.starting_equity
        equity = start
        peak = start
        max_dd = 0.0
        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] < 0]
        gross_profit = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        net_pnl = sum(t["net_pnl"] for t in trades)
        returns = []
        for t in trades:
            equity += t["net_pnl"]
            returns.append(t["net_pnl"] / start)
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100)
        r_values = [t["r_multiple"] for t in trades]
        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (float("inf") if gross_profit else 0.0),
            "net_pnl": round(net_pnl, 4),
            "return_pct": round((equity - start) / start * 100, 4) if start else 0.0,
            "max_drawdown_pct": round(max_dd, 4),
            "avg_r": round(sum(r_values) / len(r_values), 4) if r_values else 0.0,
            "sharpe_per_trade": round(self._sharpe(returns), 4),
            "avg_bars_held": round(sum(t["bars_held"] for t in trades) / len(trades), 2) if trades else 0.0,
            "final_equity": round(equity, 2),
        }

    def _sharpe(self, returns: list[float]) -> float:
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(variance)
        return (mean / std) if std else 0.0
