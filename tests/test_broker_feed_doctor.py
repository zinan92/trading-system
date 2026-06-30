from pathlib import Path

from services.broker_feed_doctor import BrokerFeedDoctor
from services.journal_store import load_json


def test_broker_feed_doctor_passes_valid_csv(tmp_path: Path):
    feed = tmp_path / "feed"
    feed.mkdir()
    (feed / "XAUUSD_5m.csv").write_text(
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

    result = BrokerFeedDoctor(root, {"input_dir": str(feed), "provider": "mt5_csv"}).run("2026-05-26")

    assert result["status"] == "pass"
    assert result["file_count"] == 1
    assert result["row_count"] == 2
    assert result["latest_timestamp"] == "2026-05-26T01:05:00+00:00"
    assert result["quality_summary"]["pass"] == 1
    assert result["quality_summary"]["non_5m_intervals"] == 0
    assert result["files"][0]["min_close"] == 4571
    assert result["files"][0]["max_close"] == 4572
    assert load_json(root / "broker_feed_doctor" / "current.json")[0]["status"] == "pass"


def test_broker_feed_doctor_fails_bad_csv(tmp_path: Path):
    feed = tmp_path / "feed"
    feed.mkdir()
    (feed / "bad.csv").write_text("timestamp,open,high\n2026-05-26T01:00:00Z,1,2\n", encoding="utf-8")

    result = BrokerFeedDoctor(tmp_path / "outputs", {"input_dir": str(feed)}).run("2026-05-26")

    assert result["status"] == "fail"
    assert "missing price columns" in result["files"][0]["errors"][0]


def test_broker_feed_doctor_fails_gold_price_outside_sanity_range(tmp_path: Path):
    feed = tmp_path / "feed"
    feed.mkdir()
    (feed / "XAUUSD_5m_bad_price.csv").write_text(
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

    result = BrokerFeedDoctor(tmp_path / "outputs", {"input_dir": str(feed), "provider": "mt5_csv"}).run("2026-05-26")

    assert result["status"] == "fail"
    assert result["price_sanity"]["min_price"] == 3000.0
    assert result["quality_summary"]["fail"] == 1
    assert any("outside sanity range" in error for error in result["files"][0]["errors"])


def test_broker_feed_doctor_warns_on_non_5m_or_duplicate_rows(tmp_path: Path):
    feed = tmp_path / "feed"
    feed.mkdir()
    (feed / "XAUUSD_5m_gappy.csv").write_text(
        "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-05-26T01:00:00Z,4570,4572,4569,4571,10",
                "2026-05-26T01:00:00Z,4570,4572,4569,4571,10",
                "2026-05-26T01:10:00Z,4571,4573,4570,4572,12",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = BrokerFeedDoctor(tmp_path / "outputs", {"input_dir": str(feed)}).run("2026-05-26")

    assert result["status"] == "warn"
    assert result["quality_summary"]["duplicate_timestamps"] == 1
    assert result["quality_summary"]["non_5m_intervals"] == 1
    assert result["quality_summary"]["gap_intervals"] == 1


def test_broker_feed_doctor_accepts_mt5_export_headers(tmp_path: Path):
    feed = tmp_path / "feed"
    feed.mkdir()
    (feed / "XAUUSD_5m_mt5.csv").write_text(
        "\n".join(
            [
                "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>",
                "2026.05.26\t09:00:00\t4570\t4572\t4569\t4571\t10",
                "2026.05.26\t09:05:00\t4571\t4573\t4570\t4572\t12",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = BrokerFeedDoctor(tmp_path / "outputs", {"input_dir": str(feed)}).run("2026-05-26")

    assert result["status"] == "pass"
    assert result["row_count"] == 2
    assert result["latest_timestamp"] == "2026-05-26T09:05:00+00:00"


def test_broker_feed_doctor_warns_when_empty(tmp_path: Path):
    result = BrokerFeedDoctor(tmp_path / "outputs", {"input_dir": str(tmp_path / "feed")}).run("2026-05-26")

    assert result["status"] == "warn"
    assert result["file_count"] == 0
    assert Path(result["readme"]).exists()
    assert Path(result["template"]).exists()
    assert "import_official_feed" in Path(result["readme"]).read_text(encoding="utf-8")


def test_broker_feed_doctor_ignores_gap_request_templates(tmp_path: Path):
    feed = tmp_path / "feed"
    feed.mkdir()
    (feed / "NEEDS_XAUUSD_5m_template.csv").write_text("timestamp,open,high,low,close,volume\n# fill me\n", encoding="utf-8")

    result = BrokerFeedDoctor(tmp_path / "outputs", {"input_dir": str(feed)}).run("2026-05-26")

    assert result["status"] == "warn"
    assert result["file_count"] == 0


def test_broker_feed_doctor_ignores_helper_templates(tmp_path: Path):
    feed = tmp_path / "feed"
    feed.mkdir()
    (feed / "README_XAUUSD_5m.md").write_text("readme\n", encoding="utf-8")
    (feed / "XAUUSD_5m.csv.template").write_text("timestamp,open,high,low,close,volume\n2026-05-26T01:00:00Z,1,2,3,4,5\n", encoding="utf-8")

    result = BrokerFeedDoctor(tmp_path / "outputs", {"input_dir": str(feed)}).run("2026-05-26")

    assert result["status"] == "warn"
    assert result["file_count"] == 0
