from __future__ import annotations

from pathlib import Path

import pytest

from services.dualtrack_execution_contract import canonical_market_event, compare_execution_snapshots
from services.dualtrack_shadow_reconciliation import DualTrackShadowReconciler
from services.journal_store import load_json


def _event() -> dict:
    return {
        "cycle_id": "2026-07-10_DAY",
        "ts_event": "2026-07-10T01:01:00+00:00",
        "event_started_at": "2026-07-10T01:01:00+00:00",
        "price": 100.0,
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm",
        "provider": "binance_usdm",
        "symbol": "XAUUSDT",
    }


def _snapshot(engine: str = "legacy_paper", *, realized: float = 1.0, units: float = 1.0) -> dict:
    return {
        "engine": engine,
        "pnl": {"realized": realized, "unrealized": 2.0},
        "fills": [{"fill_id": "fill-1"}],
        "positions": [{"status": "open", "remaining_units": units}],
    }


def test_canonical_market_event_is_stable_and_preserves_provenance() -> None:
    first = canonical_market_event(_event())
    second = canonical_market_event(_event())

    assert first["schema_version"] == "dualtrack-market-event-v1"
    assert first["event_id"] == second["event_id"]
    assert first["source"] == "market_db:binance_usdm"
    assert first["instrument_id"] == "XAUUSDT"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("fresh", False, "stale"),
        ("is_synthetic", True, "synthetic"),
        ("source", "", "source"),
        ("price", 0, "price"),
        ("low", 102, "low exceeds"),
    ],
)
def test_canonical_market_event_fails_closed(field: str, value, message: str) -> None:
    event = _event()
    event[field] = value
    with pytest.raises(ValueError, match=message):
        canonical_market_event(event)


def test_execution_parity_is_exact_and_includes_open_units() -> None:
    report = compare_execution_snapshots(_snapshot(), _snapshot("nautilus", units=0.5), cycle_id="2026-07-10_DAY")

    assert report["status"] == "drift"
    assert report["tolerance"] == {"mode": "exact", "value": 0}
    assert report["differences"] == [
        {"path": "positions[0].remaining_units", "authoritative": 1.0, "candidate": 0.5},
        {"path": "positions.open_units", "authoritative": 1.0, "candidate": 0.5},
    ]


def test_execution_parity_compares_fill_economics_and_position_shape() -> None:
    authoritative = _snapshot()
    authoritative["fills"] = [{"side": "buy", "event": "entry", "price": 100.0, "pnl_units": 1.0}]
    authoritative["positions"] = [{"status": "open", "side": "long", "remaining_units": 1.0}]
    candidate = _snapshot("nautilus")
    candidate["fills"] = [{"side": "buy", "event": "entry", "price": 100.1, "quantity": 1.0}]
    candidate["positions"] = [{"status": "closed", "side": "long", "remaining_units": 0.0}]

    report = compare_execution_snapshots(authoritative, candidate, cycle_id="2026-07-10_DAY")

    assert report["status"] == "drift"
    assert {row["path"] for row in report["differences"]} == {
        "fills[0].price",
        "positions[0].status",
        "positions[0].remaining_units",
        "positions.open_units",
    }


def test_execution_parity_compares_order_terminal_state() -> None:
    authoritative = _snapshot()
    authoritative["orders"] = [{"state": "filled", "side": "buy", "event": "entry", "order_type": "limit", "price": 100, "quantity": 1}]
    candidate = _snapshot("nautilus")
    candidate["orders"] = [{"state": "accepted", "side": "buy", "event": "entry", "order_type": "limit", "price": 100, "quantity": 1}]

    report = compare_execution_snapshots(authoritative, candidate, cycle_id="2026-07-10_DAY")

    assert report["status"] == "drift"
    assert report["differences"] == [{
        "path": "orders[0].state",
        "authoritative": "filled",
        "candidate": "accepted",
    }]


def test_execution_parity_applies_only_declared_venue_precision() -> None:
    authoritative = _snapshot()
    authoritative["orders"] = [{
        "state": "accepted", "side": "buy", "event": "entry", "order_type": "limit",
        "price": 4035.1043, "quantity": 0.19749744,
    }]
    authoritative["fills"] = [{
        "side": "buy", "event": "entry", "price": 4035.1043, "quantity": 0.19749744,
    }]
    authoritative["positions"] = [{"status": "open", "side": "long", "remaining_units": 0.19749744}]
    candidate = _snapshot("nautilus", units=0.197)
    candidate["orders"] = [{
        "state": "accepted", "side": "buy", "event": "entry", "order_type": "limit",
        "price": 4035.10, "quantity": 0.197,
    }]
    candidate["fills"] = [{
        "side": "buy", "event": "entry", "price": 4035.10, "quantity": 0.197,
    }]
    candidate["positions"] = [{"status": "open", "side": "long", "remaining_units": 0.197}]
    candidate["capabilities"] = {
        "comparison_normalization": {
            "mode": "venue_precision",
            "price_decimals": 2,
            "quantity_decimals": 3,
            "money_decimals": 8,
        },
    }

    report = compare_execution_snapshots(authoritative, candidate, cycle_id="2026-07-10_DAY")

    assert report["status"] == "pass"
    assert report["tolerance"] == candidate["capabilities"]["comparison_normalization"]


def test_execution_parity_never_normalizes_semantic_or_money_drift() -> None:
    authoritative = _snapshot(realized=1.00000001)
    authoritative["fills"] = [{"side": "buy", "event": "entry", "price": 100.004, "quantity": 1.0004}]
    candidate = _snapshot("nautilus", realized=1.00000002)
    candidate["fills"] = [{"side": "sell", "event": "entry", "price": 100.00, "quantity": 1.000}]
    candidate["capabilities"] = {
        "comparison_normalization": {
            "mode": "venue_precision",
            "price_decimals": 2,
            "quantity_decimals": 3,
            "money_decimals": 8,
        },
    }

    report = compare_execution_snapshots(authoritative, candidate, cycle_id="2026-07-10_DAY")

    assert report["status"] == "drift"
    assert {row["path"] for row in report["differences"]} == {"pnl.realized", "fills[0].side"}


@pytest.mark.parametrize(
    "normalization",
    [
        {"mode": "venue_precision", "price_decimals": None, "quantity_decimals": 3, "money_decimals": 8},
        {"mode": "venue_precision", "price_decimals": 2, "quantity_decimals": -1, "money_decimals": 8},
        {"mode": "something_else", "price_decimals": 2, "quantity_decimals": 3, "money_decimals": 8},
    ],
)
def test_execution_parity_rejects_invalid_declared_normalization(normalization: dict) -> None:
    candidate = _snapshot("nautilus")
    candidate["capabilities"] = {"comparison_normalization": normalization}

    with pytest.raises(ValueError, match="normalization"):
        compare_execution_snapshots(_snapshot(), candidate, cycle_id="2026-07-10_DAY")


def test_shadow_reconciler_records_blocked_when_candidate_is_not_available(tmp_path: Path) -> None:
    report = DualTrackShadowReconciler(tmp_path / "outputs").record(
        "2026-07-10_DAY",
        authoritative=_snapshot(),
        candidate=None,
        candidate_reason="instrument_definition_missing",
    )

    assert report["status"] == "blocked"
    assert report["blocker"] == "instrument_definition_missing"
    assert load_json(tmp_path / "outputs" / "dualtrack" / "reconciliation" / "current.json") == [report]
