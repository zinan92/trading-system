from __future__ import annotations

from pathlib import Path

from services.journal_store import JournalStore, load_json, write_json

ACTIVE_ENTRY_ORDER_STATES = {"entry", "submitting", "accepted", "partially_filled"}
EXPIRED_PENDING_NOTE = "auto-rejected (expired): limit entry expired after {ttl} bars without touching entry"


def evaluate_pending_entry_state(output_root: Path, run_date: str, candles: list, asset: str) -> dict:
    root = Path(output_root)
    ticket_by_id = {
        str(item.get("ticket_id")): item
        for item in load_json(root / "trade_tickets" / f"{run_date}.json")
        if isinstance(item, dict) and item.get("ticket_id")
    }
    active_pending: list[dict] = []
    expired_pending: list[dict] = []
    for item in load_json(root / "journal_pending" / f"{run_date}.json"):
        if not isinstance(item, dict):
            continue
        ticket_id = str(item.get("ticket_id") or "")
        ticket = ticket_by_id.get(ticket_id, {})
        merged = {**ticket, **item}
        if asset and str(merged.get("asset") or asset) != asset:
            continue
        summary = _entry_summary(item, ticket, source="journal_pending")
        status = evaluate_limit_entry_status(merged, candles)
        if status.get("status") == "expired":
            expired_pending.append({**summary, "expiry": status})
        else:
            active_pending.append({**summary, "expiry": status})

    active_orders: list[dict] = []
    for item in load_json(root / "order_lifecycle" / f"{run_date}.json"):
        if not isinstance(item, dict) or str(item.get("state") or "") not in ACTIVE_ENTRY_ORDER_STATES:
            continue
        ticket = {}
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if isinstance(metadata.get("ticket"), dict):
            ticket = metadata["ticket"]
        elif item.get("ticket_id"):
            ticket = ticket_by_id.get(str(item.get("ticket_id")), {})
        if asset and str(ticket.get("asset") or asset) != asset:
            continue
        active_orders.append(
            {
                "source": "order_lifecycle",
                "ticket_id": str(item.get("ticket_id") or ticket.get("ticket_id") or ""),
                "signal_id": str(ticket.get("signal_id") or ""),
                "asset": str(ticket.get("asset") or asset),
                "order_id": str(item.get("order_id") or ""),
                "state": str(item.get("state") or ""),
                "entry_order_limit_price": _limit_price(ticket),
                "entry_order_ttl_bars": int(ticket.get("entry_order_ttl_bars", 0) or 0),
            }
        )

    return {
        "has_active": bool(active_pending or active_orders),
        "active_pending": active_pending,
        "expired_pending": expired_pending,
        "active_orders": active_orders,
        "active_count": len(active_pending) + len(active_orders),
        "expired_count": len(expired_pending),
    }


def reject_expired_pending_entries(output_root: Path, run_date: str, state: dict) -> dict:
    root = Path(output_root)
    store = JournalStore(root)
    closed: list[dict] = []
    errors: list[dict] = []
    for item in state.get("expired_pending", []) or []:
        ticket_id = str(item.get("ticket_id") or "")
        if not ticket_id:
            continue
        ttl = int(item.get("entry_order_ttl_bars", 0) or 0)
        note = EXPIRED_PENDING_NOTE.format(ttl=ttl or "configured")
        try:
            decision = store.record_decision(run_date=run_date, ticket_id=ticket_id, decision="rejected", notes=note)
            closed.append({"ticket_id": ticket_id, "decision_status": decision.get("decision_status", "rejected")})
        except (ValueError, OSError, KeyError, RuntimeError) as exc:
            _drop_pending(root, run_date, ticket_id)
            errors.append({"ticket_id": ticket_id, "error": f"{type(exc).__name__}: {exc}", "dropped_pending": True})
    return {"closed": closed, "errors": errors}


def build_pending_entry_block(asset: str, signal_id: str, state: dict) -> dict:
    pending_entry = _first_active_entry(state)
    return {
        "asset": asset,
        "ticket_id": str(pending_entry.get("ticket_id") or ""),
        "signal_id": signal_id,
        "reason": "pending_entry_exists",
        "pending_entry": pending_entry,
        "pending_entry_state": {
            "active_count": int(state.get("active_count", 0) or 0),
            "active_pending_count": len(state.get("active_pending", []) or []),
            "active_order_count": len(state.get("active_orders", []) or []),
            "expired_count": int(state.get("expired_count", 0) or 0),
        },
    }


