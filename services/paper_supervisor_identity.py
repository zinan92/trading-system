"""Exact, shared identities for Paper Supervisor start authority."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping, Sequence
from typing import Any


INTENT_CONTRACT_SCHEMA_VERSION = "paper-supervisor-start-intent-contract-v1"


def build_start_intent_contract(
    *,
    plan: Mapping[str, Any],
    commands: Sequence[Mapping[str, Any]],
    pre_start_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind one future public start to its exact plan and order identities."""

    plan_identity = {
        "strategy_plan_id": _required_text(
            plan.get("strategy_plan_id"),
            "strategy_plan_id",
        ),
        "strategy_plan_version": _positive_int(
            plan.get("version"),
            "strategy_plan_version",
        ),
        "strategy_type": _required_text(
            plan.get("strategy_type") or "grid",
            "strategy_type",
        ),
        "direction": _required_text(
            plan.get("direction"),
            "direction",
        ),
    }
    fingerprints = sorted(order_fingerprint(row) for row in commands)
    if not fingerprints or len(set(fingerprints)) != len(fingerprints):
        raise ValueError("execution_receipt_identity_invalid")
    return {
        "schema_version": INTENT_CONTRACT_SCHEMA_VERSION,
        "pre_start_plan_identity": (
            _plan_identity(pre_start_plan)
            if pre_start_plan
            else None
        ),
        "plan_identity": plan_identity,
        "expected_order_count": len(fingerprints),
        "expected_order_fingerprints": fingerprints,
    }


def _plan_identity(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "strategy_plan_id": _required_text(
            plan.get("strategy_plan_id"),
            "strategy_plan_id",
        ),
        "strategy_plan_version": _positive_int(
            plan.get("version")
            or plan.get("strategy_plan_version"),
            "strategy_plan_version",
        ),
        "strategy_type": _required_text(
            plan.get("strategy_type") or "grid",
            "strategy_type",
        ),
        "direction": _required_text(
            plan.get("direction"),
            "direction",
        ),
    }


def order_fingerprint(order: Mapping[str, Any]) -> str:
    """Hash the immutable logical order identity shared by plan and snapshot."""

    source_fill_id = _required_text(
        order.get("source_fill_id"),
        "source_fill_id",
    )
    plan_id = _required_text(
        order.get("strategy_plan_id"),
        "strategy_plan_id",
    )
    version = _positive_int(
        order.get("strategy_plan_version"),
        "strategy_plan_version",
    )
    payload = {
        "source_fill_id": source_fill_id,
        "strategy_plan_id": plan_id,
        "strategy_plan_version": version,
        "event": _required_text(order.get("event") or "entry", "event"),
        "symbol": _required_text(order.get("symbol"), "symbol"),
        "side": _required_text(order.get("side"), "side").lower(),
        "order_type": _required_text(
            order.get("order_type"),
            "order_type",
        ).lower(),
        "price": _number(order.get("price"), "price"),
        "quantity": _number(order.get("quantity"), "quantity"),
        "notional": _number(order.get("notional"), "notional"),
        "sl": _number(order.get("sl"), "sl"),
        "tp": (
            _number(order.get("tp"), "tp")
            if order.get("tp") not in {None, ""}
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def accepted_order_fingerprints(
    snapshot: Mapping[str, Any],
) -> list[str]:
    """Return exact accepted-entry identities; malformed rows fail closed."""

    rows = snapshot.get("orders")
    if not isinstance(rows, list):
        raise ValueError("execution_receipt_identity_invalid")
    accepted = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("state") or "").lower() == "accepted"
        and str(row.get("event") or "entry").lower() == "entry"
    ]
    order_ids = [
        _required_text(
            row.get("order_id"),
            "execution_receipt_identity",
        )
        for row in accepted
    ]
    if len(set(order_ids)) != len(order_ids):
        raise ValueError("execution_receipt_identity_invalid")
    fingerprints = sorted(order_fingerprint(row) for row in accepted)
    if len(set(fingerprints)) != len(fingerprints):
        raise ValueError("execution_receipt_identity_invalid")
    return fingerprints


