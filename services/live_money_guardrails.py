from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config_loader import load_risk_rules
from services.journal_store import load_json, write_json


READY_STATUS = "READY"
HALT_STATUS = "BLOCKED_OPERATOR_HALT"


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class LiveHaltStore:
    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)

    def current(self) -> dict:
        rows = load_json(self.output_root / "live_halt" / "current.json")
        return rows[-1] if rows else {}

    def is_active(self) -> bool:
        return bool(self.current().get("active"))

    def activate(self, run_date: str, *, reason: str, source: str, details: dict | None = None) -> dict:
        record = {
            "run_date": run_date,
            "active": True,
            "status": HALT_STATUS,
            "reason_code": "operator_halt",
            "reason": reason,
            "source": source,
            "activated_at": _utcnow(),
            "details": details or {},
        }
        self._persist(run_date, record)
        return record

    def clear(self, run_date: str, *, reason: str, source: str, operator: str = "manual") -> dict:
        previous = self.current()
        record = {
            "run_date": run_date,
            "active": False,
            "status": READY_STATUS,
            "reason_code": "halt_cleared",
            "reason": reason,
            "source": source,
            "operator": operator,
            "cleared_at": _utcnow(),
            "previous_halt": previous if previous.get("active") else {},
        }
        self._persist(run_date, record)
        return record

    def _persist(self, run_date: str, record: dict) -> None:
        write_json(self.output_root / "live_halt" / "current.json", [record])
        path = self.output_root / "live_halt" / f"{run_date}.json"
        rows = load_json(path)
        rows.append(record)
        write_json(path, rows)


