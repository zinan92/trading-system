from pathlib import Path

from services.data_gap_repair import DataGapRepairRequest
from services.journal_store import load_json, write_json


def test_data_gap_repair_request_writes_markdown_and_template(tmp_path: Path):
    root = tmp_path / "outputs"
    feed = tmp_path / "feed"
    run_date = "2026-05-26"
    write_json(
        root / "data_gaps" / "current.json",
        [
            {
                "status": "fail",
                "gap_count": 1,
                "estimated_missing_bars": 83,
                "latest_gap": {
                    "from_timestamp": "2026-05-25T19:20:16Z",
                    "to_timestamp": "2026-05-26T02:21:45Z",
                    "gap_minutes": 421.48,
                    "estimated_missing_bars": 83,
                    "from_provider": "gold-api.com",
                    "to_provider": "gold-api.com",
                },
                "gaps": [],
            }
        ],
    )

    result = DataGapRepairRequest(root, feed).build(run_date)

    assert result["status"] == "open"
    assert Path(result["request_markdown"]).exists()
    assert Path(result["csv_template"]).exists()
    assert "timestamp,open,high,low,close,volume" in Path(result["csv_template"]).read_text(encoding="utf-8")
    assert load_json(root / "data_gap_repair_requests" / "current.json")[0]["gap_count"] == 1


def test_data_gap_repair_request_passes_when_no_gaps(tmp_path: Path):
    root = tmp_path / "outputs"
    write_json(root / "data_gaps" / "current.json", [{"status": "pass", "gap_count": 0}])

    result = DataGapRepairRequest(root, tmp_path / "feed").build("2026-05-26")

    assert result["status"] == "pass"
    assert result["csv_template"] == ""
