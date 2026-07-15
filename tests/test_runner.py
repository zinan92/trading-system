from pathlib import Path

from pipelines.runner import run_runner_once
from services.journal_store import load_json
from services.runner_status import RunnerStatusStore


def test_runner_once_writes_status(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(tmp_path / "market_data.db"))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4567.01")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-07-02T00:00:00+00:00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")
    monkeypatch.delenv("TRADING_ORCHESTRATOR_LIVE_ENV", raising=False)
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)

    # This test exercises the no-public-data cold start (public_5m_rows == 0,
    # data_source_status == "fail"). Block outbound HTTP so the binance_usdm /
    # yahoo feeds cannot populate real bars and the scenario stays deterministic
    # regardless of whether the test host has network access. The env gold price
    # (read from TRADING_ORCHESTRATOR_GOLD_PRICE) is unaffected and is kept as a
    # quote, never a bar.
    def _no_network(*args, **kwargs):
        raise OSError("network disabled for deterministic offline runner test")

    monkeypatch.setattr("urllib.request.urlopen", _no_network)

    status = run_runner_once("2026-07-02", paper_auto_approve=False, interval_seconds=60)

    assert status["state"] == "ok"
    assert status["collector_count"] == 5
    assert status["oanda_feed_status"] == "skipped"
    assert status["oanda_feed_imported_rows"] == 0
    assert "configure_oanda_v20_in_datafeed" in status["oanda_feed_missing_env"]
    assert status["broker_feed_new_files"] == 0
    assert status["broker_receipt_total_count"] == 0
    assert status["data_source_status"] == "fail"
    assert status["data_source_lineage_status"] == "fail"
    # Offline cold start: the gold-api/env spot price is kept as a quote (not a
    # bar), so the only bars are the synthetic seed — truth level reflects that.
    assert status["data_truth_level"] == "synthetic_seed"
    assert status["data_lineage_ready_for_live"] is False
    assert status["official_5m_rows"] == 0
    assert status["public_5m_rows"] == 0
    assert status["live_submission_safety_status"] == "pass"
    assert status["live_submission_blocked_by_gate"] is True
    assert status["live_submission_network_call_attempted"] is False
    assert status["audit_status"] == "fail"
    assert status["mock_runtime_status"] == "fail"
    assert status["risk_monitor_status"] in {"warn", "block"}
    assert isinstance(status["risk_kill_switch_active"], bool)
    assert status["risk_allow_paper_auto_approve"] is False
    assert status["mock_ready"] is False
    assert status["mock_running"] is True
    assert (root / "runner_status" / "current.json").exists()
    assert (root / "oanda_feed" / "current.json").exists()
    assert (root / "broker_feed_imports" / "current.json").exists()
    assert (root / "broker_receipts" / "summary_current.json").exists()
    assert (root / "data_source_preflight" / "current.json").exists()
    assert (root / "data_source_lineage" / "current.json").exists()
    assert (root / "live_submission_safety" / "current.json").exists()
    assert (root / "audits" / "current.json").exists()
    assert (root / "health" / "current.json").exists()
    assert (root / "mock_runtime" / "current.json").exists()
    history = load_json(root / "runner_status" / "2026-07-02.json")
    assert history[-1]["state"] == "ok"


def test_runner_status_store_current_empty_when_missing(tmp_path: Path):
    assert RunnerStatusStore(tmp_path / "outputs").current() == {}
