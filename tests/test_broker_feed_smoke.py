from pathlib import Path

from services.broker_feed_smoke import BrokerFeedSmoke
from services.journal_store import load_json


def test_broker_feed_smoke_imports_official_csv_in_sandbox(tmp_path: Path):
    root = tmp_path / "outputs"
    sandbox = tmp_path / "sandbox"

    result = BrokerFeedSmoke(root, sandbox).run("2026-05-26")

    assert result["status"] == "pass"
    assert result["import_summary"]["imported_rows"] == 3
    assert result["preflight"]["ready_for_live"] is True
    assert result["preflight"]["latest_provider"] == "mt5_csv"
    assert result["preflight"]["official_rows"] == 3
    assert Path(result["sandbox_db"]).exists()
    assert load_json(root / "broker_feed_smoke" / "current.json")[0]["status"] == "pass"