def evaluate_limit_entry_status(ticket: dict, candles: list) -> dict:
    if str(ticket.get("order_type", "")).lower() != "limit":
        return {"status": "not_applicable"}
    price = _limit_price(ticket)
    if price is None:
        return {"status": "waiting", "reason": "limit price missing"}
    for bar in _active_limit_window(ticket, candles):
        if _bar_touches_price(bar, price):
            return {
                "status": "fillable",
                "actual_entry": round(price, 4),
                "limit_price": round(price, 4),
                "reason": "bar touched limit entry during active order window",
                "touched_bar_timestamp": str(_bar_value(bar, "timestamp") or ""),
            }
    if _is_expired(ticket, candles):
        ttl = int(ticket.get("entry_order_ttl_bars", 0) or 0)
        return {"status": "expired", "limit_price": round(price, 4), "reason": EXPIRED_PENDING_NOTE.format(ttl=ttl or "configured"), "ttl_bars": ttl}
    return {"status": "waiting", "limit_price": round(price, 4), "reason": "limit entry has not traded through price"}


def load_entry_candles(output_root: Path, run_date: str, ticket: dict) -> list[dict]:
    root = Path(output_root)
    asset = str(ticket.get("asset") or "GOLD")
    timeframe = str(ticket.get("entry_order_timeframe") or "").strip()
    candidates = []
    if timeframe:
        candidates.append(root / "clean_bars" / run_date / f"{asset}_{timeframe}.json")
    candidates.extend(sorted((root / "clean_bars" / run_date).glob(f"{asset}_*.json")))
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        rows = load_json(path)
        if rows:
            return rows
    return []


def _first_active_entry(state: dict) -> dict:
    pending = state.get("active_pending", []) or []
    if pending:
        return pending[0]
    orders = state.get("active_orders", []) or []
    return orders[0] if orders else {}


def _entry_summary(item: dict, ticket: dict, *, source: str) -> dict:
    merged = {**ticket, **item}
    return {
        "source": source,
        "journal_id": str(merged.get("journal_id") or ""),
        "ticket_id": str(merged.get("ticket_id") or ""),
        "signal_id": str(merged.get("signal_id") or ""),
        "asset": str(merged.get("asset") or ""),
        "decision_status": str(merged.get("decision_status") or ""),
        "created_at": str(merged.get("created_at") or merged.get("entry_order_created_bar_timestamp") or ""),
        "required_user_action": str(merged.get("required_user_action") or ""),
        "notes": str(merged.get("notes") or ""),
        "order_type": str(merged.get("order_type") or ""),
        "entry_order_limit_price": _limit_price(merged),
        "entry_order_ttl_bars": int(merged.get("entry_order_ttl_bars", 0) or 0),
        "entry_order_timeframe": str(merged.get("entry_order_timeframe") or ""),
        "entry_order_created_bar_timestamp": str(merged.get("entry_order_created_bar_timestamp") or ""),
    }


def _is_expired(ticket: dict, candles: list) -> bool:
    ttl = int(ticket.get("entry_order_ttl_bars", 0) or 0)
    created_at = str(ticket.get("entry_order_created_bar_timestamp") or "").strip()
    if ttl <= 0 or not created_at or not candles:
        return False
    created_index = None
    for index, bar in enumerate(candles):
        if str(_bar_value(bar, "timestamp") or "") == created_at:
            created_index = index
            break
    if created_index is None:
        return False
    return (len(candles) - 1 - created_index) >= ttl


def _active_limit_window(ticket: dict, candles: list) -> list:
    if not candles:
        return []
    ttl = int(ticket.get("entry_order_ttl_bars", 0) or 0)
    created_at = str(ticket.get("entry_order_created_bar_timestamp") or "").strip()
    if not created_at:
        return candles[-1:]
    created_index = None
    for index, bar in enumerate(candles):
        if str(_bar_value(bar, "timestamp") or "") == created_at:
            created_index = index
            break
    if created_index is None:
        return candles[-1:]
    start = created_index + 1
    end = len(candles)
    if ttl > 0:
        end = min(end, created_index + ttl + 1)
    return candles[start:end]


def _bar_touches_price(bar, price: float) -> bool:
    low = _float_or_none(_bar_value(bar, "low"))
    high = _float_or_none(_bar_value(bar, "high"))
    if low is None or high is None:
        close = _float_or_none(_bar_value(bar, "close"))
        return close is not None and abs(close - price) < 1e-9
    return low <= price <= high


def _limit_price(ticket: dict) -> float | None:
    direct = _float_or_none(ticket.get("entry_order_limit_price"))
    if direct is not None:
        return direct
    zone = str(ticket.get("entry_zone") or "").strip()
    if not zone:
        return None
    try:
        left, right = zone.split("-", 1)
        return (float(left) + float(right)) / 2
    except ValueError:
        return None


def _bar_value(bar, key: str):
    if isinstance(bar, dict):
        return bar.get(key)
    return getattr(bar, key, None)


def _float_or_none(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _drop_pending(output_root: Path, run_date: str, ticket_id: str) -> None:
    path = output_root / "journal_pending" / f"{run_date}.json"
    rows = [item for item in load_json(path) if item.get("ticket_id") != ticket_id]
    write_json(path, rows)
