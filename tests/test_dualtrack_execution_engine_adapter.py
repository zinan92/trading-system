from __future__ import annotations

from pathlib import Path

import pytest

from services.dualtrack_execution_adapter import (
    ExecutionEngineAdapter,
    LegacyPaperExecutionAdapter,
    build_execution_engine_adapter,
)
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


def _entry() -> dict:
    return {
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T01:02:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 95.0,
        "tp": 110.0,
        "source": "adapter_contract_test",
    }


def test_legacy_adapter_exposes_canonical_execution_snapshot(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)

    fill = adapter.submit_order(_entry())
    snapshot = adapter.snapshot(
        "2026-07-05_DAY",
        mark_price=105.0,
        mark_fresh=True,
        mark_source="canonical_test_feed",
    )

    assert isinstance(adapter, ExecutionEngineAdapter)
    assert fill["event"] == "entry"
    assert snapshot["schema_version"] == "dualtrack-execution-v1"
    assert snapshot["engine"] == "legacy_paper"
    assert snapshot["cycle_id"] == "2026-07-05_DAY"
    assert snapshot["orders"] == []
    assert len(snapshot["fills"]) == 1
    assert len(snapshot["positions"]) == 1
    assert snapshot["positions"][0]["status"] == "open"
    assert snapshot["positions"][0]["unrealized_pnl"] == 50.0
    assert snapshot["pnl"]["realized"] == pytest.approx(-0.05)
    assert snapshot["pnl"]["unrealized"] == 50.0
    assert snapshot["capabilities"]["native_order_lifecycle"] is False


def test_legacy_adapter_market_event_executes_protection_and_reconciles(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)
    adapter.submit_order(_entry())

    event = adapter.process_market_event({
        "cycle_id": "2026-07-05_DAY",
        "ts_event": "2026-07-05T01:03:00+00:00",
        "price": 94.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "canonical_test_feed",
    })
    reconciliation = adapter.reconcile("2026-07-05_DAY")
    snapshot = adapter.snapshot("2026-07-05_DAY", mark_price=94.0, mark_fresh=True)

    assert event["status"] == "triggered"
    assert event["triggered"][0]["event"] == "stop"
    assert snapshot["positions"][0]["status"] == "closed"
    assert snapshot["positions"][0]["remaining_units"] == 0.0
    assert reconciliation["status"] == "ok"
    assert reconciliation["issues"] == []


def test_legacy_adapter_rejects_untrusted_market_event(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)

    with pytest.raises(ValueError, match="synthetic market data is forbidden"):
        adapter.process_market_event({
            "cycle_id": "2026-07-05_DAY",
            "ts_event": "2026-07-05T01:03:00+00:00",
            "price": 94.0,
            "fresh": True,
            "is_synthetic": True,
            "source": "synthetic_seed",
        })


def test_adapter_factory_fails_closed_for_unimplemented_nautilus_engine(tmp_path: Path) -> None:
    assert build_execution_engine_adapter(tmp_path / "outputs", engine="legacy_paper").name == "legacy_paper"

    with pytest.raises(RuntimeError, match="Nautilus adapter spike is not enabled"):
        build_execution_engine_adapter(tmp_path / "outputs", engine="nautilus")
