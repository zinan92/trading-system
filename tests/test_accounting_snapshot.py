from __future__ import annotations

import pytest

from schemas.accounting import ACCOUNTING_SNAPSHOT_SCHEMA, build_accounting_snapshot


def _parts() -> dict:
    return {
        "source_type": "execution_engine",
        "source_name": "legacy_paper",
        "source_schema_version": "dualtrack-execution-v1",
        "scope": {"cycle_id": "2026-07-18_DAY"},
        "currency": "USDT",
        "orders": [{"order_id": "order-1", "state": "filled"}],
        "fills": [{"fill_id": "fill-1", "trade_id": "trade-1"}],
        "positions": [{"position_id": "position-1", "trade_id": "trade-1"}],
        "trades": [{"trade_id": "trade-1", "status": "open"}],
        "counts": {"trade_count": 1, "completed_trade_count": 0},
        "pnl": {"net_realized_pnl": -0.5, "unrealized_pnl": 2.0},
        "account": {"starting_balance": 10_000.0, "equity": 10_001.5},
        "completeness": {"status": "complete", "limitations": []},
        "reconciliation": {"status": "pass", "issues": []},
    }


def test_accounting_snapshot_is_deeply_immutable_and_json_serializable() -> None:
    parts = _parts()

    snapshot = build_accounting_snapshot(**parts)
    parts["orders"][0]["state"] = "cancelled"
    parts["completeness"]["limitations"].append("mutated-after-build")

    assert snapshot.schema_version == ACCOUNTING_SNAPSHOT_SCHEMA
    assert snapshot.orders[0]["state"] == "filled"
    assert snapshot.to_dict()["completeness"]["limitations"] == []
    with pytest.raises(TypeError):
        snapshot.counts["trade_count"] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.orders[0]["state"] = "cancelled"  # type: ignore[index]


def test_accounting_snapshot_id_is_deterministic_and_content_addressed() -> None:
    first = build_accounting_snapshot(**_parts())
    second = build_accounting_snapshot(**_parts())
    changed = _parts()
    changed["pnl"]["net_realized_pnl"] = -0.6
    third = build_accounting_snapshot(**changed)

    assert first.snapshot_id == second.snapshot_id
    assert first.to_dict() == second.to_dict()
    assert first.snapshot_id != third.snapshot_id
    assert first.snapshot_id.startswith("accounting-")

