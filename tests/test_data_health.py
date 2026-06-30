from __future__ import annotations

from pathlib import Path

from schemas.market_data import Bar
from services.data_health import DataHealthAuditor
from services.market_store import MarketStore


def _bar(ts: str, *, o: float, h: float, l: float, c: float, v: float, provider: str) -> Bar:
    return Bar(
        symbol="GOLD",
        timeframe="5m",
        timestamp=ts,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
        provider=provider,
        quality_flags=["test"],
    )


def _store_with(tmp_path: Path, bars: list[Bar]) -> Path:
    db = tmp_path / "market.db"
    MarketStore(db).upsert_bars(bars)
    return db


def test_audit_flags_degenerate_snapshots_and_misalignment(tmp_path: Path) -> None:
    bars = [
        _bar("2026-05-29T01:00:00+00:00", o=4500, h=4505, l=4498, c=4503, v=100, provider="binance_usdm"),
        _bar("2026-05-29T01:02:30Z",      o=4503, h=4503, l=4503, c=4503, v=0,   provider="gold-api.com"),
        _bar("2026-05-29T01:05:00+00:00", o=4503, h=4510, l=4501, c=4509, v=200, provider="binance_usdm"),
    ]
    db = _store_with(tmp_path, bars)
    result = DataHealthAuditor(db_path=db).run("2026-05-29")

    assert result["summary"]["total_bars"] == 3
    assert result["summary"]["degenerate_snapshot_count"] == 1
    assert result["summary"]["misaligned_count"] == 1
    codes = {i["code"] for i in result["issues"]}
    assert "degenerate_snapshots_in_bars_table" in codes
    assert "misaligned_timestamps" in codes
    assert result["status"] in {"warn", "error"}


def test_audit_flags_provider_conflicts(tmp_path: Path) -> None:
    bars = [
        _bar("2026-05-29T01:00:00+00:00", o=4500, h=4505, l=4498, c=4503, v=100, provider="binance_usdm"),
        _bar("2026-05-29T01:00:30Z",      o=4503, h=4503, l=4503, c=4503, v=0,   provider="gold-api.com"),
    ]
    db = _store_with(tmp_path, bars)
    result = DataHealthAuditor(db_path=db).run("2026-05-29")
    assert result["summary"]["provider_conflict_count"] == 1
    sample = result["provider_conflicts"][0]
    assert {sample["a"]["provider"], sample["b"]["provider"]} == {"binance_usdm", "gold-api.com"}


def test_audit_detects_data_gap(tmp_path: Path) -> None:
    bars = [
        _bar("2026-05-27T08:50:00+00:00", o=4500, h=4501, l=4499, c=4500, v=10, provider="binance_usdm"),
        _bar("2026-05-28T08:50:00+00:00", o=4391, h=4392, l=4390, c=4391, v=20, provider="binance_usdm"),
    ]
    db = _store_with(tmp_path, bars)
    result = DataHealthAuditor(db_path=db).run("2026-05-28")
    assert len(result["gaps"]) == 1
    gap = result["gaps"][0]
    assert gap["duration_minutes"] == 1440.0
    assert "data_gaps" in {i["code"] for i in result["issues"]}


def test_clean_drops_only_degenerate_rows(tmp_path: Path) -> None:
    bars = [
        _bar("2026-05-29T01:00:00+00:00", o=4500, h=4505, l=4498, c=4503, v=100, provider="binance_usdm"),
        _bar("2026-05-29T01:02:30Z",      o=4503, h=4503, l=4503, c=4503, v=0,   provider="gold-api.com"),
        _bar("2026-05-29T01:03:30Z",      o=4501, h=4501, l=4501, c=4501, v=0,   provider="gold-api.com"),
    ]
    db = _store_with(tmp_path, bars)
    auditor = DataHealthAuditor(db_path=db)

    dry = auditor.clean_degenerate_snapshots(dry_run=True)
    assert dry == {"would_delete": 2, "dry_run": True}
    # nothing actually deleted yet
    assert auditor.run("2026-05-29")["summary"]["degenerate_snapshot_count"] == 2

    deleted = auditor.clean_degenerate_snapshots(dry_run=False)
    assert deleted == {"deleted": 2, "dry_run": False}
    after = auditor.run("2026-05-29")
    assert after["summary"]["degenerate_snapshot_count"] == 0
    assert after["summary"]["total_bars"] == 1


def test_clean_synthetic_drops_only_seed_rows(tmp_path: Path) -> None:
    bars = [
        _bar("2026-05-29T01:00:00+00:00", o=4500, h=4505, l=4498, c=4503, v=100, provider="binance_usdm"),
        _bar("2026-05-29T00:57:13+00:00", o=4499, h=4502, l=4497, c=4501, v=0,   provider="local_synthetic_seed"),
        _bar("2026-05-29T00:52:13+00:00", o=4498, h=4500, l=4496, c=4499, v=0,   provider="local_synthetic_seed"),
    ]
    db = _store_with(tmp_path, bars)
    auditor = DataHealthAuditor(db_path=db)

    dry = auditor.clean_synthetic_seed(dry_run=True)
    assert dry == {"would_delete": 2, "dry_run": True}
    # nothing deleted yet — the seed rows still show as misaligned
    assert auditor.run("2026-05-29")["summary"]["misaligned_count"] == 2

    deleted = auditor.clean_synthetic_seed(dry_run=False)
    assert deleted == {"deleted": 2, "dry_run": False}
    after = auditor.run("2026-05-29")
    assert after["summary"]["total_bars"] == 1  # only the real binance bar remains
    assert after["summary"]["misaligned_count"] == 0
    assert all(p["provider"] != "local_synthetic_seed" for p in after["providers"])


def test_audit_passes_clean_dataset(tmp_path: Path) -> None:
    bars = [
        _bar("2026-05-29T01:00:00+00:00", o=4500, h=4505, l=4498, c=4503, v=100, provider="binance_usdm"),
        _bar("2026-05-29T01:05:00+00:00", o=4503, h=4510, l=4501, c=4509, v=200, provider="binance_usdm"),
        _bar("2026-05-29T01:10:00+00:00", o=4509, h=4512, l=4506, c=4510, v=150, provider="binance_usdm"),
    ]
    db = _store_with(tmp_path, bars)
    result = DataHealthAuditor(db_path=db).run("2026-05-29")
    assert result["summary"]["degenerate_snapshot_count"] == 0
    assert result["summary"]["misaligned_count"] == 0
    assert result["summary"]["gap_count"] == 0
    # status will be "error" only because the latest bar is in the past
    # (test fixtures use 2026-05-29 timestamps), so we just confirm none of
    # the data-integrity issues fired.
    codes = {i["code"] for i in result["issues"]}
    assert "degenerate_snapshots_in_bars_table" not in codes
    assert "misaligned_timestamps" not in codes
    assert "provider_conflicts" not in codes
