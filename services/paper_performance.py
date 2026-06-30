from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import load_json, write_json


class PaperPerformanceAnalyzer:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def build(self, run_date: str) -> dict:
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        closed_today = load_json(self.output_root / "paper_trades" / "closed" / f"{run_date}.json")
        all_closed = self._all_closed_trades()
        positions = self._load_mapping(self.output_root / "paper_positions" / "current.json")
        latest_prices = {symbol: self._latest_close(run_date, symbol) for symbol in {item.get("symbol", "GOLD") for item in open_trades}}
        open_metrics = [self._open_trade_metrics(trade, latest_prices.get(trade.get("symbol", "GOLD")), positions.get(trade.get("symbol", "GOLD"), {})) for trade in open_trades]
        closed_today_metrics = [self._closed_trade_metrics(trade) for trade in closed_today]
        all_closed_metrics = [self._closed_trade_metrics(trade) for trade in all_closed]
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "summary": self._summary(open_metrics, closed_today_metrics, all_closed_metrics, positions),
            "open_trades": open_metrics,
            "closed_today": closed_today_metrics,
            "closed_all": self._closed_summary(all_closed_metrics),
            "positions": positions,
        }
        write_json(self.output_root / "performance" / f"{run_date}.json", [payload])
        write_json(self.output_root / "performance" / "current.json", [payload])
        return payload

    def _summary(self, open_metrics: list[dict], closed_today: list[dict], all_closed: list[dict], positions: dict) -> dict:
        realized_today = round(sum(float(item.get("realized_pnl", 0)) for item in closed_today), 4)
        realized_all = round(sum(float(item.get("realized_pnl", 0)) for item in all_closed), 4)
        unrealized = round(sum(float(item.get("unrealized_pnl", 0)) for item in positions.values()), 4)
        open_costs = round(sum(float(item.get("open_cost", 0)) for item in open_metrics), 4)
        closed_costs = round(sum(float(item.get("total_cost", 0)) for item in all_closed), 4)
        open_risk = round(sum(float(item.get("risk_amount", 0)) for item in open_metrics), 4)
        open_r = round(sum(float(item.get("unrealized_r", 0)) for item in open_metrics), 4)
        wins = sum(1 for item in all_closed if float(item.get("realized_pnl", 0)) > 0)
        losses = sum(1 for item in all_closed if float(item.get("realized_pnl", 0)) < 0)
        closed_count = len(all_closed)
        avg_r = round(sum(float(item.get("realized_r", 0)) for item in all_closed) / closed_count, 4) if closed_count else 0.0
        gross_profit = sum(float(item.get("realized_pnl", 0)) for item in all_closed if float(item.get("realized_pnl", 0)) > 0)
        gross_loss = abs(sum(float(item.get("realized_pnl", 0)) for item in all_closed if float(item.get("realized_pnl", 0)) < 0))
        profit_factor = round(gross_profit / gross_loss, 4) if gross_loss else (round(gross_profit, 4) if gross_profit else 0.0)
        return {
            "open_trade_count": len(open_metrics),
            "closed_today_count": len(closed_today),
            "closed_all_count": closed_count,
            "open_by_signal_regime": self._count_by(open_metrics, "signal_regime", "unknown"),
            "closed_by_signal_regime": self._count_by(all_closed, "signal_regime", "unknown"),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / closed_count, 4) if closed_count else 0.0,
            "realized_pnl_today": realized_today,
            "realized_pnl_all": realized_all,
            "unrealized_pnl": unrealized,
            "net_pnl_marked": round(realized_all + unrealized, 4),
            "open_costs": open_costs,
            "closed_costs": closed_costs,
            "total_execution_costs": round(open_costs + closed_costs, 4),
            "open_risk_amount": open_risk,
            "open_unrealized_r": open_r,
            "avg_realized_r": avg_r,
            "expectancy_r": avg_r,
            "profit_factor": profit_factor,
        }

    def _closed_summary(self, closed_metrics: list[dict]) -> dict:
        by_reason: dict[str, int] = {}
        by_regime: dict[str, int] = {}
        for item in closed_metrics:
            reason = str(item.get("exit_reason", "unknown"))
            by_reason[reason] = by_reason.get(reason, 0) + 1
            regime = str(item.get("signal_regime", "unknown"))
            by_regime[regime] = by_regime.get(regime, 0) + 1
        return {"by_exit_reason": by_reason, "by_signal_regime": by_regime}

    def _count_by(self, rows: list[dict], field: str, default: str) -> dict:
        counts: dict[str, int] = {}
        for item in rows:
            key = str(item.get(field) or default)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _open_trade_metrics(self, trade: dict, latest_price: float | None, position: dict) -> dict:
        side = str(trade.get("side", "long"))
        direction = 1 if side == "long" else -1
        entry = float(trade.get("entry_price", 0))
        stop = float(trade.get("stop_loss", entry))
        quantity = float(trade.get("quantity", 0))
        latest = float(latest_price if latest_price is not None else position.get("last_price", entry))
        risk_amount = abs(entry - stop) * quantity
        gross_unrealized_pnl = (latest - entry) * quantity * direction
        open_cost = float(trade.get("entry_total_cost", 0) or position.get("total_costs", 0) or 0)
        unrealized_pnl = gross_unrealized_pnl - open_cost
        return {
            **trade,
            "latest_price": round(latest, 4),
            "risk_amount": round(risk_amount, 4),
            "gross_unrealized_pnl": round(gross_unrealized_pnl, 4),
            "open_cost": round(open_cost, 4),
            "unrealized_pnl": round(unrealized_pnl, 4),
            "unrealized_r": round(unrealized_pnl / risk_amount, 4) if risk_amount else 0.0,
            "distance_to_stop_pct": round(((latest - stop) * direction / latest) * 100, 4) if latest else 0.0,
            "distance_to_target_pct": round(((float(trade.get("target", latest)) - latest) * direction / latest) * 100, 4) if latest else 0.0,
        }

    def _closed_trade_metrics(self, trade: dict) -> dict:
        side = str(trade.get("side", "long"))
        direction = 1 if side == "long" else -1
        entry = float(trade.get("entry_price", 0))
        stop = float(trade.get("stop_loss", entry))
        quantity = float(trade.get("quantity", 0))
        realized = float(trade.get("realized_pnl", 0))
        risk_amount = abs(entry - stop) * quantity
        return {
            **trade,
            "risk_amount": round(risk_amount, 4),
            "realized_r": round(realized / risk_amount, 4) if risk_amount else 0.0,
            "total_cost": round(float(trade.get("total_cost", 0) or 0), 4),
            "direction": direction,
        }

    def _latest_close(self, run_date: str, symbol: str) -> float | None:
        rows = load_json(self.output_root / "clean_bars" / run_date / f"{symbol}_5m.json")
        if not rows:
            return None
        return float(rows[-1].get("close", 0))

    def _all_closed_trades(self) -> list[dict]:
        root = self.output_root / "paper_trades" / "closed"
        if not root.exists():
            return []
        trades = []
        seen = set()
        for path in sorted(root.glob("*.json")):
            for item in load_json(path):
                key = item.get("trade_id") or item.get("order_id") or repr(item)
                if key in seen:
                    continue
                seen.add(key)
                trades.append(item)
        return trades

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_text(encoding="utf-8"))