def accepted_order_identities(
    snapshot: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Bind every accepted receipt ID to its immutable logical identity."""

    rows = snapshot.get("orders")
    if not isinstance(rows, list):
        raise ValueError("execution_receipt_identity_invalid")
    identities = [
        {
            "order_id": _required_text(
                row.get("order_id"),
                "execution_receipt_identity",
            ),
            "fingerprint": order_fingerprint(row),
            "side": _order_side(row.get("side")),
            "quantity": _number(
                row.get("quantity"),
                "execution_receipt_identity",
            ),
        }
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("state") or "").lower() == "accepted"
        and str(row.get("event") or "entry").lower() == "entry"
    ]
    order_ids = [row["order_id"] for row in identities]
    fingerprints = [row["fingerprint"] for row in identities]
    if (
        len(set(order_ids)) != len(order_ids)
        or len(set(fingerprints)) != len(fingerprints)
    ):
        raise ValueError("execution_receipt_identity_invalid")
    return sorted(identities, key=lambda row: row["order_id"])


def open_position_identities(
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Bind each open position to exact, persisted entry-fill evidence."""

    positions = snapshot.get("positions")
    fills = snapshot.get("fills")
    orders = snapshot.get("orders")
    if (
        not isinstance(positions, list)
        or not isinstance(fills, list)
        or not isinstance(orders, list)
    ):
        raise ValueError("execution_receipt_identity_invalid")
    open_positions = [
        row
        for row in positions
        if isinstance(row, Mapping)
        and str(row.get("status") or "").lower() == "open"
    ]
    entry_fills = [
        row
        for row in fills
        if isinstance(row, Mapping)
        and str(row.get("event") or "").lower() == "entry"
    ]
    position_ids: set[str] = set()
    trade_ids: set[str] = set()
    used_fill_ids: set[str] = set()
    result: list[dict[str, Any]] = []
    for position in open_positions:
        position_id = _required_text(
            position.get("position_id"),
            "execution_receipt_identity",
        )
        trade_id = _required_text(
            position.get("trade_id"),
            "execution_receipt_identity",
        )
        if position_id in position_ids or trade_id in trade_ids:
            raise ValueError("execution_receipt_identity_invalid")
        position_ids.add(position_id)
        trade_ids.add(trade_id)
        plan_id = _required_text(
            position.get("strategy_plan_id"),
            "execution_receipt_identity",
        )
        plan_version = _positive_int(
            position.get("strategy_plan_version"),
            "execution_receipt_identity",
        )
        position_side = _position_side(position.get("side"))
        entry_price = _positive_decimal(
            position.get("entry_price"),
            "execution_receipt_identity",
        )
        remaining = _positive_decimal(
            position.get("remaining_units", position.get("quantity")),
            "execution_receipt_identity",
        )
        matching_fills = [
            row
            for row in entry_fills
            if str(row.get("trade_id") or "") == trade_id
        ]
        if not matching_fills:
            raise ValueError("execution_receipt_identity_invalid")
        matching_orders = [
            row
            for row in orders
            if isinstance(row, Mapping)
            and str(row.get("order_id") or "") == trade_id
            and str(row.get("event") or "entry").lower() == "entry"
        ]
        if len(matching_orders) != 1:
            raise ValueError("execution_receipt_identity_invalid")
        entry_order = matching_orders[0]
        order_quantity = _positive_decimal(
            entry_order.get("quantity"),
            "execution_receipt_identity",
        )
        if (
            _entry_position_side(entry_order.get("side"))
            != position_side
            or _required_text(
                entry_order.get("strategy_plan_id"),
                "execution_receipt_identity",
            )
            != plan_id
            or _positive_int(
                entry_order.get("strategy_plan_version"),
                "execution_receipt_identity",
            )
            != plan_version
        ):
            raise ValueError("execution_receipt_identity_invalid")
        fill_ids: list[str] = []
        entry_quantity = Decimal("0")
        entry_notional = Decimal("0")
        for fill in matching_fills:
            fill_id = _required_text(
                fill.get("fill_id"),
                "execution_receipt_identity",
            )
            if fill_id in used_fill_ids:
                raise ValueError("execution_receipt_identity_invalid")
            used_fill_ids.add(fill_id)
            fill_ids.append(fill_id)
            if _required_text(
                fill.get("order_id"),
                "execution_receipt_identity",
            ) != trade_id:
                raise ValueError("execution_receipt_identity_invalid")
            fill_quantity = _positive_decimal(
                fill.get("quantity"),
                "execution_receipt_identity",
            )
            fill_price = _positive_decimal(
                fill.get("price"),
                "execution_receipt_identity",
            )
            entry_quantity += fill_quantity
            entry_notional += fill_price * fill_quantity
            if _entry_position_side(fill.get("side")) != position_side:
                raise ValueError("execution_receipt_identity_invalid")
            fill_plan_id = _required_text(
                fill.get("strategy_plan_id"),
                "execution_receipt_identity",
            )
            if fill_plan_id != plan_id:
                raise ValueError("execution_receipt_identity_invalid")
            if _positive_int(
                fill.get("strategy_plan_version"),
                "execution_receipt_identity",
            ) != plan_version:
                raise ValueError("execution_receipt_identity_invalid")
        if (
            remaining > entry_quantity
            or entry_quantity > order_quantity
            or (
                str(entry_order.get("state") or "").lower() == "filled"
                and entry_quantity != order_quantity
            )
            or entry_price != entry_notional / entry_quantity
        ):
            raise ValueError("execution_receipt_identity_invalid")
        result.append(
            {
                "position_id": position_id,
                "trade_id": trade_id,
                "entry_fill_ids": sorted(fill_ids),
                "strategy_plan_id": plan_id,
                "strategy_plan_version": plan_version,
                "side": position_side,
                "entry_price": format(entry_price.normalize(), "f"),
                "entry_quantity": format(
                    entry_quantity.normalize(),
                    "f",
                ),
                "order_quantity": format(
                    order_quantity.normalize(),
                    "f",
                ),
            }
        )
    return sorted(result, key=lambda row: row["position_id"])


def all_plan_entry_fingerprints(
    snapshot: Mapping[str, Any],
    *,
    strategy_plan_id: str,
    strategy_plan_version: int,
) -> list[str]:
    """Return every historical entry identity for exact running adoption."""

    rows = snapshot.get("orders")
    if not isinstance(rows, list):
        raise ValueError("execution_receipt_identity_invalid")
    selected = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("event") or "entry").lower() == "entry"
        and str(row.get("strategy_plan_id") or "") == strategy_plan_id
        and int(row.get("strategy_plan_version") or 0)
        == strategy_plan_version
    ]
    fingerprints = sorted(order_fingerprint(row) for row in selected)
    if len(set(fingerprints)) != len(fingerprints):
        raise ValueError("execution_receipt_identity_invalid")
    return fingerprints


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label}_invalid")
    return text


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}_invalid") from exc
    if parsed <= 0:
        raise ValueError(f"{label}_invalid")
    return parsed


def _number(value: Any, label: str) -> str:
    return format(_positive_decimal(value, label).normalize(), "f")


def _positive_decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label}_invalid") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label}_invalid")
    return parsed


def _order_side(value: Any) -> str:
    side = _required_text(
        value,
        "execution_receipt_identity",
    ).lower()
    if side not in {"buy", "sell"}:
        raise ValueError("execution_receipt_identity_invalid")
    return side


def _position_side(value: Any) -> str:
    side = _required_text(
        value,
        "execution_receipt_identity",
    ).lower()
    if side in {"long", "buy"}:
        return "long"
    if side in {"short", "sell"}:
        return "short"
    raise ValueError("execution_receipt_identity_invalid")


def _entry_position_side(value: Any) -> str:
    return "long" if _order_side(value) == "buy" else "short"
