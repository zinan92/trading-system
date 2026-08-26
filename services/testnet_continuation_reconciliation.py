"""Identity-bound Broker reconciliation used before Testnet continuation.

The strategy lifecycles keep their canonical state machines, but all paths that
could add exposure must first observe the public Broker truth.  Local fixtures
are explicitly marked as simulation-only because they do not project fills into
their account snapshot; an external Testnet adapter must prove the full order
and position set or the slice remains blocked.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any


def reconcile_before_continuation(
    broker: object,
    state: Mapping[str, Any],
    *,
    timestamp: str,
    strategy_family: str,
    local_position_quantity: float,
) -> dict[str, Any]:
    """Read orders and account positions before a next-entry/re-arm/resume.

    The return value is safe to persist.  It never mutates the Broker and a
    missing/ambiguous read is a blocker rather than a guessed ``ok`` result.
    """

    config = getattr(broker, "broker_config", None)
    if not isinstance(config, Mapping):
        return _blocked("broker_identity_unavailable", timestamp, strategy_family)
    instrument_id = str(state.get("instrument_id") or "").strip()
    account_id = str(config.get("account_id") or "").strip()
    if not instrument_id or not account_id:
        return _blocked("broker_identity_incomplete", timestamp, strategy_family)
    if (
        str(config.get("broker_id") or "").lower() != "hyperliquid"
        or str(config.get("environment") or "").lower() != "testnet"
        or config.get("live_trading_enabled") is True
        or config.get("real_money_eligible") is True
    ):
        return _blocked("broker_environment_identity_mismatch", timestamp, strategy_family)
    try:
        open_orders = broker.request("order_execution", "open_orders", instrument_id)
        account = broker.request("account", "read", account_id)
    except Exception as exc:  # noqa: BLE001 - unknown venue truth must stop.
        return _blocked(
            f"broker_truth_query_failed:{type(exc).__name__}",
            timestamp,
            strategy_family,
        )
    if not isinstance(open_orders, (list, tuple)):
        return _blocked("broker_open_orders_shape_invalid", timestamp, strategy_family)
    positions = getattr(account, "positions", None)
    if positions is None and isinstance(account, Mapping):
        positions = account.get("positions")
    if not isinstance(positions, (list, tuple)):
        return _blocked("broker_positions_shape_invalid", timestamp, strategy_family)

    broker_order_ids = {
        _order_identity(row)
        for row in open_orders
        if _order_identity(row)
    }
    local_order_ids = {
        str(row.get("order_id") or row.get("client_order_id") or "").strip()
        for row in state.get("orders") or ()
        if isinstance(row, Mapping) and row.get("state") == "accepted"
    }
    foreign_orders = sorted(broker_order_ids - local_order_ids)
    transport_state = str(config.get("transport_state") or "").strip().lower()
    simulation_fixture = transport_state == "local_fixture"
    if foreign_orders:
        return _blocked(
            "foreign_open_order_detected",
            timestamp,
            strategy_family,
            broker_open_order_count=len(broker_order_ids),
            local_open_order_count=len(local_order_ids),
            foreign_order_ids=foreign_orders,
        )
    if not simulation_fixture and broker_order_ids != local_order_ids:
        return _blocked(
            "open_order_set_mismatch",
            timestamp,
            strategy_family,
            broker_open_order_count=len(broker_order_ids),
            local_open_order_count=len(local_order_ids),
        )

    broker_position_quantity = _position_quantity(positions, instrument_id)
    if not math.isfinite(broker_position_quantity):
        return _blocked("broker_position_quantity_invalid", timestamp, strategy_family)
    if not simulation_fixture and abs(broker_position_quantity - local_position_quantity) > 1e-9:
        return _blocked(
            "position_quantity_mismatch",
            timestamp,
            strategy_family,
            broker_position_quantity=broker_position_quantity,
            local_position_quantity=local_position_quantity,
        )
    account_address = str(
        getattr(account, "account_address", None)
        or (account.get("account_address") if isinstance(account, Mapping) else "")
        or (account.get("accountAddress") if isinstance(account, Mapping) else "")
        or ""
    ).strip()
    if account_address and account_address != account_id:
        return _blocked("account_identity_mismatch", timestamp, strategy_family)
    provenance = getattr(account, "provenance", None)
    provenance_transport = str(getattr(provenance, "transport_state", "") or "").lower()
    if provenance_transport and provenance_transport not in {"local_fixture", "external_testnet"}:
        return _blocked("account_transport_identity_mismatch", timestamp, strategy_family)
    return {
        "status": "ok",
        "at": timestamp,
        "strategy_family": strategy_family,
        "instrument_id": instrument_id,
        "account_id": account_id,
        "broker_id": str(config.get("broker_id") or ""),
        "environment": str(config.get("environment") or ""),
        "transport_state": transport_state,
        "simulation_fixture": simulation_fixture,
        "broker_open_order_count": len(broker_order_ids),
        "local_open_order_count": len(local_order_ids),
        "broker_position_quantity": broker_position_quantity,
        "local_position_quantity": local_position_quantity,
    }


def _order_identity(row: object) -> str:
    if isinstance(row, Mapping):
        return str(
            row.get("order_id")
            or row.get("broker_order_id")
            or row.get("client_order_id")
            or row.get("cloid")
            or ""
        ).strip()
    return str(
        getattr(row, "order_id", None)
        or getattr(row, "broker_order_id", None)
        or getattr(row, "client_order_id", None)
        or ""
    ).strip()


def _position_quantity(positions: tuple[object, ...] | list[object], instrument_id: str) -> float:
    total = 0.0
    for row in positions:
        row_instrument = (
            row.get("instrument_id") if isinstance(row, Mapping) else getattr(row, "instrument_id", None)
        )
        if row_instrument and str(row_instrument) != instrument_id:
            continue
        raw = (
            row.get("signed_quantity") if isinstance(row, Mapping) else getattr(row, "signed_quantity", None)
        )
        if raw is None:
            raw = row.get("quantity") if isinstance(row, Mapping) else getattr(row, "quantity", 0)
        try:
            total += float(raw or 0)
        except (TypeError, ValueError):
            return float("nan")
    return total if math.isfinite(total) else float("nan")


def _blocked(reason: str, timestamp: str, strategy_family: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "blocked",
        "at": timestamp,
        "strategy_family": strategy_family,
        "reason": reason,
        **extra,
    }


__all__ = ["reconcile_before_continuation"]
