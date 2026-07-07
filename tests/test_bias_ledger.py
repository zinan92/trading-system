from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from schemas.market_data import Bar
from services.bias_ledger import BiasLedger, BiasLedgerBlockedError
from services.completion_audit import CompletionAudit
from services.market_store import MarketStore
from services.market_view_intake import MarketViewIntake


def _iso(hour: int, minute: int = 0) -> str:
    return datetime(2026, 7, 6, hour, minute, tzinfo=timezone.utc).isoformat()


def _view(run_date: str, score: int, issued_at: str, expires_at: str, reference_price: float | None = None) -> dict:
    return {
        "run_date": run_date,
        "generated_at": issued_at,
        "direction_score": score,
        "reference_price": reference_price,
        "expiry": {"expires_at": expires_at, "valid_for_hours": 12},
    }


def _bar(db_path: Path, timestamp: str, close: float) -> None:
    MarketStore(db_path).upsert_bars(
        [Bar("GOLD", "1m", timestamp, close, close, close, close, 1, "fixture", [])]
    )


def test_recorded_view_appends_open_entry_with_reference_price_and_expiry(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    _bar(db_path, _iso(9, 59), 4202.5)

    entry = BiasLedger(root, db_path).append_open_view(
        _view("2026-07-06", 65, _iso(10), _iso(22))
    )

    assert entry["status"] == "open"
    assert entry["price_at_issue"] == 4202.5
    assert entry["expires_at"] == _iso(22)
    assert entry["pending_reason"] is None
    assert BiasLedger(root, db_path).current_entries()[entry["view_id"]]["view_id"] == entry["view_id"]


def test_settle_long_up_is_correct(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    ledger = BiasLedger(root, db_path)
    entry = ledger.append_open_view(_view("2026-07-06", 70, _iso(10), _iso(22), 100))
    _bar(db_path, _iso(22), 101)

    ledger.settle_expired(as_of=_iso(23))

    current = ledger.current_entries()[entry["view_id"]]
    assert current["status"] == "adjudicated"
    assert current["verdict"] == "correct"
    assert current["realized_move_pct"] == pytest.approx(1.0)
    assert current["brier"] == pytest.approx(0.09)


def test_settle_short_up_is_wrong(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    ledger = BiasLedger(root, db_path)
    entry = ledger.append_open_view(_view("2026-07-06", 30, _iso(10), _iso(22), 100))
    _bar(db_path, _iso(22), 101)

    ledger.settle_expired(as_of=_iso(23))

    current = ledger.current_entries()[entry["view_id"]]
    assert current["status"] == "adjudicated"
    assert current["verdict"] == "wrong"
    assert current["brier"] == pytest.approx(0.49)


def test_settle_small_move_is_undecidable_without_brier(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    ledger = BiasLedger(root, db_path)
    entry = ledger.append_open_view(_view("2026-07-06", 70, _iso(10), _iso(22), 100))
    _bar(db_path, _iso(22), 100.2)

    ledger.settle_expired(as_of=_iso(23))

    current = ledger.current_entries()[entry["view_id"]]
    assert current["status"] == "adjudicated"
    assert current["verdict"] == "undecidable"
    assert current["brier"] is None


def test_no_claim_counts_brier_but_not_hit_rate_and_summary_is_recomputed(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    ledger = BiasLedger(root, db_path)
    ledger.append_open_view(_view("2026-07-06", 70, _iso(8), _iso(12), 100))
    ledger.append_open_view(_view("2026-07-06", 30, _iso(9), _iso(13), 100))
    no_claim = ledger.append_open_view(_view("2026-07-06", 50, _iso(10), _iso(14), 100))
    ledger.append_open_view(_view("2026-07-06", 70, _iso(11), _iso(15), 100))
    _bar(db_path, _iso(12), 101)
    _bar(db_path, _iso(13), 101)
    _bar(db_path, _iso(14), 99)
    _bar(db_path, _iso(15), 100.2)

    summary = ledger.settle_expired(as_of=_iso(23))["summary"]

    assert ledger.current_entries()[no_claim["view_id"]]["verdict"] == "no_claim"
    assert ledger.current_entries()[no_claim["view_id"]]["brier"] == pytest.approx(0.25)
    assert summary["total"] == 4
    assert summary["correct"] == 1
    assert summary["wrong"] == 1
    assert summary["no_claim"] == 1
    assert summary["undecidable"] == 1
    assert summary["hit_rate"] == pytest.approx(0.5)
    assert summary["mean_brier"] == pytest.approx((0.09 + 0.49 + 0.25) / 3)


def test_record_blocks_when_expired_missing_reference_price(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    entry = BiasLedger(root, db_path).append_open_view(
        _view("2026-07-05", 65, _iso(8), _iso(9), None)
    )

    with pytest.raises(BiasLedgerBlockedError) as exc:
        MarketViewIntake(root, market_db=db_path).record(
            "2026-07-06",
            "今天偏多，观点有效 12 小时。",
            as_of=_iso(10),
        )

    assert entry["view_id"] in str(exc.value)
    assert "missing_reference_price" in str(exc.value)
    assert not (root / "market_views" / "2026-07-06.json").exists()


def test_record_gate_settles_old_view_then_accepts_new_view(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    old = BiasLedger(root, db_path).append_open_view(
        _view("2026-07-05", 70, _iso(8), _iso(9), 100)
    )
    _bar(db_path, _iso(9), 101)
    _bar(db_path, _iso(9, 30), 4200)

    payload = MarketViewIntake(root, market_db=db_path).record(
        "2026-07-06",
        "今天偏多，观点有效 12 小时。",
        as_of=_iso(10),
    )

    current = BiasLedger(root, db_path).current_entries()
    assert current[old["view_id"]]["status"] == "adjudicated"
    assert current[old["view_id"]]["verdict"] == "correct"
    assert payload["bias_ledger"]["status"] == "open"
    assert current[payload["bias_ledger"]["view_id"]]["run_date"] == "2026-07-06"


def test_missing_settle_bar_becomes_pending_and_blocks_without_verdict(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    ledger = BiasLedger(root, db_path)
    entry = ledger.append_open_view(_view("2026-07-05", 70, _iso(8), _iso(9), 100))
    _bar(db_path, _iso(8, 40), 101)

    ledger.settle_expired(as_of=_iso(10))

    current = ledger.current_entries()[entry["view_id"]]
    assert current["status"] == "pending_data"
    assert current["pending_reason"] == "no_bar_within_tolerance"
    assert current["verdict"] is None
    with pytest.raises(BiasLedgerBlockedError) as exc:
        MarketViewIntake(root, market_db=db_path).record("2026-07-06", "今天偏多。", as_of=_iso(10))
    assert entry["view_id"] in str(exc.value)
    assert "no_bar_within_tolerance" in str(exc.value)


def test_settlement_rerun_is_idempotent(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    ledger = BiasLedger(root, db_path)
    entry = ledger.append_open_view(_view("2026-07-06", 70, _iso(10), _iso(22), 100))
    _bar(db_path, _iso(22), 101)

    ledger.settle_expired(as_of=_iso(23))
    first_rows = (root / "bias_ledger" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    first_current = ledger.current_entries()[entry["view_id"]]
    ledger.settle_expired(as_of=_iso(23))

    assert (root / "bias_ledger" / "ledger.jsonl").read_text(encoding="utf-8").splitlines() == first_rows
    assert ledger.current_entries()[entry["view_id"]] == first_current


def test_adjudicated_entry_is_not_changed_by_later_market_data(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    ledger = BiasLedger(root, db_path)
    entry = ledger.append_open_view(_view("2026-07-06", 70, _iso(10), _iso(22), 100))
    _bar(db_path, _iso(22), 101)
    ledger.settle_expired(as_of=_iso(23))
    settled = ledger.current_entries()[entry["view_id"]]

    _bar(db_path, _iso(22), 98)
    ledger.settle_expired(as_of=_iso(23))

    assert ledger.current_entries()[entry["view_id"]] == settled


def test_bias_ledger_settle_cli_settles_fixture_and_is_idempotent(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    ledger = BiasLedger(root, db_path)
    ledger.append_open_view(_view("2026-07-06", 70, _iso(10), _iso(22), 100))
    _bar(db_path, _iso(22), 101)

    command = [
        sys.executable,
        "-m",
        "pipelines.bias_ledger_settle",
        "--run-date",
        "2026-07-06",
        "--output-root",
        str(root),
        "--market-db",
        str(db_path),
        "--as-of",
        _iso(23),
        "--json",
    ]
    first = subprocess.run(command, check=True, capture_output=True, text=True)
    first_count = len((root / "bias_ledger" / "ledger.jsonl").read_text(encoding="utf-8").splitlines())
    second = subprocess.run(command, check=True, capture_output=True, text=True)

    assert json.loads(first.stdout)["summary"]["adjudicated"] == 1
    assert json.loads(second.stdout)["settled"] == 0
    assert len((root / "bias_ledger" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()) == first_count


def test_completion_audit_human_bias_ledger_pass_and_fail(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    ledger = BiasLedger(root, db_path)
    ledger.append_open_view(_view("2026-07-06", 70, _iso(10), "2099-07-06T22:00:00+00:00", 100))

    passed = CompletionAudit(root, db_path)._human_bias_ledger("2026-07-06")

    assert passed["status"] == "pass"
    expired_root = tmp_path / "expired_outputs"
    expired_ledger = BiasLedger(expired_root, db_path)
    expired_ledger.append_open_view(_view("2026-07-06", 70, _iso(8), _iso(9), None))
    expired_ledger.settle_expired(as_of=_iso(10))
    failed = CompletionAudit(expired_root, db_path)._human_bias_ledger("2026-07-06")

    assert failed["status"] == "fail"
    assert failed["evidence"]["overdue_blockers"][0]["pending_reason"] == "missing_reference_price"
