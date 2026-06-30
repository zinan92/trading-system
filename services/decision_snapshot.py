from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from schemas.signal import Signal
from schemas.trade_ticket import TradeTicket
from services.journal_store import load_json, write_json


class DecisionSnapshotBuilder:
    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)

    def record(
        self,
        run_date: str,
        strategy_id: str,
        timeframe: str,
        candles: list,
        signal: Signal,
        direction_bias: dict | None = None,
        position_gate: dict | None = None,
        ticket: TradeTicket | None = None,
        final_decision: str = "no_go",
        no_go_reason: str = "",
        risk_block: dict | None = None,
    ) -> dict:
        latest = candles[-1] if candles else None
        indicators = self._indicators(candles)
        ticket_dict = ticket.to_dict() if ticket else {}
        snapshot = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "strategy_id": strategy_id,
            "asset": signal.asset,
            "timeframe": timeframe,
            "signal_id": signal.signal_id,
            "bar_timestamp": getattr(latest, "timestamp", ""),
            "price": float(getattr(latest, "close", 0.0) or 0.0),
            "signal": {
                "direction": signal.direction,
                "strength": signal.strength,
                "confidence": signal.confidence,
                "regime": signal.regime,
                "thesis": signal.thesis,
            },
            "direction_bias": direction_bias or {},
            "position_gate": position_gate or {},
            "indicators": indicators,
            "execution_plan": {
                "ticket_id": ticket_dict.get("ticket_id", ""),
                "action": ticket_dict.get("action", ""),
                "entry_zone": ticket_dict.get("entry_zone", ""),
                "take_profit": (ticket_dict.get("targets") or [None])[0],
                "stop_loss": ticket_dict.get("stop_loss"),
                "risk_reward": (ticket_dict.get("trade_quality") or {}).get("reward_to_risk"),
                "target_equity_return_pct": (ticket_dict.get("trade_quality") or {}).get("target_equity_return_pct"),
            },
            "final_decision": final_decision,
            "no_go_reason": no_go_reason,
            "risk_block": risk_block or {},
            "as_of_contract": {
                "uses_bars_through": getattr(latest, "timestamp", ""),
                "no_future_bars": True,
            },
        }
        self._record(run_date, snapshot)
        return snapshot

    def _record(self, run_date: str, snapshot: dict) -> None:
        path = self.output_root / "decision_snapshots" / f"{run_date}.json"
        rows = [item for item in load_json(path) if item.get("signal_id") != snapshot.get("signal_id")]
        rows.append(snapshot)
        write_json(path, rows)
        write_json(self.output_root / "decision_snapshots" / "current.json", [snapshot])

    def _indicators(self, candles: list) -> dict:
        closes = [float(getattr(item, "close", 0.0) or 0.0) for item in candles]
        highs = [float(getattr(item, "high", 0.0) or 0.0) for item in candles]
        lows = [float(getattr(item, "low", 0.0) or 0.0) for item in candles]
        return {
            "ema50": self._ema(closes, 50),
            "rsi14": self._rsi(closes, 14),
            "macd": self._macd(closes),
            "atr14": self._atr(highs, lows, closes, 14),
            "bar_count": len(candles),
        }

    def _ema(self, values: list[float], period: int) -> float | None:
        if not values:
            return None
        alpha = 2 / (period + 1)
        ema = values[0]
        for value in values[1:]:
            ema = alpha * value + (1 - alpha) * ema
        return round(ema, 4)

    def _rsi(self, closes: list[float], period: int) -> float | None:
        if len(closes) <= period:
            return None
        gains = []
        losses = []
        for prev, cur in zip(closes[-period - 1 : -1], closes[-period:]):
            delta = cur - prev
            gains.append(max(delta, 0.0))
            losses.append(abs(min(delta, 0.0)))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 4)

    def _macd(self, closes: list[float]) -> dict:
        if not closes:
            return {"line": None, "signal": None, "histogram": None}
        ema12_series = self._ema_series(closes, 12)
        ema26_series = self._ema_series(closes, 26)
        macd_series = [a - b for a, b in zip(ema12_series, ema26_series)]
        signal_series = self._ema_series(macd_series, 9)
        line = macd_series[-1]
        signal = signal_series[-1]
        return {
            "line": round(line, 4),
            "signal": round(signal, 4),
            "histogram": round(line - signal, 4),
        }

    def _ema_series(self, values: list[float], period: int) -> list[float]:
        if not values:
            return []
        alpha = 2 / (period + 1)
        out = [values[0]]
        for value in values[1:]:
            out.append(alpha * value + (1 - alpha) * out[-1])
        return out

    def _atr(self, highs: list[float], lows: list[float], closes: list[float], period: int) -> float | None:
        if len(closes) <= 1:
            return None
        trs = []
        for idx in range(1, len(closes)):
            trs.append(max(
                highs[idx] - lows[idx],
                abs(highs[idx] - closes[idx - 1]),
                abs(lows[idx] - closes[idx - 1]),
            ))
        window = trs[-period:] if len(trs) >= period else trs
        return round(sum(window) / len(window), 4) if window else None
