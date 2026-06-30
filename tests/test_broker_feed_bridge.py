from pathlib import Path

from services.broker_feed_bridge import BrokerFeedBridge
from services.journal_store import load_json
from services.market_store import MarketStore


def test_broker_feed_bridge_imports_new_csv_files_once(tmp_path: Path):
    input_dir = tmp_path / "feed"
    input_dir.mkdir()
    csv_path = input_dir / "XAUUSD_5m.csv"
    csv_path.write_text(
        "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-05-26T01:00:00Z,4570,4572,4569,4571,10",
                "2026-05-26T01:05:00Z,4571,4573,4570,4572,12",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    bridge = BrokerFeedBridge(root, db_path, {"input_dir": str(input_dir), "provider": "mt5_csv"})

    first = bridge.import_pending("2026-05-26")
    second = bridge.import_pending("2026-05-26")

    assert first["new_files"] == 1
    assert first["imported_rows"] == 2
    assert first["imported_files"][0]["imported_rows"] == 2
    assert first["total_import_log_entries"] == 1
    assert second["new_files"] == 0
    assert second["skipped_previously_imported"] == 1
    latest_bar = MarketStore(db_path).load_bars("GOLD", "5m", 10)[-1]
    assert latest_bar.provider == "mt5_csv"
    assert "official_broker_feed" in latest_bar.quality_flags
    assert len([item for item in load_json(root / "broker_feed_imports" / "2026-05-26.json") if item["status"] == "imported"]) == 1
    assert load_json(root / "broker_feed_imports" / "2026-05-26.json")[0]["sha256"]


def test_broker_feed_bridge_reimports_same_path_when_content_changes(tmp_path: Path):
    input_dir = tmp_path / "feed"
    input_dir.mkdir()
    csv_path = input_dir / "XAUUSD_5m.csv"
    csv_path.write_text(
        "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-05-26T01:00:00Z,4570,4572,4569,4571,10",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    bridge = BrokerFeedBridge(root, db_path, {"input_dir": str(input_dir), "provider": "mt5_csv"})

    first = bridge.import_pending("2026-05-26")
    csv_path.write_text(
        "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-05-26T01:00:00Z,4570,4572,4569,4571,10",
                "2026-05-26T01:05:00Z,4571,4573,4570,4572,12",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    second = bridge.import_pending("2026-05-26")

    imports = [item for item in load_json(root / "broker_feed_imports" / "2026-05-26.json") if item["status"] == "imported"]
    assert first["new_files"] == 1
    assert second["new_files"] == 1
    assert second["imported_rows"] == 2
    assert len(imports) == 2
    assert imports[0]["sha256"] != imports[1]["sha256"]


def test_broker_feed_bridge_records_bad_csv_errors(tmp_path: Path):
    input_dir = tmp_path / "feed"
    input_dir.mkdir()
    (input_dir / "bad.csv").write_text("timestamp,open,high,low,close\n2026-05-26T01:00:00Z,1,2\n", encoding="utf-8")
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    bridge = BrokerFeedBridge(root, db_path, {"input_dir": str(input_dir), "provider": "mt5_csv"})

    result = bridge.import_pending("2026-05-26")

    assert result["new_files"] == 1
    assert result["imported_rows"] == 0
    assert result["errors"][0]["status"] == "error"


def test_broker_feed_bridge_blocks_price_outside_sanity_range(tmp_path: Path):
    input_dir = tmp_path / "feed"
    input_dir.mkdir()
    (input_dir / "bad_price.csv").write_text(
        "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-05-26T01:00:00Z,2350,2352,2349,2351,10",
                "2026-05-26T01:05:00Z,2351,2353,2350,2352,12",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    bridge = BrokerFeedBridge(root, db_path, {"input_dir": str(input_dir), "provider": "mt5_csv"})

    result = bridge.import_pending("2026-05-26")

    assert result["new_files"] == 1
    assert result["imported_rows"] == 0
    assert "outside sanity range" in result["errors"][0]["error"]
    assert result["errors"][0]["validation"]["status"] == "fail"
    assert MarketStore(db_path).load_bars("GOLD", "5m", 10) == []


def test_broker_feed_bridge_ignores_gap_request_templates(tmp_path: Path):
    input_dir = tmp_path / "feed"
    input_dir.mkdir()
    (input_dir / "NEEDS_XAUUSD_5m_template.csv").write_text("timestamp,open,high,low,close,volume\n# fill me\n", encoding="utf-8")
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    bridge = BrokerFeedBridge(root, db_path, {"input_dir": str(input_dir), "provider": "mt5_csv"})

    result = bridge.import_pending("2026-05-26")

    assert result["new_files"] == 0
    assert result["imported_rows"] == 0


def test_broker_feed_bridge_ignores_helper_templates(tmp_path: Path):
    input_dir = tmp_path / "feed"
    input_dir.mkdir()
    (input_dir / "README_XAUUSD_5m.md").write_text("readme\n", encoding="utf-8")
    (input_dir / "XAUUSD_5m.csv.template").write_text("timestamp,open,high,low,close,volume\n2026-05-26T01:00:00Z,1,2,3,4,5\n", encoding="utf-8")
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    bridge = BrokerFeedBridge(root, db_path, {"input_dir": str(input_dir), "provider": "mt5_csv"})

    result = bridge.import_pending("2026-05-26")

    assert result["new_files"] == 0
    assert result["imported_rows"] == 0
