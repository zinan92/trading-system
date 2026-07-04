from pathlib import Path

import pytest

from pipelines.daily import run_daily_pipeline
from services.journal_store import JournalStore, load_json
from services.journal_store import write_json


def test_record_decision_moves_pending_item(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(tmp_path / "market_data.db"))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4568.12")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-05-08T00:00:00+00:00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_DISABLE_DATA_QUALITY_GATE", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_DISABLE_POSITION_GATE", "1")
    run_daily_pipeline("2026-05-08")
    pending = load_json(root / "journal_pending" / "2026-05-08.json")
    ticket_id = pending[0]["ticket_id"]

    record = JournalStore(output_root=root).record_decision(
        run_date="2026-05-08",
        ticket_id=ticket_id,
        decision="skipped",
        notes="test note",
    )

    assert record["ticket_id"] == ticket_id
    assert record["decision_status"] == "skipped"
    decisions = load_json(root / "journal_decisions" / "2026-05-08.json")
    remaining = load_json(root / "journal_pending" / "2026-05-08.json")
    assert any(item["ticket_id"] == ticket_id for item in decisions)
    assert all(item["ticket_id"] != ticket_id for item in remaining)


def test_executed_paper_blocks_daily_risk_cap(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-11"
    ticket = {
        "ticket_id": "ticket_gold_20260511_captest",
        "signal_id": "sig_gold_captest",
        "asset": "GOLD",
        "asset_class": "commodity",
        "action": "prepare_buy",
        "entry_zone": "4550-4590",
        "stop_loss": 4480,
        "targets": [4750],
        "position_size_pct": 50,
        "max_loss_pct": 2.0,
        "order_type": "limit",
        "time_in_force": "day",
        "paper_only": True,
    }
    write_json(root / "trade_tickets" / f"{run_date}.json", [ticket])
    write_json(root / "journal_pending" / f"{run_date}.json", [{**ticket, "journal_id": "j1", "decision_status": "pending_manual_decision"}])
    write_json(root / "journal_decisions" / f"{run_date}.json", [
        {"ticket_id": "old1", "decision_status": "executed_paper", "paper_order": {"order_id": "p1"}, "risk_snapshot": {"max_loss_pct": 3.0}}
    ])
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_paper": True, "ready_for_live": False}])

    with pytest.raises(ValueError, match="daily paper risk cap exceeded"):
        JournalStore(output_root=root).record_decision(run_date, ticket["ticket_id"], "executed_paper")


def test_record_decision_uses_injected_broker_adapter(tmp_path: Path):
    from schemas.market_data import PaperOrder

    root = tmp_path / "outputs"
    run_date = "2026-06-03"
    ticket = {
        "ticket_id": "ticket_gold_20260603_inject",
        "signal_id": "sig_gold_inject",
        "asset": "GOLD",
        "asset_class": "commodity",
        "action": "prepare_buy",
        "entry_zone": "4470-4480",
        "stop_loss": 4450,
        "targets": [4500],
        "position_size_pct": 8,
        "max_loss_pct": 0.5,
        "order_type": "market",
        "time_in_force": "day",
    }
    write_json(root / "trade_tickets" / f"{run_date}.json", [ticket])
    write_json(root / "journal_pending" / f"{run_date}.json", [{**ticket, "journal_id": "j1", "decision_status": "pending_manual_decision"}])
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_paper": True}])

    class _StubAdapter:
        name = "stub"

        def __init__(self):
            self.calls = []

        def submit_order(self, request):
            self.calls.append(request)
            return PaperOrder(order_id="stub_1", ticket_id=request.ticket["ticket_id"], status="filled", requested_price=4475, fill_price=4475, quantity=0.05, filled_at="2026-06-03T00:00:00+00:00", rejection_reason="")

    stub = _StubAdapter()
    record = JournalStore(output_root=root).record_decision(run_date, ticket["ticket_id"], "executed_paper", broker_adapter=stub)

    assert len(stub.calls) == 1  # the injected adapter was used, not the default paper one
    assert record["paper_order"]["order_id"] == "stub_1"


def test_executed_paper_blocks_when_data_source_preflight_fails(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-12"
    ticket = {
        "ticket_id": "ticket_gold_20260512_preflight",
        "signal_id": "sig_gold_preflight",
        "asset": "GOLD",
        "asset_class": "commodity",
        "action": "prepare_buy",
        "entry_zone": "4550-4590",
        "stop_loss": 4480,
        "targets": [4750],
        "position_size_pct": 8,
        "max_loss_pct": 0.5,
        "order_type": "limit",
        "time_in_force": "day",
        "paper_only": True,
    }
    write_json(root / "trade_tickets" / f"{run_date}.json", [ticket])
    write_json(root / "journal_pending" / f"{run_date}.json", [{**ticket, "journal_id": "j1", "decision_status": "pending_manual_decision"}])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4560}])
    write_json(
        root / "data_source_preflight" / f"{run_date}.json",
        [{"status": "fail", "ready_for_paper": False, "ready_for_live": False, "message": "latest bar is synthetic"}],
    )

    with pytest.raises(ValueError, match="data source preflight blocks paper execution"):
        JournalStore(output_root=root).record_decision(run_date, ticket["ticket_id"], "executed_paper")

    assert load_json(root / "paper_orders" / f"{run_date}.json") == []
