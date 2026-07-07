from __future__ import annotations

from pathlib import Path

import pytest

from services.dualtrack_human import DualTrackHumanEngine
from services.dualtrack_store import DualTrackPlanStore
from services.dualtrack_tiger_human_sync import DualTrackTigerHumanSync
from services.journal_store import load_json, write_json


def _plan(cycle_id: str = "2026-07-05_DAY", direction: str = "long") -> dict:
    if direction == "short":
        return {
            "cycle_id": cycle_id,
            "direction": "short",
            "range": {"low": None, "high": 4200.0},
            "key_levels": [4183.0],
            "invalidation": [{"side": "above", "price": 4200.0, "confirm": "touch"}],
            "confidence": 7,
        }
    return {
        "cycle_id": cycle_id,
        "direction": "long",
        "range": {"low": 4160.0, "high": None},
        "key_levels": [4183.0],
        "invalidation": [{"side": "below", "price": 4160.0, "confirm": "touch"}],
        "confidence": 7,
    }


def _write_order_sync(root: Path, rows: list[dict]) -> None:
    write_json(
        root / "tiger_order_sync" / "current.json",
        [
            {
                "run_date": "2026-07-05",
                "provider": "tiger_openapi",
                "mode": "paper",
                "sync_status": "synced",
                "error": "",
                "open_order_count": 0,
                "filled_order_count": len(rows),
                "exchange_open_orders": [],
                "exchange_filled_orders": rows,
                "checked_at": "2026-07-05T01:03:00+00:00",
            }
        ],
    )


def _filled_order(**overrides) -> dict:
    row = {
        "symbol": "MGC2608",
        "root_symbol": "MGC",
        "order_id": "T100",
        "parent_id": "",
        "side": "BUY",
        "type": "LMT",
        "status": "FILLED",
        "quantity": 2.0,
        "filled_quantity": 2.0,
        "average_fill_price": 4183.2,
        "currency": "USD",
        "filled_at": "2026-07-05T01:02:03+00:00",
        "source": "tiger_filled_orders",
    }
    return {**row, **overrides}


def test_tiger_human_sync_imports_filled_orders_into_human_ledger(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    DualTrackPlanStore(root).save_human_plan(_plan(), now="2026-07-05T00:59:00+00:00")
    _write_order_sync(root, [_filled_order()])

    report = DualTrackTigerHumanSync(root).run("2026-07-05")
    fills = load_json(root / "dualtrack" / "fills" / "2026-07-05_DAY_human.json")
    account = load_json(root / "dualtrack" / "accounts" / "2026-07-05_DAY_human.json")[0]

    assert report["status"] == "synced"
    assert report["imported_count"] == 1
    assert report["duplicate_count"] == 0
    assert report["skipped_count"] == 0
    assert report["safety"]["opens_tiger_sdk_client"] is False
    assert report["safety"]["submits_orders"] is False
    assert len(fills) == 1
    fill = fills[0]
    assert fill["track"] == "human"
    assert fill["source"] == "tiger_openapi_order_sync"
    assert fill["external_order_id"] == "T100"
    assert fill["symbol"] == "MGC2608"
    assert fill["root_symbol"] == "MGC"
    assert fill["side"] == "buy"
    assert fill["order_type"] == "limit"
    assert fill["contracts"] == 2
    assert fill["quantity"] == 2
    assert fill["notional"] == pytest.approx(4183.2 * 2 * 10)
    assert fill["cost"] == pytest.approx(5.4)
    assert fill["realized_pnl"] == pytest.approx(-5.4)
    assert fill["cost_model"]["venue"] == "tiger_mgc"
    assert fill["out_of_plan"] is False
    assert account["realized_pnl"] == pytest.approx(-5.4)
    payload = DualTrackHumanEngine(root).human_payload("2026-07-05_DAY")
    assert payload["fills"][0]["external_order_id"] == "T100"
    assert payload["realized_pnl"] == pytest.approx(-5.4)


def test_tiger_human_sync_is_idempotent_for_same_source_fill(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _write_order_sync(root, [_filled_order()])
    sync = DualTrackTigerHumanSync(root)

    first = sync.run("2026-07-05")
    second = sync.run("2026-07-05")
    fills = load_json(root / "dualtrack" / "fills" / "2026-07-05_DAY_human.json")

    assert first["imported_count"] == 1
    assert second["imported_count"] == 0
    assert second["duplicate_count"] == 1
    assert len(fills) == 1
    assert load_json(root / "dualtrack" / "accounts" / "2026-07-05_DAY_human.json")[0]["realized_pnl"] == pytest.approx(-5.4)


def test_tiger_human_sync_preserves_out_of_plan_flag_and_broker_reported_cost(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    DualTrackPlanStore(root).save_human_plan(_plan(direction="short"), now="2026-07-05T00:59:00+00:00")
    _write_order_sync(root, [_filled_order(commission=6.25)])

    report = DualTrackTigerHumanSync(root).run("2026-07-05")
    fill = load_json(root / "dualtrack" / "fills" / "2026-07-05_DAY_human.json")[0]

    assert report["imported_count"] == 1
    assert fill["out_of_plan"] is True
    assert fill["cost"] == pytest.approx(6.25)
    assert fill["realized_pnl"] == pytest.approx(-6.25)
    assert fill["cost_model"]["cost_source"] == "broker_reported"
    assert fill["cost_model"]["estimated_cost"] == pytest.approx(5.4)


def test_tiger_human_sync_ignores_zero_commission_as_missing_fee(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _write_order_sync(root, [_filled_order(commission=0.0)])

    DualTrackTigerHumanSync(root).run("2026-07-05")
    fill = load_json(root / "dualtrack" / "fills" / "2026-07-05_DAY_human.json")[0]

    assert fill["cost"] == pytest.approx(5.4)
    assert "cost_source" not in fill["cost_model"]


def test_tiger_human_sync_skips_unusable_filled_orders_without_writing_fills(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _write_order_sync(
        root,
        [
            _filled_order(order_id="bad_qty", filled_quantity=0),
            _filled_order(order_id="bad_price", average_fill_price=None),
            _filled_order(order_id="bad_symbol", symbol="ES2608", root_symbol="ES"),
        ],
    )

    report = DualTrackTigerHumanSync(root).run("2026-07-05")

    assert report["status"] == "blocked"
    assert report["imported_count"] == 0
    assert report["skipped_count"] == 3
    assert {row["reason"] for row in report["skipped"]} == {
        "missing_filled_quantity",
        "missing_average_fill_price",
        "unsupported_symbol",
    }
    assert not (root / "dualtrack" / "fills").exists()


def test_tiger_human_sync_blocks_when_order_sync_artifact_is_missing(tmp_path: Path) -> None:
    report = DualTrackTigerHumanSync(tmp_path / "outputs").run("2026-07-05")

    assert report["status"] == "blocked"
    assert report["reason"] == "order_sync_missing"
    assert report["imported_count"] == 0
