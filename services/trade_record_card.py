from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


TRADE_RECORD_CARD_VERSION = "trade-record-card-v1"


class TradeRecordCardBuilder:
    """Build the M1 per-trade record card.

    The card is intentionally stricter than the raw trade row. Raw artifacts may
    be sparse or legacy-shaped; the card must never leave the reader guessing.
    Missing information is turned into an explicit audit failure instead of an
    empty dashboard cell.
    """

    REQUIRED_FIELDS = (
        "trade_id",
        "strategy_id",
        "status",
        "symbol",
        "side",
        "quantity",
        "entry_reason",
        "exit_reason",
        "entry_price",
        "take_profit",
        "stop_loss",
        "protection_status",
        "pnl_status",
        "pnl_hand_check",
        "compliance_verdict",
    )

    def __init__(
        self,
        *,
        run_date: str,
        strategy_id: str = "",
        strategy_config: dict | None = None,
        latest_price: float | None = None,
        account_equity: float = 10_000.0,
    ) -> None:
        self.run_date = run_date
        self.strategy_id = strategy_id or "unscoped_strategy"
        self.strategy_config = strategy_config or {}
        self.latest_price = latest_price
        self.account_equity = float(account_equity or 10_000.0)

    def build(self, trade: dict) -> dict:
        classification = self.strategy_config.get("classification", {}) if isinstance(self.strategy_config, dict) else {}
        status = self._status(trade)
        entry = self._entry(trade)
        exit_plan = self._exit(trade, status)
        protection = self._protection(trade, status)
        pnl = self._pnl(trade, status)
        compliance = self._compliance(trade, entry, exit_plan, protection, pnl)
        sources = self._sources(trade)
        identity = {
            "strategy_id": self._text(trade.get("strategy_id") or self.strategy_id, "unscoped_strategy"),
            "trader_id": self._text(
                trade.get("trader_id")
                or self.strategy_config.get("trader_id")
                or classification.get("trader_id")
                or self._default_trader_id(classification),
                "unassigned_trader",
            ),
            "portfolio_id": self._text(
                trade.get("portfolio_id")
                or self.strategy_config.get("portfolio_id")
                or classification.get("portfolio_id")
                or f"portfolio_{self.strategy_id}",
                "unassigned_portfolio",
            ),
            "strategy_family": self._text(classification.get("family") or trade.get("strategy_family"), "unclassified_family"),
            "strategy_variant": self._text(
                self.strategy_config.get("strategy_variant")
                or classification.get("style")
                or trade.get("strategy_variant"),
                "base_variant",
            ),
        }
        card = {
            "schema_version": TRADE_RECORD_CARD_VERSION,
            "run_date": self.run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "trade_id": self._text(trade.get("trade_id"), "missing_trade_id"),
            "status": status,
            **identity,
            "symbol": self._text(trade.get("symbol"), "GOLD"),
            "side": self._side(trade),
            "quantity": self._number(trade.get("quantity")),
            "entry": entry,
            "exit": exit_plan,
            "protection": protection,
            "pnl": pnl,
            "compliance": compliance,
            "sources": sources,
        }
        audit = self._audit(card)
        card["audit"] = audit
        card["display"] = self._display(card)
        return card

    def summarize(self, cards: list[dict]) -> dict:
        rows = [card for card in cards if isinstance(card, dict)]
        failed = [card for card in rows if (card.get("audit") or {}).get("status") != "complete"]
        protection_missing = [
            card for card in rows if (card.get("protection") or {}).get("status") in {"missing", "partial"}
        ]
        return {
            "schema_version": TRADE_RECORD_CARD_VERSION,
            "trade_count": len(rows),
            "complete_count": len(rows) - len(failed),
            "incomplete_count": len(failed),
            "protection_missing_count": len(protection_missing),
            "status": "pass" if not failed and not protection_missing else "fail",
            "red_line": "no trade card may contain blank/unknown required fields or hidden protection gaps",
            "incomplete_trade_ids": [card.get("trade_id", "missing_trade_id") for card in failed],
            "protection_missing_trade_ids": [card.get("trade_id", "missing_trade_id") for card in protection_missing],
        }

    def _entry(self, trade: dict) -> dict:
        reason = (
            trade.get("entry_reason")
            or (trade.get("ticket") or {}).get("rationale")
            or (trade.get("strategy_signal") or {}).get("thesis")
            or trade.get("signal_regime")
        )
        return {
            "reason": self._text(reason, "missing_entry_reason"),
            "price": self._number(trade.get("entry_price")),
            "requested_price": self._number(trade.get("requested_entry_price")),
            "opened_at": self._text(trade.get("opened_at"), "missing_opened_at"),
            "order_id": self._text(trade.get("order_id"), "missing_order_id"),
            "ticket_id": self._text(trade.get("ticket_id"), "missing_ticket_id"),
            "signal_id": self._text(trade.get("signal_id"), "missing_signal_id"),
        }

    def _exit(self, trade: dict, status: str) -> dict:
        is_closed = status == "closed"
        reason = trade.get("exit_reason") or ((trade.get("exit_decision") or {}).get("exit_reason"))
        return {
            "status": "closed" if is_closed else "open",
            "reason": self._text(reason, "holding_open_position" if not is_closed else "missing_exit_reason"),
            "price": self._number(trade.get("exit_price")),
            "closed_at": self._text(trade.get("closed_at"), "position_still_open" if not is_closed else "missing_closed_at"),
        }

    def _protection(self, trade: dict, status: str) -> dict:
        flags = [str(item) for item in trade.get("quality_flags", []) if item is not None]
        stop = self._number(trade.get("stop_loss"))
        target = self._number(trade.get("target"))
        exchange_managed = bool(trade.get("exchange_managed") or "exchange_managed" in flags)
        explicit_missing = bool(trade.get("protective_order_missing") or "protective_order_missing" in flags)
        has_stop = stop is not None and stop != 0
        has_target = target is not None and target != 0
        if explicit_missing or (not exchange_managed and not has_stop and not has_target):
            protection_status = "missing"
        elif exchange_managed:
            protection_status = "exchange_managed"
        elif has_stop and has_target:
            protection_status = "protected"
        else:
            protection_status = "partial"
        return {
            "status": protection_status,
            "stop_loss": stop,
            "take_profit": target,
            "exchange_managed": exchange_managed,
            "missing": protection_status in {"missing", "partial"},
            "alert_required": protection_status in {"missing", "partial"} and status == "open",
            "summary": self._protection_summary(protection_status, stop, target, exchange_managed),
        }

    def _pnl(self, trade: dict, status: str) -> dict:
        side = self._side(trade)
        direction = 1 if side == "long" else -1
        quantity = float(self._number(trade.get("quantity")) or 0.0)
        entry = self._number(trade.get("entry_price"))
        stop = self._number(trade.get("stop_loss"))
        target = self._number(trade.get("target"))
        risk_amount = abs(float(entry or 0.0) - float(stop or entry or 0.0)) * quantity if entry is not None else 0.0
        realized = self._number(trade.get("realized_pnl"))
        unrealized = self._number(trade.get("unrealized_pnl"))
        latest = self._number(trade.get("latest_price")) or self.latest_price
        if status == "open" and unrealized is None and entry is not None and latest is not None:
            unrealized = round((float(latest) - float(entry)) * quantity * direction - float(trade.get("entry_total_cost", 0) or 0), 4)
        if status == "closed":
            pnl_value = realized
            pnl_status = "realized" if realized is not None else "missing_realized_pnl"
            r_multiple = round(float(realized or 0.0) / risk_amount, 4) if risk_amount else None
        else:
            pnl_value = unrealized
            pnl_status = "unrealized" if unrealized is not None else "missing_unrealized_pnl"
            r_multiple = round(float(unrealized or 0.0) / risk_amount, 4) if risk_amount else None
        target_move_pct = None
        if entry and target:
            target_move_pct = abs((float(target) - float(entry)) / float(entry)) * 100
        hand_check = self._pnl_hand_check(
            trade=trade,
            status=status,
            direction=direction,
            quantity=quantity,
            entry=entry,
            realized=realized,
            unrealized=unrealized,
            latest=latest,
        )
        return {
            "status": pnl_status,
            "value": pnl_value,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "gross_realized_pnl": self._number(trade.get("gross_realized_pnl")),
            "risk_amount": round(risk_amount, 4),
            "r_multiple": r_multiple,
            "target_move_pct": round(target_move_pct, 4) if target_move_pct is not None else None,
            "target_equity_return_pct_at_5x": round(target_move_pct * 5, 4) if target_move_pct is not None else None,
            "account_equity": self.account_equity,
            "hand_check": hand_check,
        }

    def _compliance(self, trade: dict, entry: dict, exit_plan: dict, protection: dict, pnl: dict) -> dict:
        checks = []

        def add(name: str, passed: bool, summary: str) -> None:
            checks.append({"name": name, "status": "pass" if passed else "fail", "summary": summary})

        add("entry_reason_present", not str(entry["reason"]).startswith("missing_"), entry["reason"])
        add("entry_price_present", entry["price"] is not None, f"entry_price={entry['price']}")
        add("exit_reason_present", not str(exit_plan["reason"]).startswith("missing_"), exit_plan["reason"])
        add(
            "protection_present",
            protection["status"] not in {"missing", "partial"},
            protection["summary"],
        )
        add("pnl_calculable", not str(pnl["status"]).startswith("missing_"), pnl["status"])
        add("r_multiple_calculable", pnl["r_multiple"] is not None, f"r={pnl['r_multiple']}")
        hand_check = pnl.get("hand_check") or {}
        add("pnl_hand_check_pass", hand_check.get("status") == "pass", hand_check.get("summary", "pnl hand-check unavailable"))
        missing = [item["name"] for item in checks if item["status"] != "pass"]
        return {
            "verdict": "pass" if not missing else "fail",
            "checks": checks,
            "missing_or_failed_checks": missing,
        }

    def _sources(self, trade: dict) -> dict:
        return {
            "source_artifacts": list(trade.get("source_artifacts", []) or []),
            "order_linked": not str(trade.get("order_id") or "").startswith("missing_") and bool(trade.get("order") or trade.get("order_id")),
            "ticket_linked": bool(trade.get("ticket") or trade.get("ticket_id")),
            "signal_linked": bool(trade.get("strategy_signal") or trade.get("signal_id")),
            "quality_flags": list(trade.get("quality_flags", []) or []),
        }

    def _audit(self, card: dict) -> dict:
        missing_fields = []
        if str(card.get("trade_id", "")).startswith("missing_"):
            missing_fields.append("trade_id")
        if card["entry"]["price"] is None:
            missing_fields.append("entry_price")
        if str(card["entry"]["reason"]).startswith("missing_"):
            missing_fields.append("entry_reason")
        if card["protection"]["stop_loss"] is None:
            missing_fields.append("stop_loss")
        if card["protection"]["take_profit"] is None:
            missing_fields.append("take_profit")
        if card["pnl"]["r_multiple"] is None:
            missing_fields.append("r_multiple")
        if ((card["pnl"].get("hand_check") or {}).get("status")) != "pass":
            missing_fields.append("pnl_hand_check")
        if card["compliance"]["verdict"] != "pass":
            missing_fields.extend(card["compliance"]["missing_or_failed_checks"])
        missing_fields = sorted(set(missing_fields))
        return {
            "status": "complete" if not missing_fields else "incomplete",
            "missing_fields": missing_fields,
            "required_fields": list(self.REQUIRED_FIELDS),
            "red_line_passed": not missing_fields and not card["protection"]["missing"],
        }

    def _display(self, card: dict) -> dict:
        pnl = card["pnl"]
        protection = card["protection"]
        return {
            "headline": f"{card['strategy_id']} {card['side']} {card['status']} {card['symbol']}",
            "entry": f"{card['entry']['reason']} @ {card['entry']['price']}",
            "exit": f"{card['exit']['reason']} @ {card['exit']['price'] if card['exit']['price'] is not None else card['exit']['status']}",
            "protection": protection["summary"],
            "pnl": f"{pnl['status']}={pnl['value']} R={pnl['r_multiple']}",
            "pnl_hand_check": (pnl.get("hand_check") or {}).get("summary", "pnl hand-check unavailable"),
            "compliance": card["compliance"]["verdict"],
        }

    def _pnl_hand_check(
        self,
        *,
        trade: dict,
        status: str,
        direction: int,
        quantity: float,
        entry: float | None,
        realized: float | None,
        unrealized: float | None,
        latest: float | None,
    ) -> dict:
        exit_price = self._number(trade.get("exit_price"))
        reference = exit_price if status == "closed" else latest
        reference_source = "exit_price" if status == "closed" else "latest_price"
        side_label = "long" if direction == 1 else "short"
        if entry is None or reference is None or quantity == 0:
            return {
                "status": "fail",
                "summary": "missing price or quantity for hand-check",
                "formula": f"({reference_source} - entry_price) * direction({side_label}) * quantity",
                "reference_price_source": reference_source,
            }
        expected_gross = round((float(reference) - float(entry)) * direction * quantity, 6)
        if status == "closed":
            cost = self._number(trade.get("total_cost"))
            if cost is None:
                cost = (self._number(trade.get("entry_total_cost")) or 0.0) + (self._number(trade.get("exit_total_cost")) or 0.0)
        else:
            cost = self._number(trade.get("entry_total_cost"))
            if cost is None:
                cost = self._number(trade.get("total_cost")) or 0.0
        expected_net = round(expected_gross - float(cost or 0.0), 6)
        reported_gross = self._number(trade.get("gross_realized_pnl" if status == "closed" else "gross_unrealized_pnl"))
        reported_net = realized if status == "closed" else unrealized
        tolerance = round(max(0.05, abs(expected_gross) * 0.003), 6)
        gross_delta = None if reported_gross is None else round(abs(expected_gross - reported_gross), 6)
        net_delta = None if reported_net is None else round(abs(expected_net - reported_net), 6)
        gross_ok = reported_gross is None or (gross_delta is not None and gross_delta <= tolerance)
        net_ok = reported_net is not None and net_delta is not None and net_delta <= tolerance
        status_value = "pass" if gross_ok and net_ok else "fail"
        formula = f"({reference_source} {reference} - entry {entry}) * {direction} * qty {quantity} - cost {cost}"
        return {
            "status": status_value,
            "summary": "PNL math reconciles" if status_value == "pass" else "PNL math mismatch",
            "formula": formula,
            "side": side_label,
            "direction_multiplier": direction,
            "quantity": round(quantity, 6),
            "entry_price": entry,
            "reference_price": reference,
            "reference_price_source": reference_source,
            "cost": cost,
            "expected_gross_pnl": expected_gross,
            "reported_gross_pnl": reported_gross,
            "gross_delta": gross_delta,
            "expected_net_pnl": expected_net,
            "reported_net_pnl": reported_net,
            "net_delta": net_delta,
            "tolerance": tolerance,
        }

    def _status(self, trade: dict) -> str:
        status = str(trade.get("status") or "open").lower()
        return "closed" if status == "closed" or trade.get("closed_at") else "open"

    def _side(self, trade: dict) -> str:
        side = str(trade.get("side") or "").lower()
        return side if side in {"long", "short"} else "long"

    def _text(self, value: Any, fallback: str) -> str:
        text = str(value).strip() if value is not None else ""
        return text if text else fallback

    def _number(self, value: Any) -> float | None:
        if value in {None, ""}:
            return None
        try:
            return round(float(value), 6)
        except (TypeError, ValueError):
            return None

    def _protection_summary(self, status: str, stop: float | None, target: float | None, exchange_managed: bool) -> str:
        if status == "exchange_managed":
            return f"exchange_managed stop={stop} target={target}"
        if status == "protected":
            return f"protected stop={stop} target={target}"
        if status == "partial":
            return f"partial_protection stop={stop} target={target}"
        return f"protection_missing stop={stop} target={target} exchange_managed={exchange_managed}"

    def _default_trader_id(self, classification: dict) -> str:
        family = str(classification.get("family") or "strategy").strip() or "strategy"
        return f"trader_{family}"
