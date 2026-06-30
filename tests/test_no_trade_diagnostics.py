from pathlib import Path

from services.journal_store import load_json, write_json
from services.no_trade_diagnostics import NoTradeDiagnostics


def test_no_trade_diagnostics_summarizes_global_gate_and_strategy_reasons(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-08"
    write_json(root / "runner_status" / "current.json", [{
        "state": "ok",
        "updated_at": "2026-06-08T08:05:18+00:00",
        "paper_auto_gate_status": "block",
        "paper_auto_gate_allow": False,
        "paper_auto_gate_reasons": ["drawdown breached"],
    }])
    write_json(root / "paper_auto_approval_gate" / "current.json", [{"status": "block", "allow_auto_approve": False, "reasons": ["drawdown breached"]}])
    strategy_root = root / "strategies" / "gold_1m_macd"
    write_json(strategy_root / "signals" / f"{run_date}.json", [{"signal_id": "s1", "status": "no_signal", "direction": "watch", "strength": 0}])
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [])
    write_json(strategy_root / "journal_pending" / f"{run_date}.json", [])
    write_json(strategy_root / "paper_orders" / f"{run_date}.json", [])
    write_json(strategy_root / "paper_trades" / "current.json", [{"trade_id": "tr1", "status": "open", "side": "long"}])
    write_json(strategy_root / "paper_reconciliation" / f"{run_date}.json", [{"status": "fail"}])

    result = NoTradeDiagnostics(root).run(run_date)
    strategy = result["strategies"][0]

    assert result["runner"]["state"] == "ok"
    assert result["global_auto_gate"]["status"] == "block"
    assert strategy["namespace"] == "gold_1m_macd"
    assert strategy["signal_status"] == "no_signal"
    assert strategy["open_trades"] == 1
    assert "strategy emitted no_signal" in strategy["reasons"]
    assert "paper reconciliation fail" in strategy["reasons"]
    saved = load_json(root / "diagnostics" / "no_trade_current.json")[0]
    assert saved["run_date"] == run_date
