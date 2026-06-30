from pathlib import Path

from services.journal_store import load_json, write_json
from services.trade_artifact_repair import TradeArtifactRepair


def test_trade_artifact_repair_backfills_missing_signal_and_ticket(tmp_path: Path):
    root = tmp_path / "outputs"
    strategy = root / "strategies" / "gold_1m_bollinger_reversion"
    run_date = "2026-06-23"
    trade_date = "2026-06-21"
    write_json(
        strategy / "paper_trades" / "current.json",
        [{
            "trade_id": "trade_1",
            "order_id": "paper_1",
            "ticket_id": "ticket_bollinger_reversion_gold_20260621_abc",
            "signal_id": "sig_bollinger_reversion_gold_20260621_abc",
            "signal_regime": "bollinger_reversion",
            "signal_strength": 74,
            "signal_confidence": 66,
            "symbol": "GOLD",
            "side": "short",
            "status": "open",
            "quantity": 0.2,
            "entry_price": 4160,
            "stop_loss": 4240,
            "target": 4000,
            "opened_at": "2026-06-21T02:32:53+00:00",
        }],
    )
    write_json(
        strategy / "paper_orders" / f"{trade_date}.json",
        [{
            "order_id": "paper_1",
            "ticket_id": "ticket_bollinger_reversion_gold_20260621_abc",
            "status": "filled",
            "requested_price": 4160,
            "fill_price": 4159.5,
            "quantity": 0.2,
            "filled_at": "2026-06-21T02:32:53+00:00",
        }],
    )
    write_json(strategy / "signals" / f"{trade_date}.json", [{"signal_id": "existing_watch", "direction": "watch"}])
    write_json(strategy / "trade_tickets" / f"{trade_date}.json", [])

    result = TradeArtifactRepair(root).run(run_date, strategy_id="gold_1m_bollinger_reversion")

    signals = load_json(strategy / "signals" / f"{trade_date}.json")
    tickets = load_json(strategy / "trade_tickets" / f"{trade_date}.json")
    signal = next(item for item in signals if item["signal_id"] == "sig_bollinger_reversion_gold_20260621_abc")
    ticket = next(item for item in tickets if item["ticket_id"] == "ticket_bollinger_reversion_gold_20260621_abc")
    receipt = load_json(strategy / "artifact_repairs" / f"{run_date}.json")[0]

    assert result["summary"]["repaired_signals"] == 1
    assert result["summary"]["repaired_tickets"] == 1
    assert result["summary"]["existing_repaired_signals"] == 1
    assert result["summary"]["existing_repaired_tickets"] == 1
    assert signal["status"] == "repaired"
    assert signal["artifact_provenance"]["status"] == "repaired_from_paper_trade"
    assert signal["artifact_provenance"]["source_trade_id"] == "trade_1"
    assert signal["evidence"][0] == "repaired_from_trade_id=trade_1"
    assert ticket["verdict"] == "repaired"
    assert ticket["artifact_provenance"]["source_order_id"] == "paper_1"
    assert ticket["trade_quality"]["source"] == "repaired_from_paper_trade"
    assert receipt["summary"]["repaired_signals"] == 1


def test_trade_artifact_repair_is_idempotent(tmp_path: Path):
    root = tmp_path / "outputs"
    strategy = root / "strategies" / "gold_5m_fibonacci"
    run_date = "2026-06-23"
    write_json(
        strategy / "paper_trades" / "current.json",
        [{
            "trade_id": "trade_1",
            "order_id": "paper_1",
            "ticket_id": "ticket_fibonacci_gold_20260621_abc",
            "signal_id": "sig_fibonacci_gold_20260621_abc",
            "signal_regime": "fibonacci_pullback",
            "signal_strength": 72,
            "signal_confidence": 65,
            "symbol": "GOLD",
            "side": "long",
            "status": "open",
            "quantity": 0.2,
            "entry_price": 4160,
            "stop_loss": 4080,
            "target": 4320,
            "opened_at": "2026-06-21T02:32:53+00:00",
        }],
    )

    first = TradeArtifactRepair(root).run(run_date, strategy_id="gold_5m_fibonacci")
    second = TradeArtifactRepair(root).run(run_date, strategy_id="gold_5m_fibonacci")
    signals = load_json(strategy / "signals" / "2026-06-21.json")
    tickets = load_json(strategy / "trade_tickets" / "2026-06-21.json")

    assert first["summary"]["repaired_signals"] == 1
    assert first["summary"]["repaired_tickets"] == 1
    assert second["summary"]["repaired_signals"] == 0
    assert second["summary"]["repaired_tickets"] == 0
    assert second["summary"]["existing_repaired_signals"] == 1
    assert second["summary"]["existing_repaired_tickets"] == 1
    assert sum(1 for item in signals if item.get("signal_id") == "sig_fibonacci_gold_20260621_abc") == 1
    assert sum(1 for item in tickets if item.get("ticket_id") == "ticket_fibonacci_gold_20260621_abc") == 1
    assert len(load_json(strategy / "artifact_repairs" / f"{run_date}.json")) == 2
