from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.journal_store import write_json


EDGE_JUDGMENT_VERSION = "edge-judgment-v1"


class EdgeJudgment:
    """M3 closed-trade-only edge judgment.

    Open PnL is reported for context but never used for winner/loser labels or
    ranking score. Thin samples are explicitly non-rankable.
    """

    def __init__(
        self,
        output_root: Path,
        *,
        strategy_id: str,
        min_closed_trades: int = 20,
    ) -> None:
        self.output_root = Path(output_root)
        self.strategy_id = strategy_id or self.output_root.name
        self.min_closed_trades = int(min_closed_trades or 20)
        self.read_errors: list[dict] = []

    def build(self, run_date: str, persist: bool = True) -> dict:
        closed = self._closed_all()
        open_trades = self._list(self.output_root / "paper_trades" / "current.json")
        performance = self._latest_mapping(self.output_root / "performance" / "current.json")
        equity = self._latest_mapping(self.output_root / "equity_curve" / "current.json")
        metrics = self._metrics(closed, open_trades, performance, equity)
        label = self._label(metrics)
        payload = {
            "schema_version": EDGE_JUDGMENT_VERSION,
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "strategy_id": self.strategy_id,
            "label": label,
            "rankable": label in {"winner", "loser", "neutral"},
            "ranking_basis": "closed_trades_only",
            "ranking_score": metrics["avg_realized_r"] if label in {"winner", "loser", "neutral"} else None,
            "metrics": metrics,
            "audit": {
                "status": "pass" if not self.read_errors else "fail",
                "read_errors": self.read_errors,
                "open_pnl_used_for_label": False,
                "open_pnl_used_for_ranking": False,
                "red_line": "thin samples or open/unrealized PnL must not masquerade as edge",
            },
        }
        if persist:
            write_json(self.output_root / "edge_judgment" / "current.json", [payload])
            write_json(self.output_root / "edge_judgment" / f"{run_date}.json", [payload])
        return payload

    def _metrics(self, closed: list[dict], open_trades: list[dict], performance: dict, equity: dict) -> dict:
        realized_values = [self._float(item.get("realized_pnl"), 0.0) for item in closed]
        wins = sum(1 for value in realized_values if value > 0)
        losses = sum(1 for value in realized_values if value < 0)
        gross_profit = sum(value for value in realized_values if value > 0)
        gross_loss = abs(sum(value for value in realized_values if value < 0))
        r_values = [self._r_value(item) for item in closed]
        r_values = [value for value in r_values if value is not None]
        closed_count = len(closed)
        summary = performance.get("summary", {}) if isinstance(performance, dict) else {}
        open_unrealized = self._float(summary.get("unrealized_pnl"), 0.0)
        return {
            "closed_trade_count": closed_count,
            "min_closed_trades": self.min_closed_trades,
            "sample_gap": max(0, self.min_closed_trades - closed_count),
            "open_trade_count": len(open_trades),
            "open_unrealized_pnl_context": round(open_unrealized, 4),
            "realized_pnl": round(sum(realized_values), 4),
            "wins": wins,
            "losses": losses,
            "breakeven": max(0, closed_count - wins - losses),
            "win_rate": round(wins / closed_count, 4) if closed_count else 0.0,
            "avg_realized_r": round(sum(r_values) / len(r_values), 4) if r_values else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (round(gross_profit, 4) if gross_profit else 0.0),
            "max_drawdown_pct": self._float(equity.get("max_drawdown_pct"), 0.0),
            "closed_trade_ids": [str(item.get("trade_id", "")) for item in closed[:20]],
        }

    def _label(self, metrics: dict) -> str:
        if metrics["closed_trade_count"] < self.min_closed_trades:
            return "insufficient_sample"
        realized = float(metrics.get("realized_pnl", 0.0) or 0.0)
        avg_r = float(metrics.get("avg_realized_r", 0.0) or 0.0)
        pf = float(metrics.get("profit_factor", 0.0) or 0.0)
        if realized > 0 and avg_r > 0 and pf >= 1.15:
            return "winner"
        if realized < 0 and avg_r < 0 and pf < 1.0:
            return "loser"
        return "neutral"

    def _r_value(self, trade: dict) -> float | None:
        if trade.get("realized_r") is not None:
            return self._float(trade.get("realized_r"), 0.0)
        entry = self._float(trade.get("entry_price"), 0.0)
        stop = self._float(trade.get("stop_loss"), entry)
        quantity = self._float(trade.get("quantity"), 0.0)
        risk = abs(entry - stop) * quantity
        if risk <= 0:
            return None
        return self._float(trade.get("realized_pnl"), 0.0) / risk

    def _closed_all(self) -> list[dict]:
        root = self.output_root / "paper_trades" / "closed"
        if not root.exists():
            return []
        rows: list[dict] = []
        seen: set[str] = set()
        for path in sorted(root.glob("*.json")):
            for item in self._list(path):
                key = str(item.get("trade_id") or item.get("order_id") or repr(item))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(item)
        return rows

    def _latest_mapping(self, path: Path) -> dict:
        data = self._read(path)
        if isinstance(data, list):
            item = data[-1] if data else {}
            return item if isinstance(item, dict) else {}
        return data if isinstance(data, dict) else {}

    def _list(self, path: Path) -> list[dict]:
        data = self._read(path)
        return data if isinstance(data, list) else []

    def _read(self, path: Path) -> Any:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.read_errors.append({"path": str(path), "error": str(exc)})
            return None

    def _float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)
