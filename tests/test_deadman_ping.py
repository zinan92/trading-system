import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.deadman_ping import ExternalDeadmanPing
from services.journal_store import load_json, write_json


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _iso(minutes_ago: float = 1.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat()


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _market_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE bars (symbol TEXT, timeframe TEXT, timestamp TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, provider TEXT, quality_flags TEXT)"
    )
    con.execute("INSERT INTO bars VALUES ('GOLD','1m',?,1,1,1,1,1,'binance_usdm','')", (_iso(1),))
    con.commit()
    con.close()


def _healthy_root(tmp_path: Path) -> tuple[Path, Path, str]:
    run_date = datetime.now(timezone.utc).date().isoformat()
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    _market_db(db)
    _write(root / "runner_status" / "current.json", {
        "updated_at": _iso(1),
        "run_date": run_date,
        "state": "ok",
        "interval_seconds": 300,
    })
    _write(root / "strategies" / "summary_current.json", [{
        "run_date": run_date,
        "generated_at": _iso(1),
        "strategy_count": 1,
        "strategies": [{"strategy_id": "gold_1m_chan", "status": "ok"}],
    }])
    write_json(root / "live_reconciliation" / "current.json", [{
        "checked_at": _iso(1),
        "confirmation_status": "confirmed_flat",
        "system_state": "READY",
        "exchange_positions": [],
        "suspected_naked_position": False,
    }])
    return root, db, run_date


def _cloud_paper_snapshot(
    root: Path,
    *,
    cycle_id: str = "2026-07-29_DAY",
    positions: list[dict] | None = None,
    reconciliation: dict | None = None,
) -> Path:
    position_rows = positions or []
    fills = []
    if position_rows:
        position = position_rows[0]
        fills = [{
            "fill_id": "fill-entry-1",
            "order_id": "order-entry-1",
            "trade_id": position["trade_id"],
            "event": "entry",
            "side": "buy",
            "price": position["entry_price"],
            "quantity": position["quantity"],
            "cost": 0,
            "gross_pnl": 0,
            "realized_pnl": 0,
            "ts": position["entry_ts"],
        }]
    write_json(root / "cloud" / "scheduler_ownership" / "current.json", [{
        "status": "active",
        "active_owner_id": "cloud-primary",
        "dual_owner_allowed": False,
        "epoch": 3,
    }])
    write_json(root / "dualtrack" / "strategy_control" / "runtime.json", [{
        "cycle_id": cycle_id,
        "actual_state": "running",
        "strategy_plan_id": "strategy-plan-cloud",
    }])
    path = root / "dualtrack" / "nautilus_authoritative" / "snapshots" / f"{cycle_id}.json"
    write_json(path, [{
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_paper",
        "cycle_id": cycle_id,
        "orders": [],
        "fills": fills,
        "positions": position_rows,
        "account": {
            "starting_cash": 10000,
            "cash": 10000,
            "ending_cash": 10000,
            "equity": 10000,
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "fees": 0,
            "margin": 400,
            "exposure": 4000 if position_rows else 0,
            "slippage": 0,
            "funding": 0,
        },
        "pnl": {"realized": 0, "unrealized": 0, "total": 0},
        "reconciliation": reconciliation or {"status": "ok", "issues": []},
    }])
    return path


