"""Canonical, engine-neutral contracts for the DualTrack execution boundary.

These helpers deliberately operate on plain dictionaries.  The current paper
engine and a future Nautilus adapter can therefore consume the exact same
market event and expose snapshots that are comparable without leaking either
engine's internal objects into the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any


MARKET_EVENT_SCHEMA = "dualtrack-market-event-v1"
EXECUTION_RECONCILIATION_SCHEMA = "dualtrack-execution-parity-v1"


def canonical_market_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a trusted execution mark or OHLC bar.

    This is intentionally stricter than a chart payload: stale, synthetic, or
    provenance-free prices must not make it to an execution engine.
    """

    if not isinstance(event, dict):
        raise ValueError("market event must be an object")
    cycle_id = str(event.get("cycle_id") or "").strip()
    if not cycle_id:
        raise ValueError("market event cycle_id is required")
    if event.get("fresh") is not True:
        raise ValueError("market event is stale")
    if event.get("is_synthetic") is not False:
        raise ValueError("synthetic market data is forbidden")
    source = str(event.get("source") or "").strip()
    if not source:
        raise ValueError("market event source is required")

    timestamp = _iso_utc(event.get("ts_event"), "market event ts_event")
    price = _positive_number(event.get("price"), "market event price")
    normalized = {
        "schema_version": MARKET_EVENT_SCHEMA,
        "cycle_id": cycle_id,
        "ts_event": timestamp,
        "event_started_at": _optional_iso_utc(event.get("event_started_at"), "market event event_started_at"),
        "price": price,
        "open": _optional_positive_number(event.get("open"), "market event open"),
        "high": _optional_positive_number(event.get("high"), "market event high"),
        "low": _optional_positive_number(event.get("low"), "market event low"),
        "fresh": True,
        "is_synthetic": False,
        "source": source,
        "provider": str(event.get("provider") or "").strip(),
        "instrument_id": str(event.get("instrument_id") or event.get("symbol") or "").strip(),
    }
    _validate_ohlc(normalized)
    normalized["event_id"] = str(event.get("event_id") or _stable_id(normalized))
    return normalized


def compare_execution_snapshots(
    authoritative: dict[str, Any],
    candidate: dict[str, Any],
    *,
    cycle_id: str,
) -> dict[str, Any]:
    """Compare normalized engine outcomes without silently applying tolerances."""

    if not isinstance(authoritative, dict) or not isinstance(candidate, dict):
        raise ValueError("execution snapshots must be objects")
    normalization = _comparison_normalization(candidate)
    fields = ("realized", "unrealized")
    differences: list[dict[str, Any]] = []
    for field in fields:
        expected = _normalized_number((authoritative.get("pnl") or {}).get(field), normalization["money_decimals"])
        actual = _normalized_number((candidate.get("pnl") or {}).get(field), normalization["money_decimals"])
        if expected != actual:
            differences.append({"path": f"pnl.{field}", "authoritative": expected, "candidate": actual})

    for field in ("orders", "fills", "positions"):
        expected = len(authoritative.get(field) or [])
        actual = len(candidate.get(field) or [])
        if expected != actual:
            differences.append({"path": f"{field}.count", "authoritative": expected, "candidate": actual})

    _compare_orders(authoritative.get("orders") or [], candidate.get("orders") or [], differences, normalization)
    _compare_fills(authoritative.get("fills") or [], candidate.get("fills") or [], differences, normalization)
    _compare_positions(authoritative.get("positions") or [], candidate.get("positions") or [], differences, normalization)

    for field in ("starting_cash", "realized_pnl", "ending_cash", "margin", "exposure", "slippage"):
        expected = _normalized_number((authoritative.get("account") or {}).get(field), normalization["money_decimals"])
        actual = _normalized_number((candidate.get("account") or {}).get(field), normalization["money_decimals"])
        if expected != actual:
            differences.append({"path": f"account.{field}", "authoritative": expected, "candidate": actual})

    expected_reconciliation = str((authoritative.get("reconciliation") or {}).get("status") or "")
    actual_reconciliation = str((candidate.get("reconciliation") or {}).get("status") or "")
    if expected_reconciliation != actual_reconciliation:
        differences.append({
            "path": "reconciliation.status",
            "authoritative": expected_reconciliation,
            "candidate": actual_reconciliation,
        })

    expected_open = _normalized_number(
        _open_units(authoritative.get("positions") or []),
        normalization["quantity_decimals"],
    )
    actual_open = _normalized_number(
        _open_units(candidate.get("positions") or []),
        normalization["quantity_decimals"],
    )
    if expected_open != actual_open:
        differences.append({"path": "positions.open_units", "authoritative": expected_open, "candidate": actual_open})

    return {
        "schema_version": EXECUTION_RECONCILIATION_SCHEMA,
        "cycle_id": cycle_id,
        "authoritative_engine": str(authoritative.get("engine") or ""),
        "candidate_engine": str(candidate.get("engine") or ""),
        "status": "pass" if not differences else "drift",
        "tolerance": _reported_normalization(normalization),
        "differences": differences,
    }


