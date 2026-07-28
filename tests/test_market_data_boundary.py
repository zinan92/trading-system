"""Architecture guard: production pipelines may consume market data only via datafeed."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scheduled_pipelines_do_not_import_exchange_collectors_or_market_store():
    forbidden = (
        "services.binance_futures_feed",
        "services.tiger_futures_feed",
        "services.oanda_feed_client",
        "services.market_store",
    )
    compatibility_commands = {
        "backfill_gold.py",
        "import_bars.py",
        "dualtrack_cycle_runner.py",  # explicit temp-db injection for isolated tests
    }
    violations = []
    for path in sorted((ROOT / "pipelines").glob("*.py")):
        if path.name in compatibility_commands:
            continue
        text = path.read_text(encoding="utf-8")
        for module in forbidden:
            if module in text:
                violations.append(f"{path.relative_to(ROOT)} imports {module}")
    assert violations == []


def test_legacy_store_imports_are_frozen_to_explicit_compatibility_seams():
    approved = {
        "pipelines/backfill_gold.py",
        "pipelines/dualtrack_cycle_runner.py",
        "pipelines/import_bars.py",
        "services/bar_importer.py",
        "services/binance_futures_feed.py",
        "services/broker_feed_bridge.py",
        "services/broker_feed_smoke.py",
        "services/market_data_access.py",
        "services/oanda_feed_client.py",
        "services/tiger_futures_feed.py",
        "services/tiger_realtime_validation.py",
    }
    actual = {
        str(path.relative_to(ROOT))
        for folder in (ROOT / "services", ROOT / "pipelines")
        for path in folder.glob("*.py")
        if "from services.market_store import MarketStore" in path.read_text(encoding="utf-8")
    }
    assert actual == approved


def test_sqlite_market_access_is_frozen_to_ownerless_test_and_rehearsal_seams():
    approved = {
        "pipelines/testnet_drill.py",
        "services/connector_config_apply.py",
        "services/cloud_backup.py",
        "services/cloud_paper_preflight.py",
        "services/data_health.py",
        "services/data_integrity_check.py",
        "services/market_store.py",
    }
    actual = {
        str(path.relative_to(ROOT))
        for folder in (ROOT / "services", ROOT / "pipelines")
        for path in folder.glob("*.py")
        if "import sqlite3" in path.read_text(encoding="utf-8")
    }
    assert actual == approved


def test_no_pipeline_contains_market_data_upstream_urls():
    market_data_hosts = (
        "fred.stlouisfed.org",
        "gold-api.com/price",
        "api-fxpractice.oanda.com/v3/instruments",
        "fapi.binance.com/fapi/v1/klines",
    )
    violations = []
    for path in sorted((ROOT / "pipelines").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for host in market_data_hosts:
            if host in text:
                violations.append(f"{path.relative_to(ROOT)} contains {host}")
    assert violations == []
