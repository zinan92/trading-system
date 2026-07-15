"""Market-view status evaluation (human directional view expiry and filter effect)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.journal_store import load_json
from services.market_view import MarketViewStore, infer_market_view_reference_price, market_view_target_expiry_bounds


class MarketViewMixin:
    def _market_view_status(self, run_date: str, latest: dict, latest_quote: dict) -> dict:
        root = self._global_output_root()
        rows = load_json(root / "market_views" / f"{run_date}.json")
        if not rows:
            rows = load_json(root / "market_views" / "current.json")
        view = rows[-1] if rows else {}
        checked_at = self._market_view_checked_at(latest, latest_quote)
        current_price = self._market_view_price(latest, latest_quote)
        if not view:
            return {
                "status": "missing",
                "expired": True,
                "filter_effect": "neutral_no_filter",
                "operator_message": "没有可用口述方向；系统不会按人工方向过滤多空信号。",
                "checked_at": checked_at.isoformat(),
                "current_price": current_price,
            }
        expiry = view.get("expiry") or {}
        generated_at = self._parse_dt(view.get("generated_at")) or checked_at
        valid_hours = self._safe_float(expiry.get("valid_for_hours"))
        if valid_hours is None:
            valid_hours = MarketViewStore.DEFAULT_VALID_FOR_HOURS
        expires_at = self._parse_dt(expiry.get("expires_at"))
        if expires_at is None:
            expires_at = generated_at.replace(microsecond=0) + timedelta(hours=valid_hours)
        reference_price = infer_market_view_reference_price(view, self.market_db)
        move_pct = self._safe_float(expiry.get("expires_if_price_moves_pct"))
        if move_pct is None:
            move_pct = MarketViewStore.DEFAULT_PRICE_MOVE_EXPIRY_PCT
        expire_above, expire_below, target_rule = market_view_target_expiry_bounds(view)
        payload = {
            "run_date": str(view.get("run_date") or run_date),
            "source": str(view.get("source") or ""),
            "generated_at": generated_at.isoformat(),
            "checked_at": checked_at.isoformat(),
            "direction_score": view.get("direction_score"),
            "direction_bias": view.get("direction_bias", ""),
            "stance": view.get("stance", ""),
            "summary": view.get("summary", ""),
            "trade_plan": view.get("trade_plan", ""),
            "key_levels": view.get("key_levels", []),
            "timeframes": view.get("timeframes", []),
            "current_price": current_price,
            "reference_price": reference_price,
            "expires_at": expires_at.isoformat(),
            "valid_for_hours": valid_hours,
            "expires_if_price_moves_pct": move_pct,
            "target_price": self._safe_float(expiry.get("target_price")),
            "target_rule": target_rule,
            "expire_above": expire_above,
            "expire_below": expire_below,
            "price_expiry_ready": bool(reference_price and move_pct),
            "status": "active",
            "expired": False,
            "reason": "market view active",
            "filter_effect": "active_direction_filter_enabled",
            "operator_message": "口述方向仍有效；强多/强空会过滤反向信号，偏多/偏空会降权反向信号。",
        }
        expired_reason = ""
        if checked_at > expires_at:
            expired_reason = f"time expired at {expires_at.isoformat()}"
        elif current_price is not None:
            if expire_above is not None and current_price >= expire_above:
                expired_reason = f"price reached expire_above {expire_above}"
            elif expire_below is not None and current_price <= expire_below:
                expired_reason = f"price reached expire_below {expire_below}"
            elif reference_price and move_pct:
                actual_move = abs((current_price - reference_price) / reference_price) * 100
                if actual_move >= move_pct:
                    expired_reason = f"price moved {actual_move:.2f}% from reference {reference_price}"
        if expired_reason:
            payload.update({
                "status": "expired",
                "expired": True,
                "reason": expired_reason,
                "filter_effect": "expired_direction_filter_disabled",
                "operator_message": "口述方向已失效；系统不再按这条观点过滤多空信号，只把它保留为历史判断。",
            })
        return payload

    def _market_view_checked_at(self, latest: dict, latest_quote: dict) -> datetime:
        for value in (latest_quote.get("timestamp"), latest.get("timestamp")):
            parsed = self._parse_dt(value)
            if parsed is not None:
                return parsed.replace(microsecond=0)
        return datetime.now(timezone.utc).replace(microsecond=0)

    def _market_view_price(self, latest: dict, latest_quote: dict) -> float | None:
        for row in (latest_quote, latest):
            for key in ("close", "price"):
                value = self._safe_float(row.get(key))
                if value is not None:
                    return value
        return None
