from schemas.asset import Asset
from schemas.market_data import Bar
from services.data_cleaner import DataCleaner
from services.data_quality_gate import DataQualityGate


def test_data_quality_gate_blocks_high_missing_ratio():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    bars = [
        Bar("GOLD", "5m", "2026-05-26T10:00:00+00:00", 4500, 4501, 4499, 4500, 1, "broker_csv", []),
        Bar("GOLD", "5m", "2026-05-26T10:15:00+00:00", 4502, 4503, 4501, 4502, 1, "broker_csv", []),
    ]
    cleaned, manifest = DataCleaner().clean(asset, "5m", bars, "raw.json")

    result = DataQualityGate({"enabled": True, "min_clean_rows": 2, "max_missing_ratio": 0.1}).evaluate(manifest, cleaned)

    assert result["allows_trading"] is False
    assert result["missing_ratio"] > 0.1
    assert any("missing ratio" in item for item in result["reasons"])


def test_data_quality_gate_disabled_allows_trading():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    bars = [Bar("GOLD", "5m", "2026-05-26T10:00:00+00:00", 4500, 4501, 4499, 4500, 1, "local_synthetic_seed", ["synthetic_seed"])]
    cleaned, manifest = DataCleaner().clean(asset, "5m", bars, "raw.json")

    result = DataQualityGate({"enabled": False}).evaluate(manifest, cleaned)

    assert result["enabled"] is False
    assert result["allows_trading"] is True


def test_data_quality_gate_blocks_latest_synthetic_bar():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    bars = [
        Bar("GOLD", "5m", "2026-05-26T10:00:00+00:00", 4500, 4501, 4499, 4500, 1, "broker_csv", []),
        Bar("GOLD", "5m", "2026-05-26T10:05:00+00:00", 4502, 4503, 4501, 4502, 1, "broker_csv", []),
        Bar("GOLD", "5m", "2026-05-26T10:10:00+00:00", 4503, 4504, 4502, 4503, 1, "local_synthetic_seed", ["synthetic_seed"]),
    ]
    cleaned, manifest = DataCleaner().clean(asset, "5m", bars, "raw.json")

    result = DataQualityGate({"enabled": True, "min_clean_rows": 3, "max_synthetic_ratio": 0.5}).evaluate(manifest, cleaned)

    assert result["allows_trading"] is False
    assert result["latest_provider"] == "local_synthetic_seed"
    assert result["latest_is_synthetic"] is True
    assert any("latest bar is synthetic" in item for item in result["reasons"])


def test_data_quality_gate_can_allow_latest_synthetic_for_explicit_dev_mode():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    bars = [
        Bar("GOLD", "5m", "2026-05-26T10:00:00+00:00", 4500, 4501, 4499, 4500, 1, "broker_csv", []),
        Bar("GOLD", "5m", "2026-05-26T10:05:00+00:00", 4502, 4503, 4501, 4502, 1, "local_synthetic_seed", ["synthetic_seed"]),
    ]
    cleaned, manifest = DataCleaner().clean(asset, "5m", bars, "raw.json")

    result = DataQualityGate(
        {"enabled": True, "min_clean_rows": 2, "max_synthetic_ratio": 0.5, "allow_latest_synthetic": True}
    ).evaluate(manifest, cleaned)

    assert result["allows_trading"] is True
    assert result["latest_is_synthetic"] is True
    assert result["allow_latest_synthetic"] is True


def test_data_quality_gate_allows_quote_derived_latest_bar_for_paper():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    bars = [
        Bar("GOLD", "5m", "2026-05-26T10:00:00+00:00", 4500, 4501, 4499, 4500, 1, "yahoo_chart:GC=F", ["historical_5m"]),
        Bar("GOLD", "5m", "2026-05-26T10:05:00+00:00", 4502, 4503, 4501, 4502, 1, "gold-api.com", ["live_snapshot", "quote_derived_bar", "paper_only_market_data"]),
    ]
    cleaned, manifest = DataCleaner().clean(asset, "5m", bars, "raw.json")

    result = DataQualityGate({"enabled": True, "min_clean_rows": 2, "max_synthetic_ratio": 0.0}).evaluate(manifest, cleaned)

    assert result["allows_trading"] is True
    assert result["latest_provider"] == "gold-api.com"
    assert result["latest_is_quote_derived"] is True


def test_data_quality_gate_does_not_block_spike_flags():
    asset = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    bars = [
        Bar("GOLD", "5m", "2026-05-26T10:00:00+00:00", 4000, 4010, 3990, 4000, 1, "binance_usdm", []),
        Bar("GOLD", "5m", "2026-05-26T10:05:00+00:00", 4500, 4520, 4480, 4500, 1, "binance_usdm", []),
    ]
    cleaned, manifest = DataCleaner().clean(asset, "5m", bars, "raw.json")

    result = DataQualityGate({"enabled": True, "min_clean_rows": 2, "max_spike_flags": 0}).evaluate(manifest, cleaned)

    assert manifest.spike_flags == 1
    assert result["spike_flags"] == 1
    assert result["allows_trading"] is True
    assert result["reasons"] == []
    assert result["thresholds"]["spike_flags_blocking"] is False
