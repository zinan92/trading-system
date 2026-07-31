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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


MARKET_EVENT_SCHEMA = "dualtrack-market-event-v1"
EXECUTION_RECONCILIATION_SCHEMA = "dualtrack-execution-parity-v1"
EXECUTION_COMMAND_CONTRACT_SCHEMA = "dualtrack-execution-contract-v1"


class ImmutableFillGuardError(RuntimeError):
    """An execution adapter detected mutation of persisted fill history."""


def normalize_execution_command(command: dict[str, Any], config: dict[str, Any] | None) -> dict[str, Any]:
    """Apply one explicit venue precision and attach immutable contract evidence.

    Both paper engines call this before accepting an order.  Raw requested
    values remain available for audit, while all executable values use the
    exact same price/quantity increments and fee-contract fingerprint.
    """

    normalized = dict(command)
    contract_evidence = execution_contract_evidence(config)
    if not contract_evidence:
        return normalized
    price_increment = _positive_decimal(contract_evidence["price_increment"], "execution price_increment")
    quantity_increment = _positive_decimal(contract_evidence["quantity_increment"], "execution quantity_increment")

    raw_price = _decimal_or_none(normalized.get("price"))
    raw_market_price = _decimal_or_none(normalized.get("market_price"))
    raw_quantity = _decimal_or_none(normalized.get("quantity", normalized.get("contracts")))
    raw_notional = _decimal_or_none(normalized.get("notional"))
    price_basis = raw_price or raw_market_price
    if raw_quantity is None and raw_notional is not None and price_basis is not None and price_basis > 0:
        raw_quantity = raw_notional / price_basis

    if raw_price is not None:
        executable_price = _round_to_increment(raw_price, price_increment)
        if executable_price != raw_price and normalized.get("requested_price") in (None, ""):
            normalized["requested_price"] = float(raw_price)
        normalized["price"] = float(executable_price)
    if raw_market_price is not None:
        normalized["market_price"] = float(_round_to_increment(raw_market_price, price_increment))
    for key in ("sl", "tp"):
        value = _decimal_or_none(normalized.get(key))
        if value is not None:
            normalized[key] = float(_round_to_increment(value, price_increment))

    if raw_quantity is not None:
        executable_quantity = _round_to_increment(raw_quantity, quantity_increment)
        if executable_quantity <= 0:
            raise ValueError("execution quantity rounds to zero at venue precision")
        if executable_quantity != raw_quantity and normalized.get("requested_quantity") in (None, ""):
            normalized["requested_quantity"] = float(raw_quantity)
        normalized["quantity"] = float(executable_quantity)
        if command.get("contracts") not in (None, ""):
            normalized["contracts"] = float(executable_quantity)
        executable_price = _decimal_or_none(normalized.get("price") or normalized.get("market_price"))
        if executable_price is not None:
            normalized["notional"] = float((executable_price * executable_quantity).quantize(Decimal("0.00000001")))

    normalized["execution_contract"] = contract_evidence
    return normalized


def execution_contract_evidence(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the exact execution/fee fingerprint used by every adapter."""

    settings = dict((config or {}).get("execution_contract") or {})
    if not settings:
        return {}
    price_increment = _positive_decimal(settings.get("price_increment"), "execution price_increment")
    quantity_increment = _positive_decimal(settings.get("quantity_increment"), "execution quantity_increment")
    contract_payload = {
        "schema_version": str(settings.get("schema_version") or EXECUTION_COMMAND_CONTRACT_SCHEMA),
        "execution_instrument_id": str(settings.get("execution_instrument_id") or ""),
        "price_increment": str(price_increment),
        "quantity_increment": str(quantity_increment),
    }
    if (config or {}).get("capital_per_track_usd") not in (None, ""):
        contract_payload["starting_cash"] = str(
            _positive_decimal((config or {}).get("capital_per_track_usd"), "execution starting_cash")
        )
    if (config or {}).get("max_leverage") not in (None, ""):
        contract_payload["max_leverage"] = str(
            _positive_decimal((config or {}).get("max_leverage"), "execution max_leverage")
        )
    fee_payload = dict((config or {}).get("paper_fee_model") or {})
    return {
        **contract_payload,
        "contract_hash": _payload_hash(contract_payload),
        "fee_contract_hash": _payload_hash(fee_payload),
    }


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

    for field in (
        "starting_cash",
        "realized_pnl",
        "ending_cash",
        "equity",
        "margin",
        "exposure",
        "slippage",
        "fees",
        "funding",
    ):
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
        for field in (
            "side", "event", "order_type", "liquidity", "price", "quantity", "cost", "slippage", "ts",
            "strategy_plan_id", "strategy_plan_version",
        ):
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
        for field in (
            "state", "side", "event", "order_type", "price", "quantity", "ts",
            "strategy_plan_id", "strategy_plan_version",
        ):
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
        for field in (
            "status", "side", "remaining_units", "entry_price", "exit_price", "realized_pnl", "sl", "tp",
            "strategy_plan_id", "strategy_plan_version",
        ):
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
    if field in {"cost", "slippage"}:
        return _normalized_number(fill.get(field), normalization["money_decimals"])
    if field == "ts":
        return _comparable_timestamp(fill.get(field))
    if field == "strategy_plan_version":
        return _comparable_integer(fill.get(field))
    return str(fill.get(field) or "").lower()


def _position_value(position: dict[str, Any], field: str, normalization: dict[str, Any]) -> Any:
    if field == "remaining_units":
        return _normalized_number(position.get(field), normalization["quantity_decimals"])
    if field in {"entry_price", "exit_price", "sl", "tp"}:
        return _normalized_number(position.get(field), normalization["price_decimals"])
    if field == "realized_pnl":
        return _normalized_number(position.get(field), normalization["money_decimals"])
    if field == "strategy_plan_version":
        return _comparable_integer(position.get(field))
    return str(position.get(field) or "").lower()


def _order_value(order: dict[str, Any], field: str, normalization: dict[str, Any]) -> Any:
    if field == "price":
        return _normalized_number(order.get(field), normalization["price_decimals"])
    if field == "quantity":
        return _normalized_number(order.get(field), normalization["quantity_decimals"])
    if field == "state":
        return _canonical_order_state(order.get(field))
    if field == "ts":
        return _comparable_timestamp(order.get(field))
    if field == "strategy_plan_version":
        return _comparable_integer(order.get(field))
    return str(order.get(field) or "").lower()


def _canonical_order_state(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "canceled": "cancelled",
        "partiallyfilled": "partially_filled",
        "partially_filled": "partially_filled",
    }
    return aliases.get(normalized, normalized)


def _comparable_timestamp(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return _iso_utc(value, "execution timestamp")
    except ValueError:
        return str(value).strip()


def _comparable_integer(value: Any) -> int | str:
    if value in (None, ""):
        return ""
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


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


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("execution numeric value is invalid") from exc
    if not parsed.is_finite():
        raise ValueError("execution numeric value must be finite")
    return parsed


def _positive_decimal(value: Any, label: str) -> Decimal:
    parsed = _decimal_or_none(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _round_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    units = (value / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (units * increment).quantize(increment)


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
