from pathlib import Path

from services.broker_receipts import BrokerReceiptImporter
from services.journal_store import load_json


def test_broker_receipt_importer_imports_and_dedupes_receipts(tmp_path: Path):
    root = tmp_path / "outputs"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    receipt = inbox / "receipt-1.json"
    receipt.write_text(
        '{"order_id":"o1","broker_order_id":"mt5-1","status":"filled","fill_price":4572.5,"filled_quantity":1.2,"timestamp":"2026-05-26T01:00:00Z"}\n',
        encoding="utf-8",
    )
    importer = BrokerReceiptImporter(root, {"inbox_dir": str(inbox), "receipt_pattern": "*.json"})

    first = importer.import_pending("2026-05-26")
    second = importer.import_pending("2026-05-26")

    assert first["new_receipts"] == 1
    assert first["total_receipts"] == 1
    assert second["new_receipts"] == 0
    current = load_json(root / "broker_receipts" / "current.json")
    assert current[0]["order_id"] == "o1"
    assert current[0]["status"] == "filled"


def test_broker_receipt_importer_ignores_json_templates(tmp_path: Path):
    root = tmp_path / "outputs"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "ORDER_RECEIPT.json.template").write_text('{"order_id":"template"}\n', encoding="utf-8")

    summary = BrokerReceiptImporter(root, {"inbox_dir": str(inbox), "receipt_pattern": "*.json"}).import_pending("2026-05-26")

    assert summary["new_receipts"] == 0
    assert summary["errors"] == []
    assert load_json(root / "broker_receipts" / "current.json") == []


def test_broker_receipt_importer_records_bad_receipt_errors(tmp_path: Path):
    root = tmp_path / "outputs"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "bad.json").write_text('{"status":"filled"}\n', encoding="utf-8")

    summary = BrokerReceiptImporter(root, {"inbox_dir": str(inbox)}).import_pending("2026-05-26")

    assert summary["new_receipts"] == 0
    assert summary["errors"][0]["source_file"].endswith("bad.json")
