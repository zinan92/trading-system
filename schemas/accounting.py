from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


ACCOUNTING_SNAPSHOT_SCHEMA = "accounting-snapshot-v1"


@dataclass(frozen=True)
class AccountingSnapshot:
    """Immutable, versioned accounting read model.

    Source events remain owned by their execution engine or venue adapter. This
    envelope is a deterministic projection only; it cannot submit, cancel, or
    rewrite an order.
    """

    schema_version: str
    snapshot_id: str
    source_type: str
    source_name: str
    source_schema_version: str
    scope: Mapping[str, Any]
    currency: str
    orders: tuple[Mapping[str, Any], ...]
    fills: tuple[Mapping[str, Any], ...]
    positions: tuple[Mapping[str, Any], ...]
    trades: tuple[Mapping[str, Any], ...]
    counts: Mapping[str, Any]
    pnl: Mapping[str, Any]
    account: Mapping[str, Any]
    completeness: Mapping[str, Any]
    reconciliation: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source_type": self.source_type,
            "source_name": self.source_name,
            "source_schema_version": self.source_schema_version,
            "scope": _thaw(self.scope),
            "currency": self.currency,
            "orders": _thaw(self.orders),
            "fills": _thaw(self.fills),
            "positions": _thaw(self.positions),
            "trades": _thaw(self.trades),
            "counts": _thaw(self.counts),
            "pnl": _thaw(self.pnl),
            "account": _thaw(self.account),
            "completeness": _thaw(self.completeness),
            "reconciliation": _thaw(self.reconciliation),
        }


def build_accounting_snapshot(
    *,
    source_type: str,
    source_name: str,
    source_schema_version: str,
    scope: Mapping[str, Any],
    currency: str,
    orders: Sequence[Mapping[str, Any]],
    fills: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    counts: Mapping[str, Any],
    pnl: Mapping[str, Any],
    account: Mapping[str, Any],
    completeness: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
) -> AccountingSnapshot:
    """Deep-copy/freeze one JSON-safe snapshot and derive its stable identity."""

    payload = {
        "schema_version": ACCOUNTING_SNAPSHOT_SCHEMA,
        "source_type": _required_text(source_type, "source_type"),
        "source_name": _required_text(source_name, "source_name"),
        "source_schema_version": _required_text(source_schema_version, "source_schema_version"),
        "scope": _plain_json(scope),
        "currency": _required_text(currency, "currency").upper(),
        "orders": _plain_json(list(orders)),
        "fills": _plain_json(list(fills)),
        "positions": _plain_json(list(positions)),
        "trades": _plain_json(list(trades)),
        "counts": _plain_json(counts),
        "pnl": _plain_json(pnl),
        "account": _plain_json(account),
        "completeness": _plain_json(completeness),
        "reconciliation": _plain_json(reconciliation),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return AccountingSnapshot(
        schema_version=ACCOUNTING_SNAPSHOT_SCHEMA,
        snapshot_id=f"accounting-{digest}",
        source_type=payload["source_type"],
        source_name=payload["source_name"],
        source_schema_version=payload["source_schema_version"],
        scope=_freeze(payload["scope"]),
        currency=payload["currency"],
        orders=_freeze(payload["orders"]),
        fills=_freeze(payload["fills"]),
        positions=_freeze(payload["positions"]),
        trades=_freeze(payload["trades"]),
        counts=_freeze(payload["counts"]),
        pnl=_freeze(payload["pnl"]),
        account=_freeze(payload["account"]),
        completeness=_freeze(payload["completeness"]),
        reconciliation=_freeze(payload["reconciliation"]),
    )


def _required_text(value: Any, label: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise ValueError(f"accounting {label} is required")
    return rendered


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"accounting snapshot contains non-JSON value: {type(value).__name__}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value

