from pathlib import Path

from services.journal_store import load_json, write_json
from services.live_broker_preflight_report import LiveBrokerPreflightReport


def test_live_broker_preflight_blocks_missing_credentials(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=\nOANDA_ACCOUNT_ID=\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    run_date = "2026-05-26"
    write_json(root / "live_activation" / f"{run_date}.json", [{"status": "blocked", "real_money_ready": False}])
    write_json(root / "live_readiness" / f"{run_date}.json", [{"status": "fail", "live_ready": False}])
    write_json(root / "live_submission_safety" / f"{run_date}.json", [{"status": "pass", "blocked_by_activation_gate": True, "network_call_attempted": False}])

    result = LiveBrokerPreflightReport(root).run(run_date)

    assert result["status"] == "blocked"
    assert result["real_submit_blocked"] is True
    assert result["summary"]["missing_env"] == ["BINANCE_API_KEY", "BINANCE_API_SECRET"]
    checks = {item["name"]: item["status"] for item in result["checks"]}
    assert checks["live_env"] == "fail"
    assert checks["activation_gate"] == "fail"
    assert checks["submission_safety"] == "pass"
    assert load_json(root / "live_broker_preflight" / "current.json")[0]["status"] == "blocked"
    assert (root / "live_broker_preflight" / f"{run_date}.md").exists()


def test_live_broker_preflight_ready_for_mt5_bridge_when_activation_ready(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=token\nOANDA_ACCOUNT_ID=acct\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.setenv("OANDA_API_TOKEN", "token")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "acct")
    run_date = "2026-05-26"
    write_json(root / "live_activation" / f"{run_date}.json", [{"status": "real_money_ready", "real_money_ready": True}])
    write_json(root / "live_readiness" / f"{run_date}.json", [{"status": "pass", "live_ready": True}])
    write_json(root / "live_submission_safety" / f"{run_date}.json", [{"status": "pass", "blocked_by_activation_gate": True, "network_call_attempted": False}])

    result = LiveBrokerPreflightReport(
        root,
    ).run(run_date)

    assert result["status"] in {"blocked", "dry_run_only"}
    assert result["summary"]["secret_hygiene"] in {"pass", "warn"}


def test_live_broker_preflight_treats_binance_as_live_provider(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    env = tmp_path / "live.env"
    env.write_text("BINANCE_API_KEY=key\nBINANCE_API_SECRET=secret\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    run_date = "2026-06-09"
    write_json(root / "live_activation" / f"{run_date}.json", [{"status": "dry_run_ready", "real_money_ready": False}])
    write_json(root / "live_readiness" / f"{run_date}.json", [{"status": "fail", "live_ready": False}])
    write_json(root / "live_submission_safety" / f"{run_date}.json", [{"status": "pass", "blocked_by_activation_gate": True, "network_call_attempted": False}])

    result = LiveBrokerPreflightReport(root).run(run_date)
    checks = {item["name"]: item["status"] for item in result["checks"]}

    assert result["provider"] == "binance_usdm"
    assert checks["broker_provider"] == "pass"
    assert checks["oanda_account"] == "pass"
    assert checks["activation_gate"] == "fail"
