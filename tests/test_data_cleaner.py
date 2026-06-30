from schemas.asset import Asset
from schemas.market_data import Bar
from services.data_cleaner import DataCleaner


def test_cleaner_sorts_dedupes_and_flags_quality():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    rows = [
        Bar("GOLD", "1d", "mock-1d-04", 140, 160, 130, 155, 1, "mock", []),
        Bar("GOLD", "1d", "mock-1d-01", 100, 101, 99, 100, 1, "mock", []),
        Bar("GOLD", "1d", "mock-1d-01", 100, 101, 99, 100, 1, "mock", []),
    ]

    cleaned, manifest = DataCleaner().clean(asset, "1d", rows, "raw.json")

    assert [row.timestamp for row in cleaned] == ["mock-1d-01", "mock-1d-04"]
    assert manifest.duplicate_rows == 1
    assert manifest.missing_bars == 2
    assert manifest.spike_flags == 1
    assert "spike" in cleaned[-1].quality_flags
    assert "missing_bar_before" in cleaned[-1].quality_flags


def test_cleaner_flags_real_5m_iso_gaps():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    rows = [
        Bar("GOLD", "5m", "2026-05-26T10:00:00+00:00", 4500, 4501, 4499, 4500, 1, "gold-api.com", []),
        Bar("GOLD", "5m", "2026-05-26T10:15:00+00:00", 4502, 4503, 4501, 4502, 1, "gold-api.com", []),
    ]

    cleaned, manifest = DataCleaner().clean(asset, "5m", rows, "raw.json")

    assert manifest.missing_bars == 2
    assert "missing_bar_before" in cleaned[-1].quality_flags


def test_cleaner_marks_quote_snapshot_gap_without_counting_missing_bars():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    rows = [
        Bar("GOLD", "5m", "2026-05-26T10:00:00+00:00", 4500, 4501, 4499, 4500, 1, "yahoo_chart:GC=F", ["historical_5m"]),
        Bar("GOLD", "5m", "2026-05-26T11:00:00+00:00", 4502, 4503, 4501, 4502, 0, "gold-api.com", ["live_snapshot", "quote_derived_bar"]),
    ]

    cleaned, manifest = DataCleaner().clean(asset, "5m", rows, "raw.json")

    assert manifest.missing_bars == 0
    assert "quote_snapshot_gap_before" in cleaned[-1].quality_flags
    assert "missing_bar_before" not in cleaned[-1].quality_flags


def test_cleaner_treats_gold_5m_maintenance_gap_as_market_closure():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    rows = [
        Bar("GOLD", "5m", "2026-05-19T20:55:00+00:00", 4485, 4486, 4484, 4485.4, 1, "yahoo_chart:GC=F", []),
        Bar("GOLD", "5m", "2026-05-19T22:00:00+00:00", 4486, 4488, 4485, 4487.2, 1, "yahoo_chart:GC=F", []),
    ]

    cleaned, manifest = DataCleaner().clean(asset, "5m", rows, "raw.json")

    assert manifest.missing_bars == 0
    assert "market_closure_before" in cleaned[-1].quality_flags
    assert "missing_bar_before" not in cleaned[-1].quality_flags


def test_cleaner_treats_gold_5m_weekend_gap_as_market_closure():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    rows = [
        Bar("GOLD", "5m", "2026-05-22T20:55:00+00:00", 4520, 4522, 4519, 4521, 1, "yahoo_chart:GC=F", []),
        Bar("GOLD", "5m", "2026-05-25T13:00:00+00:00", 4570, 4572, 4569, 4571, 1, "gold-api.com", []),
    ]

    cleaned, manifest = DataCleaner().clean(asset, "5m", rows, "raw.json")

    assert manifest.missing_bars == 0
    assert "market_closure_before" in cleaned[-1].quality_flags
