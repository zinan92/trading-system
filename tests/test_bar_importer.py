from pathlib import Path

from pipelines.import_bars import import_bars
from services.bar_importer import BarCsvImporter
from services.market_store import MarketStore


def test_bar_csv_importer_writes_5m_gold_rows(tmp_path: Path):
    csv_path = tmp_path / "xauusd_5m.csv"
    csv_path.write_text(
        "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-05-26T09:00:00+08:00,4570,4572,4569,4571,10",
                "2026-05-26T09:05:00+08:00,4571,4574,4570,4573,12",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    store = MarketStore(tmp_path / "market_data.db")

    result = BarCsvImporter(store).import_csv(csv_path, provider="broker_csv")
    rows = store.load_bars("GOLD", "5m", 10)

    assert result["imported_rows"] == 2
    assert rows[0].timestamp == "2026-05-26T01:00:00+00:00"
    assert rows[-1].close == 4573
    assert rows[-1].provider == "broker_csv"
    assert rows[-1].quality_flags == ["csv_import", "official_broker_feed"]
    assert result["coverage"][0]["rows"] == 2


def test_import_bars_pipeline_logs_import(tmp_path: Path, monkeypatch):
    csv_path = tmp_path / "xauusd_5m.csv"
    csv_path.write_text(
        "\n".join(
            [
                "datetime,open,high,low,close",
                "2026-05-26T01:10:00Z,4573,4575,4572,4574",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(output_root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))

    result = import_bars(csv_path, "GOLD", "5m", "broker_csv", "2026-05-26")

    assert result["local_db"] == str(db_path)
    assert result["imported_rows"] == 1
    assert (output_root / "imports" / "2026-05-26.json").exists()
    assert MarketStore(db_path).load_bars("GOLD", "5m", 1)[0].close == 4574


def test_bar_csv_importer_accepts_mt5_export_headers(tmp_path: Path):
    csv_path = tmp_path / "XAUUSD_5m_mt5.csv"
    csv_path.write_text(
        "\n".join(
            [
                "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>",
                "2026.05.26\t09:00:00\t4570\t4572\t4569\t4571\t10",
                "2026.05.26\t09:05:00\t4571\t4574\t4570\t4573\t12",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    store = MarketStore(tmp_path / "market_data.db")

    result = BarCsvImporter(store).import_csv(csv_path, provider="mt5_csv")
    rows = store.load_bars("GOLD", "5m", 10)

    assert result["imported_rows"] == 2
    assert rows[0].timestamp == "2026-05-26T09:00:00+00:00"
    assert rows[-1].close == 4573
    assert rows[-1].volume == 12
    assert rows[-1].provider == "mt5_csv"