def test_cloud_paper_deadman_uses_authoritative_flat_snapshot(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    _cloud_paper_snapshot(root)

    result = ExternalDeadmanPing(root, db, url="", position_url="").run(run_date)

    assert result["severity"] == "normal"
    assert result["exposure"]["has_open_position"] is False
    assert result["exposure"]["position_unknown"] is False
    assert result["exposure"]["source"] == "nautilus_authoritative.snapshot"
    assert result["exposure"]["identity_matches"] is True
    assert result["exposure"]["engine_reconciliation_status"] == "ok"
    assert result["exposure"]["accounting_reconciliation_status"] == "pass"


def test_cloud_paper_deadman_detects_authoritative_open_position(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    _cloud_paper_snapshot(root, positions=[{
        "position_id": "position-1",
        "trade_id": "trade-1",
        "status": "open",
        "side": "long",
        "quantity": 1,
        "remaining_quantity": 1,
        "entry_price": 4000,
        "entry_ts": _iso(2),
    }])

    result = ExternalDeadmanPing(root, db, url="", position_url="").run(run_date)

    assert result["severity"] == "critical"
    assert result["exposure"]["has_open_position"] is True
    assert result["exposure"]["position_unknown"] is False
    assert result["exposure"]["strategy_open_trade_count"] == 1


def test_cloud_paper_deadman_fails_closed_for_stale_snapshot(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    path = _cloud_paper_snapshot(root)
    stale = datetime.now(timezone.utc).timestamp() - 1200
    os.utime(path, (stale, stale))

    result = ExternalDeadmanPing(root, db, url="", position_url="").run(run_date)

    assert result["severity"] == "critical"
    assert result["exposure"]["position_unknown"] is True
    assert result["exposure"]["reconciliation_freshness"]["reason_code"] == "paper_execution_snapshot_stale"


def test_cloud_paper_deadman_fails_closed_for_reconciliation_drift(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    _cloud_paper_snapshot(root, reconciliation={"status": "drift", "issues": [{"code": "mismatch"}]})

    result = ExternalDeadmanPing(root, db, url="", position_url="").run(run_date)

    assert result["severity"] == "critical"
    assert result["exposure"]["position_unknown"] is True
    assert result["exposure"]["engine_reconciliation_status"] == "drift"


def test_cloud_paper_deadman_fails_closed_for_identity_mismatch(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    path = _cloud_paper_snapshot(root)
    rows = load_json(path)
    rows[-1]["cycle_id"] = "2026-07-28_NIGHT"
    write_json(path, rows)

    result = ExternalDeadmanPing(root, db, url="", position_url="").run(run_date)

    assert result["severity"] == "critical"
    assert result["exposure"]["position_unknown"] is True
    assert result["exposure"]["identity_matches"] is False


def test_cloud_paper_deadman_fails_closed_for_malformed_accounting(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    path = _cloud_paper_snapshot(root)
    rows = load_json(path)
    rows[-1]["account"]["equity"] = "not-a-number"
    write_json(path, rows)

    result = ExternalDeadmanPing(root, db, url="", position_url="").run(run_date)

    assert result["severity"] == "critical"
    assert result["exposure"]["position_unknown"] is True
    assert result["exposure"]["accounting_reconciliation_status"] == "missing"
    assert result["exposure"]["accounting_error"].startswith("AccountingContractError:")


def test_deadman_ping_without_url_is_recorded_but_not_sent(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)

    result = ExternalDeadmanPing(root, db, url="", position_url="").run(run_date)

    assert result["status"] == "not_configured"
    assert result["configured"] is False
    assert result["severity"] == "normal"
    saved = load_json(root / "deadman_ping" / "current.json")[0]
    assert saved["status"] == "not_configured"


def test_deadman_ping_sends_flat_heartbeat_to_default_url(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        return _Response()

    result = ExternalDeadmanPing(
        root,
        db,
        url="https://hc-ping.example/flat",
        position_url="https://hc-ping.example/position",
        opener=opener,
        timeout_seconds=3,
    ).run(run_date)

    assert result["status"] == "sent"
    assert result["severity"] == "normal"
    assert calls and calls[0][0].startswith("https://hc-ping.example/flat?")
    assert "position_open=0" in calls[0][0]
    assert calls[0][1] == 3


def test_deadman_ping_uses_position_url_and_critical_severity_when_exchange_position_open(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    write_json(root / "live_reconciliation" / "current.json", [{
        "checked_at": _iso(1),
        "confirmation_status": "confirmed_open",
        "system_state": "PAUSED_POSITION_LIMIT",
        "exchange_positions": [{"symbol": "XAUUSDT", "position_amt": "0.02", "entry_price": 2400}],
        "suspected_naked_position": False,
    }])
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return _Response()

    result = ExternalDeadmanPing(
        root,
        db,
        url="https://hc-ping.example/flat",
        position_url="https://hc-ping.example/position",
        opener=opener,
    ).run(run_date)

    assert result["severity"] == "critical"
    assert result["exposure"]["has_open_position"] is True
    assert calls and calls[0].startswith("https://hc-ping.example/position?")
    assert "position_open=1" in calls[0]


def test_deadman_ping_sends_healthchecks_fail_when_always_on_is_blocked(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    _write(root / "runner_status" / "current.json", {
        "updated_at": _iso(45),
        "run_date": run_date,
        "state": "ok",
        "interval_seconds": 300,
    })
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return _Response()

    result = ExternalDeadmanPing(
        root,
        db,
        url="https://hc-ping.example/default",
        opener=opener,
    ).run(run_date)

    assert result["status"] == "fail_sent"
    assert result["always_on"]["status"] == "BLOCKED_ALWAYS_ON_STALE"
    assert result["ping"]["failure_signal"] is True
    assert result["ping"]["success_ping"] is False
    assert calls and calls[0].startswith("https://hc-ping.example/default/fail?")


def test_deadman_ping_sends_healthchecks_fail_when_loaded_schedule_job_last_run_failed(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return _Response()

    result = ExternalDeadmanPing(
        root,
        db,
        url="https://hc-ping.example/default",
        opener=opener,
        schedule_status_provider=lambda _: {
            "status": "runtime_failed",
            "runtime_failed_jobs": ["com.wendy.trading-orchestrator.dualtrack-live-tick"],
            "healthy_current_count": 4,
            "required_count": 5,
        },
    ).run(run_date)

    assert result["status"] == "fail_sent"
    assert result["schedule_runtime"]["status"] == "runtime_failed"
    assert result["ping"]["failure_signal"] is True
    assert calls and calls[0].startswith("https://hc-ping.example/default/fail?")
    assert "schedule_status=runtime_failed" in calls[0]


def test_deadman_ping_treats_stale_reconciliation_as_possible_position(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    write_json(root / "live_reconciliation" / "current.json", [{
        "checked_at": _iso(30),
        "confirmation_status": "confirmed_open",
        "system_state": "PAUSED_POSITION_LIMIT",
        "exchange_positions": [{"symbol": "XAUUSDT", "position_amt": "0.02", "entry_price": 2400}],
        "suspected_naked_position": False,
    }])
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return _Response()

    result = ExternalDeadmanPing(
        root,
        db,
        url="https://hc-ping.example/default",
        opener=opener,
    ).run(run_date)

    assert result["severity"] == "critical"
    assert result["exposure"]["has_open_position"] is True
    assert result["exposure"]["position_unknown"] is True
    assert result["exposure"]["reconciliation_fresh"] is False
    assert result["exposure"]["reconciliation_freshness"]["reason_code"] == "live_reconciliation_stale"
    assert calls and "position_open=1" in calls[0]


def test_deadman_ping_treats_missing_reconciliation_as_possible_position(tmp_path: Path):
    root, db, run_date = _healthy_root(tmp_path)
    (root / "live_reconciliation" / "current.json").unlink()

    result = ExternalDeadmanPing(root, db, url="", position_url="").run(run_date)

    assert result["severity"] == "critical"
    assert result["exposure"]["has_open_position"] is True
    assert result["exposure"]["position_unknown"] is True
    assert result["exposure"]["reconciliation_freshness"]["reason_code"] == "live_reconciliation_missing"


def test_cloud_deadman_uses_layered_health_and_never_persists_url(
    tmp_path: Path,
    monkeypatch,
):
    root, db, run_date = _healthy_root(tmp_path)
    monkeypatch.setenv("GRIDMIND_RUNTIME_MODE", "cloud")
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return _Response()

    result = ExternalDeadmanPing(
        root,
        db,
        url="https://hc-ping.example/secret-token",
        opener=opener,
        cloud_health_provider=lambda: {
            "status": "degraded",
            "incidents": [
                {
                    "stage": "daily_self_review",
                    "code": "daily_self_review_missing_or_incomplete",
                    "next_action": "Regenerate the review.",
                }
            ],
        },
    ).run(run_date)

    assert result["status"] == "fail_sent"
    assert result["ping"]["target_kind"] == "fail"
    assert calls and "/fail?" in calls[0]
    saved = (
        root / "deadman_ping" / "current.json"
    ).read_text(encoding="utf-8")
    assert "secret-token" not in saved
    assert '"url"' not in saved


def test_cloud_deadman_warning_health_uses_success_endpoint(
    tmp_path: Path,
    monkeypatch,
):
    root, db, run_date = _healthy_root(tmp_path)
    monkeypatch.setenv("GRIDMIND_RUNTIME_MODE", "cloud")
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return _Response()

    result = ExternalDeadmanPing(
        root,
        db,
        url="https://hc-ping.example/deadman",
        opener=opener,
        cloud_health_provider=lambda: {
            "status": "degraded",
            "severity": "warning",
            "incidents": [
                {
                    "stage": "daily_self_review",
                    "code": "daily_self_review_missing_or_incomplete",
                    "severity": "warning",
                }
            ],
        },
    ).run(run_date)

    assert result["status"] == "sent"
    assert result["ping"]["target_kind"] == "success"
    assert calls and "/fail" not in calls[0]


def test_deadman_ping_cli_loads_deadman_url_from_live_env(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("TRADING_ORCHESTRATOR_DEADMAN_URL", raising=False)
    monkeypatch.delenv("TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL", raising=False)
    live_env = tmp_path / "live.env"
    live_env.write_text("TRADING_ORCHESTRATOR_DEADMAN_URL=https://example.invalid/deadman\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(live_env))
    captured = {}

    class FakeDeadmanPing:
        def __init__(self, output_root, market_db, *, url, position_url, timeout_seconds):
            captured["env_url"] = os.getenv("TRADING_ORCHESTRATOR_DEADMAN_URL", "")
            captured["url_arg"] = url
            captured["position_url_arg"] = position_url
            captured["timeout_seconds"] = timeout_seconds

        def run(self, run_date: str, dry_run: bool = False) -> dict:
            return {
                "status": "dry_run_sent" if dry_run else "sent",
                "severity": "normal",
                "configured": True,
                "exposure": {"has_open_position": False},
            }

    import pipelines.deadman_ping as deadman_ping_cli

    monkeypatch.setattr(deadman_ping_cli, "ExternalDeadmanPing", FakeDeadmanPing)
    monkeypatch.setattr(sys, "argv", ["deadman_ping", "--date", "2026-07-08", "--dry-run", "--json"])

    deadman_ping_cli.main()
    capsys.readouterr()

    assert captured["env_url"] == "https://example.invalid/deadman"
    assert captured["url_arg"] is None


def test_deadman_ping_cloud_runtime_uses_systemd_environment(monkeypatch):
    import pipelines.deadman_ping as deadman_ping_cli

    monkeypatch.setenv("GRIDMIND_RUNTIME_MODE", "cloud")
    monkeypatch.setenv(
        "TRADING_ORCHESTRATOR_DEADMAN_URL",
        "https://example.invalid/already-loaded",
    )
    monkeypatch.setattr(
        deadman_ping_cli,
        "apply_live_env",
        lambda: pytest.fail("cloud service reread the protected EnvironmentFile"),
    )

    deadman_ping_cli._load_runtime_env()
