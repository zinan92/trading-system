import json
import sqlite3
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
