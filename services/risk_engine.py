from __future__ import annotations

from dataclasses import replace

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
        if not adjusted_signal.approved_candidate(default["min_signal_strength"], default["min_confidence"]):
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
        stop_pct = float(overrides.get("stop_loss_pct", 4.0))
        target_pct = float(overrides.get("target_pct", stop_pct * 2))
        position_size_pct = float(overrides.get("position_size_pct", default["position_size_pct"]))
        if adjusted_signal.regime == "event_risk_reduction" or adjusted_signal.factor_scores.get("event", 100) < 50:
            position_size_pct *= float(default.get("event_window_size_multiplier", 0.4))
        max_loss_pct = float(default["max_loss_pct"])

        if adjusted_signal.direction == "long":
            entry_low = latest * 0.995
            entry_high = latest * 1.005
            stop_loss = latest * (1 - stop_pct / 100)
            target = latest * (1 + target_pct / 100)
            action = "prepare_buy"
        else:
            entry_low = latest * 0.995
            entry_high = latest * 1.005
            stop_loss = latest * (1 + stop_pct / 100)
            target = latest * (1 - target_pct / 100)
            action = "prepare_sell"

        ticket = TradeTicket(
            ticket_id=f"ticket_{adjusted_signal.signal_id.removeprefix('sig_')}",
            signal_id=adjusted_signal.signal_id,
            asset=adjusted_signal.asset,
            asset_class=adjusted_signal.asset_class,
            action=action,
            entry_zone=f"{entry_low:.2f}-{entry_high:.2f}",
            stop_loss=round(stop_loss, 2),
            targets=[round(target, 2)],
            position_size_pct=position_size_pct,
            max_loss_pct=max_loss_pct,
            order_type="limit",
            time_in_force="day",
            paper_only=True,
            trigger=f"Review {adjusted_signal.asset} if signal remains above strength/confidence thresholds.",
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
        )
        quality = self.quality_gate.evaluate_ticket(ticket.to_dict(), latest_price=latest)
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

    def _signal_gate_payload(self, signal: Signal, default: dict) -> dict:
        min_strength = int(default.get("min_signal_strength", 0) or 0)
        min_confidence = int(default.get("min_confidence", 0) or 0)
        reasons = []
        direction_passes = signal.direction in {"long", "short"}
        strength_passes = int(signal.strength) >= min_strength
        confidence_passes = int(signal.confidence) >= min_confidence
        if not direction_passes:
            reasons.append("signal is not directional")
        if not strength_passes:
            reasons.append(f"signal strength {signal.strength} below minimum {min_strength}")
        if not confidence_passes:
            reasons.append(f"signal confidence {signal.confidence} below minimum {min_confidence}")
        return {
            "passes": bool(direction_passes and strength_passes and confidence_passes),
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
