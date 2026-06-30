from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import load_json, write_json


class PaperExitMonitor:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def run(self, run_date: str) -> dict:
        trades = [item for item in load_json(self.output_root / "paper_trades" / "current.json") if item.get("status", "open") == "open"]
        rows = [self._monitor_trade(run_date, trade) for trade in trades]
        touched = [item for item in rows if item.get("stop_touched") or item.get("target_touched")]
        nearest_stop = min((item.get("distance_to_stop_pct", 999999) for item in rows), default=None)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "alert" if touched else ("pass" if rows else "empty"),
            "summary": {
                "open_trades": len(rows),
                "stop_touched": sum(1 for item in rows if item.get("stop_touched")),
                "target_touched": sum(1 for item in rows if item.get("target_touched")),
                "nearest_stop_distance_pct": round(nearest_stop, 4) if nearest_stop is not None else None,
            },
            "monitors": rows,
        }
        write_json(self.output_root / "paper_exit_monitor" / f"{run_date}.json", [payload])
        write_json(self.output_root / "paper_exit_monitor" / "current.json", [payload])
        return payload

    def _monitor_trade(self, run_date: str, trade: dict) -> dict:
        latest = self._latest_bar(run_date, str(trade.get("symbol", "GOLD")))
        side = str(trade.get("side", "long"))
        direction = 1 if side == "long" else -1
        entry = float(trade.get("entry_price", 0) or 0)
        stop = float(trade.get("stop_loss", entry) or entry)
        target = float(trade.get("target", entry) or entry)
        close = float(latest.get("close", entry) or entry)
        high = float(latest.get("high", close) or close)
        low = float(latest.get("low", close) or close)
        if side == "long":
            stop_touched = low <= stop
            target_touched = high >= target
            distance_to_stop_pct = ((close - stop) / close) * 100 if close else 0
            distance_to_target_pct = ((target - close) / close) * 100 if close else 0
        else:
            stop_touched = high >= stop
            target_touched = low <= target
            distance_to_stop_pct = ((stop - close) / close) * 100 if close else 0
            distance_to_target_pct = ((close - target) / close) * 100 if close else 0
        unrealized = (close - entry) * float(trade.get("quantity", 0) or 0) * direction
        return {
            "trade_id": trade.get("trade_id", ""),
            "ticket_id": trade.get("ticket_id", ""),
            "symbol": trade.get("symbol", "GOLD"),
            "side": side,
            "entry_price": entry,
            "latest_price": round(close, 4),
            "latest_timestamp": latest.get("timestamp", ""),
            "stop_loss": stop,
            "target": target,
            "stop_touched": stop_touched,
            "target_touched": target_touched,
            "distance_to_stop_pct": round(distance_to_stop_pct, 4),
            "distance_to_target_pct": round(distance_to_target_pct, 4),
            "unrealized_pnl": round(unrealized, 4),
        }

    def _latest_bar(self, run_date: str, symbol: str) -> dict:
        rows = load_json(self.output_root / "clean_bars" / run_date / f"{symbol}_5m.json")
        return rows[-1] if rows else {}
