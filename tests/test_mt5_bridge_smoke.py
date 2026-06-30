from pathlib import Path

from services.journal_store import load_json
from services.mt5_bridge_smoke import Mt5BridgeSmoke


def test_mt5_bridge_smoke_writes_outbox_and_imports_mock_receipt(tmp_path: Path):
    root = tmp_path / "outputs"
    sandbox = tmp_path / "smoke"

    result = Mt5BridgeSmoke(root, sandbox).run("2026-05-26")

    assert result["status"] == "pass"
    assert result["order"]["status"] == "bridge_dry_run"
    assert len(result["outbox_files"]) == 1
    assert Path(result["mock_receipt_file"]).exists()
    receipts = load_json(root / "broker_receipts" / "current.json")
    assert receipts[0]["order_id"] == result["order"]["order_id"]
    assert receipts[0]["status"] == "filled"
    smoke = load_json(root / "mt5_bridge_smoke" / "current.json")
    assert smoke[0]["status"] == "pass"