def _validate_ohlc(event: dict[str, Any]) -> None:
    values = {key: event.get(key) for key in ("open", "high", "low")}
    if all(value is None for value in values.values()):
        return
    if any(value is None for value in values.values()):
        raise ValueError("market event OHLC must include open, high, and low together")
    if event["low"] > min(event["open"], event["high"], event["price"]):
        raise ValueError("market event low exceeds OHLC range")
    if event["high"] < max(event["open"], event["low"], event["price"]):
        raise ValueError("market event high is below OHLC range")


def _iso_utc(value: Any, label: str) -> str:
    if value in (None, ""):
        raise ValueError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _optional_iso_utc(value: Any, label: str) -> str | None:
    return None if value in (None, "") else _iso_utc(value, label)


def _positive_number(value: Any, label: str) -> float:
    parsed = _number_or_none(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _optional_positive_number(value: Any, label: str) -> float | None:
    return None if value in (None, "") else _positive_number(value, label)


def _number_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _open_units(positions: list[dict[str, Any]]) -> float:
    return sum(
        _number_or_none(item.get("remaining_units")) or 0.0
        for item in positions
        if item.get("status") == "open"
    )


def _compare_fills(
    authoritative: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    differences: list[dict[str, Any]],
    normalization: dict[str, Any],
) -> None:
    for index, (expected_fill, actual_fill) in enumerate(zip(authoritative, candidate)):
        for field in ("side", "event", "price", "quantity"):
            expected = _fill_value(expected_fill, field, normalization)
            actual = _fill_value(actual_fill, field, normalization)
            if expected != actual:
                differences.append({
                    "path": f"fills[{index}].{field}",
                    "authoritative": expected,
                    "candidate": actual,
                })


def _compare_orders(
    authoritative: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    differences: list[dict[str, Any]],
    normalization: dict[str, Any],
) -> None:
    for index, (expected_order, actual_order) in enumerate(zip(authoritative, candidate)):
        for field in ("state", "side", "event", "order_type", "price", "quantity"):
            expected = _order_value(expected_order, field, normalization)
            actual = _order_value(actual_order, field, normalization)
            if expected != actual:
                differences.append({
                    "path": f"orders[{index}].{field}",
                    "authoritative": expected,
                    "candidate": actual,
                })


def _compare_positions(
    authoritative: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    differences: list[dict[str, Any]],
    normalization: dict[str, Any],
) -> None:
    for index, (expected_position, actual_position) in enumerate(zip(authoritative, candidate)):
        for field in ("status", "side", "remaining_units"):
            expected = _position_value(expected_position, field, normalization)
            actual = _position_value(actual_position, field, normalization)
            if expected != actual:
                differences.append({
                    "path": f"positions[{index}].{field}",
                    "authoritative": expected,
                    "candidate": actual,
                })


def _fill_value(fill: dict[str, Any], field: str, normalization: dict[str, Any]) -> Any:
    if field == "quantity":
        return _normalized_number(
            fill.get("quantity", fill.get("pnl_units", fill.get("units"))),
            normalization["quantity_decimals"],
        )
    if field == "price":
        return _normalized_number(fill.get(field), normalization["price_decimals"])
    return str(fill.get(field) or "").lower()


def _position_value(position: dict[str, Any], field: str, normalization: dict[str, Any]) -> Any:
    if field == "remaining_units":
        return _normalized_number(position.get(field), normalization["quantity_decimals"])
    return str(position.get(field) or "").lower()


def _order_value(order: dict[str, Any], field: str, normalization: dict[str, Any]) -> Any:
    if field == "price":
        return _normalized_number(order.get(field), normalization["price_decimals"])
    if field == "quantity":
        return _normalized_number(order.get(field), normalization["quantity_decimals"])
    return str(order.get(field) or "").lower()


def _comparison_normalization(candidate: dict[str, Any]) -> dict[str, Any]:
    provided = (candidate.get("capabilities") or {}).get("comparison_normalization") or {}
    if not provided:
        return {
            "mode": "exact",
            "price_decimals": None,
            "quantity_decimals": None,
            "money_decimals": None,
        }
    if provided.get("mode") != "venue_precision":
        raise ValueError("execution comparison normalization mode is invalid")
    try:
        values = {
            "mode": "venue_precision",
            "price_decimals": int(provided["price_decimals"]),
            "quantity_decimals": int(provided["quantity_decimals"]),
            "money_decimals": int(provided["money_decimals"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("execution comparison normalization is invalid") from exc
    if any(values[key] < 0 or values[key] > 15 for key in ("price_decimals", "quantity_decimals", "money_decimals")):
        raise ValueError("execution comparison normalization is invalid")
    return values


def _reported_normalization(normalization: dict[str, Any]) -> dict[str, Any]:
    if normalization["mode"] == "exact":
        return {"mode": "exact", "value": 0}
    return dict(normalization)


def _normalized_number(value: Any, decimals: int | None) -> float | None:
    parsed = _number_or_none(value)
    if parsed is None or decimals is None:
        return parsed
    return round(parsed, decimals)


def _stable_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"market-{hashlib.sha256(encoded).hexdigest()[:20]}"
