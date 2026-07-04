from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone

from schemas.analysis import Analysis
from schemas.backtest import BacktestEvidence
from schemas.signal import Signal
from schemas.trade_ticket import TradeTicket
from services.kline_client import Candle
from services.trade_quality import TradeQualityGate


class RiskEngine:
    def __init__(self, rules: dict) -> None:
        self.rules = rules
        self.quality_gate = TradeQualityGate(rules)
        self.last_rejection: dict = {}

    def generate_ticket(
        self,
        signal: Signal,
        candles: list[Candle],
        analysis: Analysis | None = None,
        backtest: BacktestEvidence | None = None,
    ) -> TradeTicket | None:
        self.last_rejection = {}
        default = self.rules["default"]
        adjusted_signal = signal
        if analysis:
            adjusted_signal = replace(
                signal,
                confidence=min(100, max(0, signal.confidence + analysis.confidence_adjustment)),
                methods=analysis.methods,
            )
        if not self._approved_candidate(adjusted_signal, default):
            if adjusted_signal.direction in {"long", "short"}:
                self.last_rejection = self._signal_gate_rejection(adjusted_signal, default)
            return None
        # Thin local backtest evidence is a promotion/live-capital warning, not
        # a paper-sampling blocker. Paper/shadow strategies need to keep
        # collecting real trade samples; quality, signal, exposure, and
        # execution-safety gates below still decide whether a ticket is allowed.
        if not candles:
            if adjusted_signal.direction in {"long", "short"}:
                self.last_rejection = {
                    "asset": adjusted_signal.asset,
                    "ticket_id": "",
                    "signal_id": adjusted_signal.signal_id,
                    "reason": "market data unavailable for risk engine",
                    "signal_gate": self._signal_gate_payload(adjusted_signal, default),
                    "market_data_gate": {
                        "passes": False,
                        "reasons": ["no candles available for ticket generation"],
                    },
                }
            return None

        latest = candles[-1].close
        overrides = self.rules.get("asset_class_overrides", {}).get(signal.asset_class, {})
        stop_equity_pct = float(overrides.get("stop_loss_pct", 4.0))
        target_equity_pct = float(overrides.get("target_pct", stop_equity_pct * 2))
        leverage = max(float(self.quality_gate.config.effective_leverage), 1.0)
        stop_price_pct = stop_equity_pct / leverage
        target_price_pct = target_equity_pct / leverage
        position_size_pct = float(overrides.get("position_size_pct", default["position_size_pct"]))
        if adjusted_signal.regime == "event_risk_reduction" or adjusted_signal.factor_scores.get("event", 100) < 50:
            position_size_pct *= float(default.get("event_window_size_multiplier", 0.4))
        max_loss_pct = float(default["max_loss_pct"])

        macd_plan = self._macd_cross_risk_plan(adjusted_signal, candles, latest)
        if macd_plan.get("status") == "invalid":
            self.last_rejection = {
                "asset": adjusted_signal.asset,
                "ticket_id": f"ticket_{adjusted_signal.signal_id.removeprefix('sig_')}",
                "signal_id": adjusted_signal.signal_id,
                "reason": macd_plan.get("reason", "MACD stop anchor invalid for ticket generation"),
                "signal_gate": self._signal_gate_payload(adjusted_signal, default),
                "market_data_gate": macd_plan,
            }
            return None

        if adjusted_signal.direction == "long":
            if macd_plan.get("status") == "ok":
                stop_loss = float(macd_plan["stop_loss"])
                target = float(macd_plan["target"])
            else:
                stop_loss = latest * (1 - stop_price_pct / 100)
                target = latest * (1 + target_price_pct / 100)
            entry_low = entry_high = self._limit_entry_price(latest, stop_loss)
            action = "prepare_buy"
        else:
            if macd_plan.get("status") == "ok":
                stop_loss = float(macd_plan["stop_loss"])
                target = float(macd_plan["target"])
            else:
                stop_loss = latest * (1 + stop_price_pct / 100)
                target = latest * (1 - target_price_pct / 100)
            entry_low = entry_high = self._limit_entry_price(latest, stop_loss)
            action = "prepare_sell"
        entry_limit_price = round(float(entry_low), 2)
        ttl_bars = int(default.get("limit_order_ttl_bars", default.get("entry_order_ttl_bars", 10)) or 10)
        latest_bar = candles[-1]

        ticket = TradeTicket(
            ticket_id=f"ticket_{adjusted_signal.signal_id.removeprefix('sig_')}",
            signal_id=adjusted_signal.signal_id,
            asset=adjusted_signal.asset,
            asset_class=adjusted_signal.asset_class,
            action=action,
            entry_zone=f"{entry_limit_price:.2f}-{entry_limit_price:.2f}",
            stop_loss=round(stop_loss, 2),
            targets=[round(target, 2)],
            position_size_pct=position_size_pct,
            max_loss_pct=max_loss_pct,
            order_type="limit",
            time_in_force="gtc",
            paper_only=True,
            trigger=f"Place a limit entry for {adjusted_signal.asset}; expire it if not touched within {ttl_bars} bars.",
            invalid_if=adjusted_signal.invalid_if,
            methods=analysis.methods if analysis else [],
            backtest=backtest.to_dict() if backtest else {},
            signal_regime=adjusted_signal.regime,
            signal_strength=adjusted_signal.strength,
            signal_confidence=adjusted_signal.confidence,
            factor_scores=adjusted_signal.factor_scores,
            source_artifacts=adjusted_signal.source_artifacts,
            rationale=analysis.thesis if analysis else adjusted_signal.thesis,
            counter_rationale=analysis.counter_thesis if analysis else "",
            manual_execution_required=True,
            verdict="approved",
            trade_quality={},
            generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            latest_price=round(float(latest), 2),
            entry_order_limit_price=entry_limit_price,
            entry_order_ttl_bars=ttl_bars,
            entry_order_timeframe=str(getattr(latest_bar, "timeframe", "")),
            entry_order_created_bar_timestamp=str(getattr(latest_bar, "timestamp", "")),
        )
        quality = self._with_position_risk(
            self.quality_gate.evaluate_ticket(ticket.to_dict(), latest_price=latest),
            position_size_pct=position_size_pct,
            max_loss_pct=max_loss_pct,
            planned_target_equity_pct=target_equity_pct,
            planned_stop_equity_pct=stop_equity_pct,
        )
        if not quality["passes"]:
            self.last_rejection = {
                "asset": adjusted_signal.asset,
                "ticket_id": ticket.ticket_id,
                "signal_id": adjusted_signal.signal_id,
                "reason": "trade quality gate blocked trading",
                "signal_gate": self._signal_gate_payload(adjusted_signal, default),
                "trade_quality": quality,
            }
            return None
        return TradeTicket(**{**ticket.to_dict(), "trade_quality": quality})

    def _with_position_risk(
        self,
        quality: dict,
        *,
        position_size_pct: float,
        max_loss_pct: float,
        planned_target_equity_pct: float,
        planned_stop_equity_pct: float,
    ) -> dict:
        result = dict(quality)
        position_fraction = max(float(position_size_pct), 0.0) / 100
        target_equity_return_pct = result.get("target_equity_return_pct")
        stop_equity_risk_pct = result.get("stop_equity_risk_pct")
        account_target_return_pct = (
            round(float(target_equity_return_pct) * position_fraction, 4)
            if target_equity_return_pct is not None
            else None
        )
        account_stop_risk_pct = (
            round(float(stop_equity_risk_pct) * position_fraction, 4)
            if stop_equity_risk_pct is not None
            else None
        )
        result.update(
            {
                "position_size_pct": round(float(position_size_pct), 4),
                "max_loss_pct": round(float(max_loss_pct), 4),
                "planned_target_equity_return_pct": round(float(planned_target_equity_pct), 4),
                "planned_stop_equity_risk_pct": round(float(planned_stop_equity_pct), 4),
                "estimated_account_target_return_pct": account_target_return_pct,
                "estimated_account_stop_risk_pct": account_stop_risk_pct,
            }
        )
        if account_stop_risk_pct is not None and account_stop_risk_pct > float(max_loss_pct):
            reasons = list(result.get("reasons", []))
            reasons.append(
                f"estimated account stop risk {account_stop_risk_pct:.2f}% exceeds max loss {float(max_loss_pct):.2f}%"
            )
            result["reasons"] = reasons
            result["passes"] = False
        return result

    def _macd_cross_risk_plan(self, signal: Signal, candles: list[Candle], latest: float) -> dict:
        if signal.regime not in {"macd_golden_cross", "macd_death_cross"}:
            return {"status": "not_applicable"}
        index = self._macd_cross_index(signal)
        if index is None or index < 0 or index >= len(candles):
            return {
                "status": "invalid",
                "reason": "MACD cross index is missing or outside candle history",
                "cross_index": index,
                "bar_count": len(candles),
            }
        anchor = candles[index]
        reward_to_risk = 1.5
        if signal.direction == "long":
            stop_loss = float(anchor.low)
            risk = float(latest) - stop_loss
            if risk <= 0:
                return {
                    "status": "invalid",
                    "reason": "MACD golden-cross stop low is not below the planned entry",
                    "cross_index": index,
                    "entry_price": latest,
                    "stop_loss": stop_loss,
                }
            target = float(latest) + reward_to_risk * risk
        elif signal.direction == "short":
            stop_loss = float(anchor.high)
            risk = stop_loss - float(latest)
            if risk <= 0:
                return {
                    "status": "invalid",
                    "reason": "MACD death-cross stop high is not above the planned entry",
                    "cross_index": index,
                    "entry_price": latest,
                    "stop_loss": stop_loss,
                }
            target = float(latest) - reward_to_risk * risk
        else:
            return {"status": "not_applicable"}
        return {
            "status": "ok",
            "source": "macd_cross_extreme",
            "cross_index": index,
            "entry_price": latest,
            "stop_loss": stop_loss,
            "target": target,
            "reward_to_risk": reward_to_risk,
        }

    def _macd_cross_index(self, signal: Signal) -> int | None:
        for item in signal.evidence:
            match = re.search(r"cross bar index=(\d+)/(\d+)", str(item))
            if match:
                return int(match.group(1))
        return None

    def _approved_candidate(self, signal: Signal, default: dict) -> bool:
        if self._is_binary_macd_signal(signal):
            return signal.direction in {"long", "short"} and signal.status == "new"
        return signal.approved_candidate(default["min_signal_strength"], default["min_confidence"])

    def _is_binary_macd_signal(self, signal: Signal) -> bool:
        return signal.regime in {"macd_golden_cross", "macd_death_cross"} or signal.horizon == "macd"

    def _limit_entry_price(self, close_price: float, stop_loss: float) -> float:
        return (float(close_price) + float(stop_loss)) / 2

    def _signal_gate_payload(self, signal: Signal, default: dict) -> dict:
        min_strength = int(default.get("min_signal_strength", 0) or 0)
        min_confidence = int(default.get("min_confidence", 0) or 0)
        reasons = []
        direction_passes = signal.direction in {"long", "short"}
        binary_macd = self._is_binary_macd_signal(signal)
        strength_passes = True if binary_macd else int(signal.strength) >= min_strength
        confidence_passes = True if binary_macd else int(signal.confidence) >= min_confidence
        if not direction_passes:
            reasons.append("signal is not directional")
        if not strength_passes:
            reasons.append(f"signal strength {signal.strength} below minimum {min_strength}")
        if not confidence_passes:
            reasons.append(f"signal confidence {signal.confidence} below minimum {min_confidence}")
        return {
            "passes": bool(direction_passes and strength_passes and confidence_passes),
            "binary_strategy": "macd" if binary_macd else "",
            "direction": signal.direction,
            "strength": signal.strength,
            "confidence": signal.confidence,
            "min_signal_strength": min_strength,
            "min_confidence": min_confidence,
            "direction_passes": direction_passes,
            "strength_passes": strength_passes,
            "confidence_passes": confidence_passes,
            "reasons": reasons,
        }

    def _signal_gate_rejection(self, signal: Signal, default: dict) -> dict:
        return {
            "asset": signal.asset,
            "ticket_id": "",
            "signal_id": signal.signal_id,
            "reason": "signal threshold gate blocked trading",
            "signal_gate": self._signal_gate_payload(signal, default),
        }
