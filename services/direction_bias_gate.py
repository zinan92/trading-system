from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.signal import Signal
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.market_view import (
    MarketViewStore,
    direction_bias_from_score,
    infer_market_view_reference_price,
    market_view_target_expiry_bounds,
)


class DirectionBiasGate:
    def __init__(self, output_root: Path, downweight_points: int = 15, market_db: Path | None = None) -> None:
        self.output_root = Path(output_root)
        self.downweight_points = int(downweight_points)
        if market_db is not None:
            self.market_db = Path(market_db)
        else:
            config = load_pipeline_config()
            self.market_db = ROOT / str(config.get("local_market_db", "data/market_data.db"))

    def apply(self, run_date: str, signal: Signal, current_price: float | None = None, as_of: str | None = None) -> tuple[Signal, dict]:
        view = MarketViewStore(self.output_root).latest(run_date)
        if not view:
            decision = self._decision(signal, {}, "allow", "no market view; neutral by default")
            self._record(run_date, decision)
            return signal, decision
        expiry = self._expiry_status(view, run_date, current_price=current_price, as_of=as_of or signal.generated_at)
        if expiry["expired"]:
            decision = self._decision(signal, view, "allow", f"market view expired: {expiry['reason']}", expiry)
            self._record(run_date, decision)
            return self._annotate(signal, decision), decision
        bias = direction_bias_from_score(view.get("direction_score", 50))
        direction = str(signal.direction or "")
        if direction not in {"long", "short"}:
            decision = self._decision(signal, view, "allow", "signal is not directional", expiry)
            self._record(run_date, decision)
            return self._annotate(signal, decision), decision
        if direction in set(bias.get("blocked_directions", [])):
            reason = f"{bias['bias']} blocks {direction} signal"
            decision = self._decision(signal, view, "block", reason, expiry)
            self._record(run_date, decision)
            return self._block(signal, decision), decision
        if self._should_downweight(direction, bias["bias"]):
            reason = f"{bias['bias']} downweights counter-bias {direction} signal"
            decision = self._decision(signal, view, "downweight", reason, expiry)
            self._record(run_date, decision)
            return self._downweight(signal, decision), decision
        decision = self._decision(signal, view, "allow", f"{direction} aligns with {bias['bias']}", expiry)
        self._record(run_date, decision)
        return self._annotate(signal, decision), decision

    def _should_downweight(self, direction: str, bias: str) -> bool:
        return (bias == "short_bias" and direction == "long") or (bias == "long_bias" and direction == "short")

    def _decision(self, signal: Signal, market_view: dict, action: str, reason: str, expiry: dict | None = None) -> dict:
        score = market_view.get("direction_score")
        bias = market_view.get("direction_bias")
        if score is None:
            score = 50
            bias = "neutral"
        effective_expiry = expiry or self._expiry_status(market_view, "", current_price=None, as_of=signal.generated_at) if market_view else {}
        return {
            "signal_id": signal.signal_id,
            "asset": signal.asset,
            "raw_direction": signal.direction,
            "action": action,
            "reason": reason,
            "direction_score": int(score),
            "direction_bias": str(bias),
            "market_view_source": str(self.output_root / "market_views" / "current.json") if market_view else "",
            "market_view_summary": market_view.get("summary", "") if market_view else "",
            "market_view_expiry": effective_expiry,
            "filter_effect": self._filter_effect(action, effective_expiry, bool(market_view)),
            "operator_message": self._operator_message(action, effective_expiry, bool(market_view)),
        }

    def _filter_effect(self, action: str, expiry: dict, has_market_view: bool) -> str:
        if not has_market_view:
            return "neutral_no_filter"
        if expiry.get("expired"):
            return "expired_direction_filter_disabled"
        if action == "block":
            return "active_view_blocked_signal"
        if action == "downweight":
            return "active_view_downweighted_signal"
        return "active_view_allows_signal"

    def _operator_message(self, action: str, expiry: dict, has_market_view: bool) -> str:
        if not has_market_view:
            return "没有可用口述方向；系统不会按人工方向过滤多空信号。"
        if expiry.get("expired"):
            return "口述方向已失效；系统不再按这条观点过滤多空信号，只把它保留为历史判断。"
        if action == "block":
            return "口述方向仍有效；这笔信号与强方向相反，已在出票前拦截。"
        if action == "downweight":
            return "口述方向仍有效；这笔信号与偏向相反，已降低强度和置信度。"
        return "口述方向仍有效；这笔信号未被人工方向过滤。"

    def _expiry_status(
        self,
        market_view: dict,
        run_date: str,
        *,
        current_price: float | None,
        as_of: str | None,
    ) -> dict:
        if not market_view:
            return {
                "status": "missing",
                "expired": False,
                "reason": "no_market_view",
                "checked_at": self._now().isoformat(),
                "filter_effect": "neutral_no_filter",
                "operator_message": "没有可用口述方向；系统不会按人工方向过滤多空信号。",
            }
        checked_at = self._parse_ts(as_of) or self._now()
        expiry = market_view.get("expiry") or {}
        if run_date and market_view.get("run_date") and market_view.get("run_date") != run_date:
            return self._expired("market view belongs to another run date", checked_at, current_price)
        expires_at = self._parse_ts(expiry.get("expires_at"))
        if expires_at is None:
            generated_at = self._parse_ts(market_view.get("generated_at"))
            if generated_at is not None:
                valid_hours = self._safe_float(expiry.get("valid_for_hours")) or MarketViewStore.DEFAULT_VALID_FOR_HOURS
                expires_at = generated_at.replace(microsecond=0) + timedelta(hours=valid_hours)
        if expires_at and checked_at > expires_at:
            return self._expired(f"time expired at {expires_at.isoformat()}", checked_at, current_price)

        reference_price = infer_market_view_reference_price(market_view, self.market_db)
        move_pct = self._safe_float(expiry.get("expires_if_price_moves_pct"))
        if move_pct is None:
            move_pct = MarketViewStore.DEFAULT_PRICE_MOVE_EXPIRY_PCT
        if reference_price and current_price and move_pct:
            actual_move_pct = abs((float(current_price) - reference_price) / reference_price) * 100
            if actual_move_pct >= move_pct:
                return {
                    **self._expired(
                        f"price moved {actual_move_pct:.2f}% from reference {reference_price:.2f}",
                        checked_at,
                        current_price,
                    ),
                    "reference_price": reference_price,
                    "actual_move_pct": round(actual_move_pct, 4),
                    "threshold_move_pct": move_pct,
                }

        expire_above, expire_below, target_rule = market_view_target_expiry_bounds(market_view)
        if current_price and expire_above and float(current_price) >= expire_above:
            return {
                **self._expired(f"price reached expire_above {expire_above:.2f}", checked_at, current_price),
                "target_price": self._safe_float(expiry.get("target_price")),
                "target_rule": target_rule,
                "expire_above": expire_above,
                "expire_below": expire_below,
            }
        if current_price and expire_below and float(current_price) <= expire_below:
            return {
                **self._expired(f"price reached expire_below {expire_below:.2f}", checked_at, current_price),
                "target_price": self._safe_float(expiry.get("target_price")),
                "target_rule": target_rule,
                "expire_above": expire_above,
                "expire_below": expire_below,
            }

        return {
            "status": "active",
            "expired": False,
            "reason": "active",
            "filter_effect": "active_direction_filter_enabled",
            "operator_message": "口述方向仍有效；强多/强空会过滤反向信号，偏多/偏空会降权反向信号。",
            "checked_at": checked_at.isoformat(),
            "current_price": current_price,
            "reference_price": reference_price,
            "expires_at": expires_at.isoformat() if expires_at else "",
            "threshold_move_pct": move_pct,
            "price_expiry_ready": bool(reference_price and move_pct),
            "target_price": self._safe_float(expiry.get("target_price")),
            "target_rule": target_rule,
            "expire_above": expire_above,
            "expire_below": expire_below,
        }

    def _expired(self, reason: str, checked_at: datetime, current_price: float | None) -> dict:
        return {
            "status": "expired",
            "expired": True,
            "reason": reason,
            "filter_effect": "expired_direction_filter_disabled",
            "operator_message": "口述方向已失效；系统不再按这条观点过滤多空信号，只把它保留为历史判断。",
            "checked_at": checked_at.isoformat(),
            "current_price": current_price,
        }

    @staticmethod
    def _parse_ts(value: object) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).replace(microsecond=0)
        except ValueError:
            return None

    @staticmethod
    def _safe_float(value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc).replace(microsecond=0)

    def _record(self, run_date: str, decision: dict) -> None:
        path = self.output_root / "direction_bias_decisions" / f"{run_date}.json"
        rows = [item for item in load_json(path) if item.get("signal_id") != decision.get("signal_id")]
        rows.append(decision)
        write_json(path, rows)
        write_json(self.output_root / "direction_bias_decisions" / "current.json", [decision])

    def _annotate(self, signal: Signal, decision: dict) -> Signal:
        artifact = str(self.output_root / "direction_bias_decisions" / "current.json")
        source_artifacts = [*list(signal.source_artifacts or [])]
        if artifact not in source_artifacts:
            source_artifacts.append(artifact)
        evidence = [*list(signal.evidence or []), f"Direction bias gate: {decision['action']} / {decision['reason']}"]
        return signal.__class__(**{**signal.to_dict(), "evidence": evidence, "source_artifacts": source_artifacts})

    def _block(self, signal: Signal, decision: dict) -> Signal:
        annotated = self._annotate(signal, decision)
        return signal.__class__(
            **{
                **annotated.to_dict(),
                "direction": "watch",
                "status": "no_signal",
                "regime": "direction_bias_block",
                "strength": 0,
                "confidence": min(signal.confidence, 40),
                "thesis": f"Direction bias blocked {signal.direction} signal: {decision['reason']}",
                "backtest_verdict": "no_trade",
            }
        )

    def _downweight(self, signal: Signal, decision: dict) -> Signal:
        annotated = self._annotate(signal, decision)
        return signal.__class__(
            **{
                **annotated.to_dict(),
                "strength": max(0, int(signal.strength) - self.downweight_points),
                "confidence": max(0, int(signal.confidence) - self.downweight_points),
                "regime": f"{signal.regime}_direction_bias_downweighted",
            }
        )
