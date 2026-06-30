from pathlib import Path

from pipelines.daily import run_daily_pipeline
from services.reporting import ReportBuilder


def test_build_daily_report(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(tmp_path / "market_data.db"))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4569.34")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-05-10T00:00:00+00:00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")
    run_daily_pipeline("2026-05-10")
    (root / "data_source_preflight").mkdir(parents=True, exist_ok=True)
    (root / "data_source_preflight" / "2026-05-10.json").write_text('[{"latest_price":4569.34,"latest_provider":"env_gold_price","ready_for_paper":true,"ready_for_live":false,"price_sanity":{"passes":true,"min_price":3000,"max_price":6000}}]\n', encoding="utf-8")
    (root / "data_source_lineage").mkdir(parents=True, exist_ok=True)
    (root / "data_source_lineage" / "2026-05-10.json").write_text('[{"truth_level":"public_snapshot"}]\n', encoding="utf-8")
    (root / "official_feed_receipts").mkdir(parents=True, exist_ok=True)
    (root / "official_feed_receipts" / "2026-05-10.json").write_text('[{"status":"warn","official_rows":0,"ready_for_live":false}]\n', encoding="utf-8")
    (root / "operation_runbooks").mkdir(parents=True, exist_ok=True)
    (root / "operation_runbooks" / "2026-05-10.json").write_text('[{"status":"paper_manual_only","permissions":{"paper_manual_review":true,"paper_auto_approve":false,"live_trading":false}}]\n', encoding="utf-8")
    (root / "live_submission_safety").mkdir(parents=True, exist_ok=True)
    (root / "live_submission_safety" / "2026-05-10.json").write_text('[{"status":"pass","blocked_by_activation_gate":true,"network_call_attempted":false,"provider":"oanda_rest","error":"live activation gate is not real_money_ready; real broker submission is blocked"}]\n', encoding="utf-8")
    path = ReportBuilder().build_daily_report("2026-05-10")
    assert Path(path).exists()
    text = Path(path).read_text(encoding="utf-8")
    assert "Signals generated" in text
    assert "Strategy Review Notes" in text
    assert "Data Coverage" in text
    assert "Data Quality Gate" in text
    assert "Market Data Truth" in text
    assert "Bot Supervisor" in text
    assert "Paper Auto Approval Gate" in text
    assert "Official feed receipt" in text
    assert "Operation gate" in text
    assert "Strategy Config" in text
    assert "Broker Readiness" in text
    assert "Live Submission Safety" in text
    assert "Network call attempted: False" in text
    assert "Paper Performance" in text
    assert "Exit Monitor" in text
    assert "Expectancy R" in text
    assert "Learning Ledger" in text
    assert "Learning actions" in text
    assert "Cumulative review days" in text
    assert "Learning state" in text
    assert "Strategy change proposal" in text
    assert "MA windows" in text
    # Offline: the env spot price is stored as a quote, not a bar, so bar
    # coverage shows the synthetic seed; the live price surfaces under Data truth.
    assert "GOLD 5m local_synthetic_seed" in text
    assert "Data coverage: GOLD 5m has" in text
    assert "Data truth:" in text
    assert "Official feed:" in text
    assert "Trading gate:" in text
    assert "Blocked Candidates" in text
    assert (root / "review_notes" / "2026-05-10.md").exists()
    assert (root / "performance" / "2026-05-10.json").exists()
    journal = root / "journals" / "2026-05-10.md"
    assert journal.exists()
    journal_text = journal.read_text(encoding="utf-8")
    assert "Trading Journal - 2026-05-10" in journal_text
    assert "Live submission safety: pass / blocked_by_activation_gate=True / network_call_attempted=False" in journal_text