class LiveMoneyGuardrails:
    def __init__(self, output_root: Path, *, broker_config: dict | None = None) -> None:
        self.output_root = Path(output_root)
        self.broker_config = broker_config or {}
        self.rules = load_risk_rules().get("default", {})
        self.live_rules = self.rules.get("live_money_guardrails", {}) if isinstance(self.rules.get("live_money_guardrails"), dict) else {}
        self.halt_store = LiveHaltStore(self.output_root)

    def limits(self) -> dict:
        return {
            "daily_loss_limit_pct": float(self.live_rules.get("daily_loss_limit_pct", self.rules.get("daily_loss_stop_pct", 1.25))),
            "single_order_max_notional": float(self.live_rules.get("single_order_max_notional", 25.0)),
            "total_account_max_notional": float(self.live_rules.get("total_account_max_notional", 25.0)),
            "max_daily_entry_orders": int(self.live_rules.get("max_daily_entry_orders", 2)),
            "reference_equity": float(self.live_rules.get("reference_equity", 5000.0)),
            "max_reconciliation_age_seconds": int(self.live_rules.get("max_reconciliation_age_seconds", 600)),
        }

    def evaluate_order(
        self,
        run_date: str,
        *,
        ticket: dict,
        symbol: str,
        side: str,
        requested_price: float,
        quantity: float,
        source: str,
        reconciliation: dict | None = None,
        persist: bool = True,
    ) -> dict:
        reconciliation = reconciliation if isinstance(reconciliation, dict) and reconciliation else self._latest_reconciliation(run_date)
        limits = self.limits()
        candidate_notional = abs(float(requested_price or 0) * float(quantity or 0))
        daily_loss = self._daily_loss_snapshot(run_date, reconciliation, limits)
        exposure = self._exposure_snapshot(reconciliation, candidate_notional, requested_price)
        daily_orders = self._daily_entry_order_count(run_date)
        halt = self.halt_store.current()
        blockers: list[dict] = []

        if halt.get("active"):
            halt_reason = halt.get("reason") or "manual halt"
            blockers.append(
                self._blocker(
                    HALT_STATUS,
                    "operator_halt",
                    "live_halt.current",
                    f"operator HALT is active: {halt_reason}",
                    {"halt": halt},
                )
            )
        elif not daily_loss.get("known"):
            blockers.append(
                self._blocker(
                    "BLOCKED_MONEY_GUARDRAIL_UNKNOWN",
                    "daily_loss_unknown",
                    "live_money_guardrails.daily_loss",
                    daily_loss.get("reason") or "cannot compute daily live/testnet loss before entry",
                    daily_loss,
                )
            )

        if daily_loss.get("known") and daily_loss.get("loss_pct", 0.0) >= limits["daily_loss_limit_pct"]:
            blockers.append(
                self._blocker(
                    "BLOCKED_DAILY_LOSS_LIMIT",
                    "daily_loss_limit",
                    "live_money_guardrails.daily_loss",
                    f"daily live/testnet loss {daily_loss['loss_pct']:.4f}% reached limit {limits['daily_loss_limit_pct']:.4f}%",
                    daily_loss,
                )
            )
        if candidate_notional > limits["single_order_max_notional"]:
            blockers.append(
                self._blocker(
                    "BLOCKED_SINGLE_ORDER_NOTIONAL_LIMIT",
                    "single_order_notional_limit",
                    "live_money_guardrails.single_order_notional",
                    f"candidate notional {candidate_notional:.4f} exceeds single-order limit {limits['single_order_max_notional']:.4f}",
                    {"candidate_notional": candidate_notional},
                )
            )
        if exposure["projected_total_notional"] > limits["total_account_max_notional"]:
            blockers.append(
                self._blocker(
                    "BLOCKED_TOTAL_NOTIONAL_LIMIT",
                    "total_notional_limit",
                    "live_money_guardrails.total_account_notional",
                    f"projected total notional {exposure['projected_total_notional']:.4f} exceeds account limit {limits['total_account_max_notional']:.4f}",
                    exposure,
                )
            )
        if daily_orders >= limits["max_daily_entry_orders"]:
            blockers.append(
                self._blocker(
                    "BLOCKED_DAILY_TRADE_LIMIT",
                    "daily_trade_limit",
                    "live_money_guardrails.daily_entry_orders",
                    f"{daily_orders} live/testnet entry orders already reached daily limit {limits['max_daily_entry_orders']}",
                    {"entry_order_count": daily_orders},
                )
            )

        primary = blockers[0] if blockers else {}
        result = {
            "run_date": run_date,
            "checked_at": _utcnow(),
            "source": source,
            "status": primary.get("status", READY_STATUS),
            "allows_new_order": not blockers,
            "primary_blocker": primary,
            "blockers": blockers,
            "limits": limits,
            "candidate": {
                "ticket_id": str(ticket.get("ticket_id") or ""),
                "symbol": symbol,
                "side": side,
                "requested_price": round(float(requested_price or 0), 8),
                "quantity": round(float(quantity or 0), 12),
                "notional": round(candidate_notional, 8),
            },
            "daily_loss": daily_loss,
            "exposure": exposure,
            "daily_entry_orders": {"count": daily_orders, "limit": limits["max_daily_entry_orders"]},
            "halt": halt,
        }
        if persist:
            self.persist(run_date, result)
        return result

    def persist(self, run_date: str, result: dict) -> None:
        write_json(self.output_root / "live_money_guardrails" / "current.json", [result])
        path = self.output_root / "live_money_guardrails" / f"{run_date}.json"
        rows = load_json(path)
        rows.append(result)
        write_json(path, rows)

    def _latest_reconciliation(self, run_date: str) -> dict:
        candidates = []
        for path in (
            self.output_root / "live_reconciliation" / f"{run_date}.json",
            self.output_root / "live_reconciliation" / "current.json",
        ):
            rows = load_json(path)
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict):
                    candidates.append(row)
        if not candidates:
            return {}
        return max(candidates, key=lambda item: self._parse_iso(str(item.get("checked_at") or "")) or datetime.min.replace(tzinfo=timezone.utc))

    def _daily_loss_snapshot(self, run_date: str, reconciliation: dict, limits: dict) -> dict:
        if not reconciliation:
            return {"known": False, "reason": "live_reconciliation artifact is missing"}
        accounting = reconciliation.get("exchange_accounting", {}) if isinstance(reconciliation.get("exchange_accounting"), dict) else {}
        balance = reconciliation.get("exchange_balance", {}) if isinstance(reconciliation.get("exchange_balance"), dict) else {}
        invariant = self._reconciliation_daily_loss_invariant(run_date, reconciliation, accounting, limits)
        if not invariant.get("ok"):
            return invariant
        positions = reconciliation.get("exchange_positions", []) if isinstance(reconciliation.get("exchange_positions"), list) else []
        balance_equity = self._finite_float(balance.get("balance"), "exchange_balance.balance")
        reference_equity = balance_equity
        if reference_equity is None or reference_equity <= 0:
            return {"known": False, "reason": "reference equity is unavailable", "exchange_balance": balance}
        realized = self._finite_float(accounting.get("net_realized_pnl_estimate"), "exchange_accounting.net_realized_pnl_estimate")
        if realized is None:
            return {
                "known": False,
                "reason": "net realized PnL is unavailable or non-finite",
                "field": "exchange_accounting.net_realized_pnl_estimate",
                "value": str(accounting.get("net_realized_pnl_estimate")),
            }
        unrealized = 0.0
        for item in positions:
            if not isinstance(item, dict):
                continue
            value = self._finite_float(item.get("unrealized_pnl", item.get("unRealizedProfit")), "positionRisk.unrealized_pnl")
            if value is None:
                return {
                    "known": False,
                    "reason": "unrealized PnL is unavailable or non-finite",
                    "field": "positionRisk.unrealized_pnl",
                    "value": str(item.get("unrealized_pnl", item.get("unRealizedProfit"))),
                    "position_count": len(positions),
                }
            unrealized += value
        net_pnl = realized + unrealized
        if not math.isfinite(net_pnl):
            return {"known": False, "reason": "net PnL is non-finite", "net_realized_pnl_estimate": realized, "unrealized_pnl": unrealized}
        loss_pct = max(0.0, -net_pnl / reference_equity * 100)
        return {
            "known": True,
            "reference_equity": round(reference_equity, 8),
            "net_realized_pnl_estimate": round(realized, 8),
            "unrealized_pnl": round(unrealized, 8),
            "net_pnl_including_unrealized": round(net_pnl, 8),
            "loss_pct": round(loss_pct, 8),
            "reconciliation_checked_at": str(reconciliation.get("checked_at") or ""),
            "utc_trading_day": accounting.get("utc_trading_day", {}),
            "formula": "max(0, -(exchange_accounting.net_realized_pnl_estimate + sum(positionRisk.unrealized_pnl)) / reference_equity * 100)",
        }

    def _reconciliation_daily_loss_invariant(self, run_date: str, reconciliation: dict, accounting: dict, limits: dict) -> dict:
        if str(reconciliation.get("error") or "").strip():
            return {
                "known": False,
                "ok": False,
                "reason": "live_reconciliation has a fetch error",
                "error": str(reconciliation.get("error") or ""),
            }
        account_observation = reconciliation.get("account_observation", {}) if isinstance(reconciliation.get("account_observation"), dict) else {}
        if account_observation.get("account_observed") is not True:
            return {
                "known": False,
                "ok": False,
                "reason": "account history was not observed",
                "account_observation": account_observation,
            }
        balance = reconciliation.get("exchange_balance", {}) if isinstance(reconciliation.get("exchange_balance"), dict) else {}
        if balance.get("balance_present") is not True or account_observation.get("balance_present") is not True:
            return {
                "known": False,
                "ok": False,
                "reason": "exchange balance was not observed",
                "exchange_balance": balance,
                "account_observation": account_observation,
            }
        if str(reconciliation.get("run_date") or "") != run_date:
            return {
                "known": False,
                "ok": False,
                "reason": "live_reconciliation run_date does not match order run_date",
                "artifact_run_date": str(reconciliation.get("run_date") or ""),
                "expected_run_date": run_date,
            }
        checked_at = self._parse_iso(str(reconciliation.get("checked_at") or ""))
        if checked_at is None:
            return {"known": False, "ok": False, "reason": "live_reconciliation checked_at is missing or invalid"}
        now = datetime.now(timezone.utc)
        age_seconds = (now - checked_at).total_seconds()
        max_age = int(limits.get("max_reconciliation_age_seconds", 300))
        if age_seconds < -60:
            return {
                "known": False,
                "ok": False,
                "reason": "live_reconciliation checked_at is in the future",
                "checked_at": checked_at.isoformat(),
                "now": now.replace(microsecond=0).isoformat(),
            }
        if age_seconds > max_age:
            return {
                "known": False,
                "ok": False,
                "reason": "live_reconciliation artifact is stale",
                "checked_at": checked_at.isoformat(),
                "age_seconds": round(age_seconds, 3),
                "max_age_seconds": max_age,
            }
        trading_day = accounting.get("utc_trading_day", {}) if isinstance(accounting.get("utc_trading_day"), dict) else {}
        if str(trading_day.get("run_date") or "") != run_date:
            return {
                "known": False,
                "ok": False,
                "reason": "exchange_accounting utc_trading_day does not match order run_date",
                "utc_trading_day": trading_day,
                "expected_run_date": run_date,
            }
        expected_start, expected_end = self._utc_day_window_ms(run_date)
        actual_start = self._finite_int(trading_day.get("start_time_ms"))
        actual_end = self._finite_int(trading_day.get("end_time_ms"))
        if actual_start != expected_start or actual_end != expected_end:
            return {
                "known": False,
                "ok": False,
                "reason": "exchange_accounting utc_trading_day window does not match expected UTC day",
                "utc_trading_day": trading_day,
                "expected_utc_trading_day": {"run_date": run_date, "start_time_ms": expected_start, "end_time_ms": expected_end},
            }
        return {"ok": True}

    def _finite_float(self, value: Any, label: str) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    def _finite_int(self, value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed

    def _parse_iso(self, value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _utc_day_window_ms(self, run_date: str) -> tuple[int, int]:
        start = datetime.strptime(run_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1) - timedelta(milliseconds=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def _exposure_snapshot(self, reconciliation: dict, candidate_notional: float, fallback_price: float) -> dict:
        positions = reconciliation.get("exchange_positions", []) if isinstance(reconciliation.get("exchange_positions"), list) else []
        existing = 0.0
        for position in positions:
            if not isinstance(position, dict):
                continue
            qty = abs(float(position.get("position_amt", position.get("positionAmt", 0)) or 0))
            price = float(position.get("entry_price", position.get("entryPrice", 0)) or 0) or float(fallback_price or 0)
            existing += qty * price
        return {
            "existing_position_notional": round(existing, 8),
            "candidate_notional": round(candidate_notional, 8),
            "projected_total_notional": round(existing + candidate_notional, 8),
        }

    def _daily_entry_order_count(self, run_date: str) -> int:
        dirs = {
            str(self.broker_config.get("request_dir", "")),
            "live_order_requests",
            "testnet_order_requests",
        }
        count = 0
        for dirname in [item for item in dirs if item]:
            rows = load_json(self.output_root / dirname / f"{run_date}.json")
            for row in rows if isinstance(rows, list) else []:
                if self._is_counted_entry_request(row):
                    count += 1
        return count

    def _is_counted_entry_request(self, row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        receipt = row.get("receipt", {}) if isinstance(row.get("receipt"), dict) else {}
        if str(receipt.get("status") or row.get("status") or "").lower() in {"blocked", "dry_run", "binance_dry_run", "rejected"}:
            return False
        request = row.get("request", {}) if isinstance(row.get("request"), dict) else {}
        entry = request.get("entry", {}) if isinstance(request.get("entry"), dict) else {}
        if not entry:
            return False
        if str(entry.get("reduceOnly", "")).lower() == "true":
            return False
        return bool(entry.get("newClientOrderId") or entry.get("clientOrderId"))

    def _blocker(self, status: str, code: str, source: str, message: str, evidence: dict) -> dict:
        return {
            "status": status,
            "code": code,
            "source": source,
            "message": message,
            "evidence": evidence,
        }
