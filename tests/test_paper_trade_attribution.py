from pathlib import Path

from services.journal_store import load_json, write_json
from services.paper_trade_attribution import PaperTradeAttributor


def test_attributor_backfills_from_ticket_signal_context(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(
        root / "trade_tickets" / f"{run_date}.json",
        [
            {
                "ticket_id": "ticket_1",
                "signal_id": "sig_1",
                "signal_regime": "trend_following",
                "signal_strength": 72,
                "signal_confidence": 66,
                "factor_scores": {"trend": 80},
                "backtest": {"verdict": "supportive"},
                "source_artifacts": ["clean_bars/day/GOLD_5m.json"],
            }
        ],
    )
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"ticket_id": "ticket_1", "signal_id": "sig_1"}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "trade_1", "ticket_id": "ticket_1", "status": "open"}])

    result = PaperTradeAttributor(root).run(run_date)

    trade = load_json(root / "paper_trades" / "current.json")[0]
    assert result["summary"]["updated_open_trades"] == 1
    assert trade["signal_id"] == "sig_1"
    assert trade["signal_regime"] == "trend_following"
    assert trade["signal_strength"] == 72
    assert trade["factor_scores"] == {"trend": 80}
    assert trade["backtest_verdict"] == "supportive"
    assert trade["attribution_status"] == "ticket"


def test_attributor_marks_legacy_journal_only_when_signal_artifact_is_missing(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"ticket_id": "ticket_legacy", "signal_id": "sig_legacy"}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "trade_legacy", "ticket_id": "ticket_legacy", "status": "open", "quality_flags": ["backfilled_from_order"]}])

    PaperTradeAttributor(root).run(run_date)

    trade = load_json(root / "paper_trades" / "current.json")[0]
    assert trade["signal_id"] == "sig_legacy"
    assert trade["signal_regime"] == "unresolved_legacy"
    assert trade["attribution_status"] == "journal_only"
    assert "attribution_journal_only" in trade["quality_flags"]


def test_attributor_does_not_rewrite_existing_attribution(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(
        root / "paper_trades" / "current.json",
        [{"trade_id": "trade_1", "ticket_id": "ticket_1", "signal_id": "sig_1", "signal_regime": "pullback_long", "attribution_status": "ticket"}],
    )

    result = PaperTradeAttributor(root).run(run_date)

    trade = load_json(root / "paper_trades" / "current.json")[0]
    assert result["summary"]["updated_open_trades"] == 0
    assert trade["signal_regime"] == "pullback_long"
