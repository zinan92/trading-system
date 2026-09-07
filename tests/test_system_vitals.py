"""M0 — system vitals: 'is the machine alive?' as one explicit contract.

These tests are the destructive acceptance for M0: a stopped runner, a stale
feed, an unprotected position, or a strategy that stopped being evaluated must
each flip the contract to `down` AND surface as an alerting health check — the
machine must catch its own death, not wait for a human to notice.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.alert_notifier import AlertNotifier
from services.system_vitals import SystemVitals, vitals_to_health_checks
from services.health_check import HealthCheck
from pipelines.dashboard_server import compact_ops_payload, compact_strategy_payload, compact_trader_payload


class _FakeSender:
    configured = True
    channel = "test"

    def send(self, text: str) -> dict:
        return {"ok": True, "channel": "test"}


def _real_iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat()


def _make_db_real(path: Path, age_minutes: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE bars (symbol TEXT, timeframe TEXT, timestamp TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, provider TEXT, quality_flags TEXT)"
    )
    ts = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).replace(microsecond=0).isoformat()
    con.execute("INSERT INTO bars VALUES ('GOLD','1m',?,1,1,1,1,1,'binance_usdm','')", (ts,))
    con.commit()
    con.close()

RUN_DATE = "2026-06-21"
NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


class _Registry:
    def __init__(self, n: int) -> None:
        self._n = n

    def enabled(self):
        return [object() for _ in range(self._n)]


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _make_db(path: Path, bar_age_minutes: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("DROP TABLE IF EXISTS bars")
    con.execute(
        "CREATE TABLE bars (symbol TEXT, timeframe TEXT, timestamp TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, provider TEXT, quality_flags TEXT)"
    )
    ts = _iso(NOW - timedelta(minutes=bar_age_minutes))
    con.execute("INSERT INTO bars VALUES ('GOLD','1m',?,1,1,1,1,1,'binance_usdm','')", (ts,))
    con.commit()
    con.close()


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _healthy_root(tmp_path: Path, *, runner_age_min=2.0, summary_run_date=RUN_DATE,
                  open_trade=("protected",), strategies_evaluated=2) -> Path:
    root = tmp_path / "outputs"
    park_checked = _iso(NOW - timedelta(minutes=runner_age_min))
    _write(root / "park_strategy" / "safety_evidence.json", {
        "status": "pass",
        "checked_at": park_checked,
        "expires_at": _iso(NOW + timedelta(minutes=5 - runner_age_min)),
    })
    _write(root / "release_gates" / "paper_service_boot_park-paper-runtime_current.json", {
        "status": "pass",
        "checked_at": park_checked,
        "service": "park-paper-runtime",
    })
    # runner heartbeat — fresh
    _write(root / "runner_status" / "current.json", {
        "updated_at": _iso(NOW - timedelta(minutes=runner_age_min)),
        "run_date": RUN_DATE, "state": "ok", "interval_seconds": 300,
    })
    # strategies summary — all evaluated this run_date
    _write(root / "strategies" / "summary_current.json", [{
        "run_date": summary_run_date,
        "generated_at": _iso(NOW - timedelta(minutes=runner_age_min)),
        "strategy_count": strategies_evaluated,
        "strategies": [{"strategy_id": f"s{i}", "status": "ok"} for i in range(strategies_evaluated)],
    }])
    # one open trade with a recent fill
    for sid, kind in zip(["gold_1m_a", "gold_1m_b"], open_trade):
        trade = {
            "trade_id": f"t_{sid}", "status": "open", "opened_at": _iso(NOW - timedelta(hours=1)),
            "stop_loss": 4242.25, "target": 3992.71, "protective_order_missing": None, "exchange_managed": None,
        }
        if kind == "unprotected":
            trade["stop_loss"] = None
            trade["target"] = None
        _write(root / "strategies" / sid / "paper_trades" / "current.json", [trade])
    return root


def _vital(payload: dict, name: str) -> dict:
    return next(v for v in payload["vitals"] if v["name"] == name)


def _vitals(tmp_path, root, *, bar_age_minutes=3.0, registry_n=2, now=NOW):
    db = tmp_path / "market.db"
    _make_db(db, bar_age_minutes)
    return SystemVitals(root, market_db=db, now=now, registry=_Registry(registry_n), park_paper_authority=True).run(RUN_DATE)


def test_all_healthy_machine_is_alive(tmp_path):
    root = _healthy_root(tmp_path, open_trade=("protected", "protected"))
    payload = _vitals(tmp_path, root)
    assert payload["overall"] == "alive"
    assert payload["alive"] is True
    assert _vital(payload, "runner_liveness")["status"] == "up"
    assert "Park Paper" in _vital(payload, "runner_liveness")["message"]
    assert {v["name"] for v in payload["vitals"]} == {
        "data_feed", "runner_liveness", "strategy_evaluation",
        "tp_sl_coverage", "execution_blocker", "no_trade_attribution",
    }
    # contract is persisted for the dashboard to read (no recompute on GET)
    assert (root / "system_vitals" / "current.json").exists()


def test_stale_data_feed_takes_machine_down(tmp_path):
    root = _healthy_root(tmp_path)
    payload = _vitals(tmp_path, root, bar_age_minutes=180)  # 3h old feed
    assert _vital(payload, "data_feed")["status"] == "down"
    assert payload["overall"] == "down"


def test_dead_runner_detected_by_age_not_just_state(tmp_path):
    # state still says "ok" — the bug M0 fixes is that a frozen-but-ok heartbeat
    # from hours ago must NOT read as alive.
    root = _healthy_root(tmp_path, runner_age_min=45)  # >5m Park Paper freshness window
    payload = _vitals(tmp_path, root)
    assert _vital(payload, "runner_liveness")["status"] == "down"
    assert payload["overall"] == "down"
    assert payload["always_on"]["status"] == "BLOCKED_ALWAYS_ON_STALE"
    assert payload["always_on"]["blocks_new_orders"] is True


def test_one_missed_heartbeat_does_not_false_page_before_stale_threshold(tmp_path):
    # The Park Paper contract has a five-minute maximum age.
    root = _healthy_root(tmp_path, runner_age_min=12)
    payload = _vitals(tmp_path, root)
    runner = _vital(payload, "runner_liveness")
    assert runner["status"] == "down"
    job = next(item for item in payload["always_on"]["jobs"] if item["name"] == "park_paper_control")
    assert job["freshness"]["stale_after_seconds"] == 300.0
    assert payload["always_on"]["status"] == "BLOCKED_ALWAYS_ON_STALE"


def test_future_dated_runner_heartbeat_blocks_instead_of_degrading(tmp_path):
    root = _healthy_root(tmp_path)
    _write(root / "park_strategy" / "safety_evidence.json", {
        "status": "pass",
        "checked_at": _iso(NOW + timedelta(minutes=5)),
        "expires_at": _iso(NOW + timedelta(minutes=10)),
    })
    _write(root / "release_gates" / "paper_service_boot_park-paper-runtime_current.json", {
        "status": "pass",
        "checked_at": _iso(NOW + timedelta(minutes=5)),
    })
    _write(root / "runner_status" / "current.json", {
        "updated_at": _iso(NOW + timedelta(minutes=5)),
        "run_date": RUN_DATE,
        "state": "ok",
        "interval_seconds": 300,
    })

    payload = _vitals(tmp_path, root)

    runner = _vital(payload, "runner_liveness")
    assert runner["status"] == "down"
    assert "FUTURE" in runner["message"]
    assert payload["always_on"]["status"] == "BLOCKED_ALWAYS_ON_STALE"
    assert payload["always_on"]["blocks_new_orders"] is True


def test_park_paper_heartbeat_missing_blocks_new_orders(tmp_path):
    root = _healthy_root(tmp_path)
    (root / "park_strategy" / "safety_evidence.json").unlink()
    (root / "runner_status" / "current.json").unlink()
    payload = _vitals(tmp_path, root)

    runner = _vital(payload, "runner_liveness")
    assert runner["status"] == "down"
    assert "missing" in runner["message"]
    assert payload["always_on"]["status"] == "BLOCKED_ALWAYS_ON_STALE"


def test_park_paper_safety_status_blocks_even_when_timestamp_is_fresh(tmp_path):
    root = _healthy_root(tmp_path)
    _write(root / "park_strategy" / "safety_evidence.json", {
        "status": "blocked",
        "checked_at": _iso(NOW - timedelta(minutes=1)),
        "expires_at": _iso(NOW + timedelta(minutes=4)),
    })
    payload = _vitals(tmp_path, root)

    runner = _vital(payload, "runner_liveness")
    assert runner["status"] == "down"
    assert "status is blocked" in runner["message"]


def test_stale_strategy_heartbeat_is_a_canonical_always_on_block(tmp_path):
    root = _healthy_root(tmp_path)
    _write(root / "strategies" / "summary_current.json", [{
        "run_date": RUN_DATE,
        "generated_at": _iso(NOW - timedelta(minutes=16)),
        "strategy_count": 2,
        "strategies": [{"strategy_id": f"s{i}", "status": "ok"} for i in range(2)],
    }])

    payload = _vitals(tmp_path, root, registry_n=2)

    assert _vital(payload, "strategy_evaluation")["status"] == "down"
    assert payload["always_on"]["status"] == "BLOCKED_ALWAYS_ON_STALE"
    blockers = {item["name"] for item in payload["always_on"]["critical_blockers"]}
    assert "strategies" in blockers


def test_future_dated_strategy_heartbeat_blocks_instead_of_degrading(tmp_path):
    root = _healthy_root(tmp_path)
    _write(root / "strategies" / "summary_current.json", [{
        "run_date": RUN_DATE,
        "generated_at": _iso(NOW + timedelta(minutes=5)),
        "strategy_count": 2,
        "strategies": [{"strategy_id": f"s{i}", "status": "ok"} for i in range(2)],
    }])

    payload = _vitals(tmp_path, root, registry_n=2)

    strategy = _vital(payload, "strategy_evaluation")
    assert strategy["status"] == "down"
    assert "FUTURE" in strategy["message"]
    assert payload["always_on"]["status"] == "BLOCKED_ALWAYS_ON_STALE"
    assert payload["always_on"]["blocks_new_orders"] is True


def test_stale_cycle_audit_degrades_but_does_not_block_new_orders(tmp_path):
    root = _healthy_root(tmp_path)
    _write(root / "cycle_audit" / "current.json", {
        "run_date": RUN_DATE,
        "cycle_id": "old",
        "finalized_at": _iso(NOW - timedelta(minutes=60)),
        "status": "completed",
    })

    payload = _vitals(tmp_path, root)

    assert payload["overall"] == "alive"
    assert payload["always_on"]["status"] == "DEGRADED"
    assert payload["always_on"]["blocks_new_orders"] is False
    degraded = {item["name"] for item in payload["always_on"]["degraded_jobs"]}
    assert "cycle_audit" in degraded


def test_future_dated_cycle_audit_heartbeat_blocks_because_clock_is_untrusted(tmp_path):
    root = _healthy_root(tmp_path)
    _write(root / "cycle_audit" / "current.json", {
        "run_date": RUN_DATE,
        "cycle_id": "future",
        "finalized_at": _iso(NOW + timedelta(minutes=5)),
        "status": "completed",
    })

    payload = _vitals(tmp_path, root)

    assert payload["always_on"]["status"] == "BLOCKED_ALWAYS_ON_STALE"
    assert payload["always_on"]["blocks_new_orders"] is True
    blocker = next(item for item in payload["always_on"]["critical_blockers"] if item["name"] == "cycle_audit")
    assert blocker["state"] == "future_timestamp"


def test_unprotected_open_position_flips_tp_sl_vital(tmp_path):
    root = _healthy_root(tmp_path, open_trade=("protected", "unprotected"))
    payload = _vitals(tmp_path, root)
    tp = _vital(payload, "tp_sl_coverage")
    assert tp["status"] == "down"
    assert tp["detail"]["open_positions"] == 2
    assert tp["detail"]["protected"] == 1
    assert payload["overall"] == "down"


def test_strategy_stopped_being_evaluated(tmp_path):
    # 3 strategies enabled, only 2 evaluated in the summary -> coverage gap
    root = _healthy_root(tmp_path, strategies_evaluated=2)
    payload = _vitals(tmp_path, root, registry_n=3)
    assert _vital(payload, "strategy_evaluation")["status"] == "down"
    assert payload["overall"] == "down"


def test_stale_summary_run_date_is_down(tmp_path):
    root = _healthy_root(tmp_path, summary_run_date="2026-06-19")
    payload = _vitals(tmp_path, root, registry_n=2)
    assert _vital(payload, "strategy_evaluation")["status"] == "down"


def test_dead_strategies_job_detected_by_summary_age(tmp_path):
    # run_date stays today, runner stays fresh, but the strategies summary froze
    # 40m ago -> the strategies job died mid-session and must be caught by age.
    root = _healthy_root(tmp_path)
    _write(root / "strategies" / "summary_current.json", [{
        "run_date": RUN_DATE,
        "generated_at": _iso(NOW - timedelta(minutes=40)),
        "strategy_count": 2,
        "strategies": [{"strategy_id": f"s{i}", "status": "ok"} for i in range(2)],
    }])
    payload = _vitals(tmp_path, root, registry_n=2)
    assert _vital(payload, "strategy_evaluation")["status"] == "down"
    assert payload["overall"] == "down"


def test_no_trade_attributed_to_system_when_machine_broken(tmp_path):
    # feed dead -> a quiet day is NOT "market", it's a SYSTEM failure
    root = _healthy_root(tmp_path)
    payload = _vitals(tmp_path, root, bar_age_minutes=180)
    attr = _vital(payload, "no_trade_attribution")
    assert attr["detail"]["attribution"] == "system"
    assert attr["status"] == "down"


def test_quiet_market_is_not_an_error(tmp_path):
    # healthy machine, just no recent trade -> diagnostic warn at most, never error
    # (M4 rule: frequency is a diagnostic, not an SLA)
    root = _healthy_root(tmp_path, open_trade=("protected", "protected"))
    # push last trade far back
    for sid in ["gold_1m_a", "gold_1m_b"]:
        _write(root / "strategies" / sid / "paper_trades" / "current.json", [{
            "trade_id": f"t_{sid}", "status": "open", "opened_at": _iso(NOW - timedelta(hours=20)),
            "stop_loss": 4242.25, "target": 3992.71,
        }])
    payload = _vitals(tmp_path, root)
    attr = _vital(payload, "no_trade_attribution")
    assert attr["status"] != "down"
    assert attr["detail"]["attribution"] in {"market_no_signal", "trading_normally", "risk"}


# ---------------------------------------------------------------------------
# Bug pins (found by adversarial audit): each asserts the FIXED behavior — a
# corrupt/malformed/clock-skewed external artifact must never read as healthy.
# ---------------------------------------------------------------------------

def test_corrupt_paper_trades_does_not_hide_unprotected_position(tmp_path):
    # strat A has a readable, protected position; strat B's file is corrupt and
    # (in reality) holds a naked position we cannot see. A corrupt trades file
    # must NOT silently read as "all protected".
    root = _healthy_root(tmp_path, open_trade=("protected",))
    corrupt = root / "strategies" / "gold_1m_corrupt" / "paper_trades" / "current.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{not valid json", encoding="utf-8")
    payload = _vitals(tmp_path, root)
    tp = _vital(payload, "tp_sl_coverage")
    assert tp["status"] == "down"
    assert "gold_1m_corrupt" in tp["detail"].get("unreadable", [])
    assert payload["overall"] == "down"


def test_corrupt_interval_seconds_does_not_crash_the_health_path(tmp_path):
    root = _healthy_root(tmp_path)
    _write(root / "runner_status" / "current.json", {
        "updated_at": _iso(NOW - timedelta(minutes=2)),
        "run_date": RUN_DATE, "state": "ok", "interval_seconds": "abc",  # malformed
    })
    # must not raise; runner vital still computed
    payload = _vitals(tmp_path, root)
    assert _vital(payload, "runner_liveness")["name"] == "runner_liveness"


def test_future_dated_feed_is_not_treated_as_fresh(tmp_path):
    # a bar 5h in the FUTURE (clock skew / bad import) must not read as "fresh"
    # and mask a dead feed.
    root = _healthy_root(tmp_path)
    payload = _vitals(tmp_path, root, bar_age_minutes=-300)
    assert _vital(payload, "data_feed")["status"] != "up"


def test_unparseable_runner_timestamp_degrades_vital(tmp_path):
    # A present-but-malformed Park Paper timestamp must flag, not silently read up.
    root = _healthy_root(tmp_path)
    _write(root / "park_strategy" / "safety_evidence.json", {
        "status": "pass", "checked_at": "2026-06-21T12:00:00+00:00Z",
        "expires_at": _iso(NOW + timedelta(minutes=5)),
    })
    payload = _vitals(tmp_path, root)
    assert _vital(payload, "runner_liveness")["status"] != "up"


def test_down_vital_becomes_an_alerting_health_check(tmp_path):
    # THE acceptance: a down vital must page through the existing M5 alert pipeline.
    root = _healthy_root(tmp_path, runner_age_min=45)
    payload = _vitals(tmp_path, root)
    checks = vitals_to_health_checks(payload)
    names = {c["name"]: c["status"] for c in checks}
    assert names.get("vitals_runner_liveness") == "error"  # down -> error (in ALERT_STATES)

    # and AlertNotifier actually fires on it
    health_result = {"status": "error", "checks": checks}
    alert = AlertNotifier(root, sender=_FakeSender()).run(RUN_DATE, health_result)
    fired = [e for e in alert["events"] if e["name"] == "vitals_runner_liveness" and e["kind"] == "firing"]
    assert fired, "a dead runner must produce a firing alert event"


# ---------------------------------------------------------------------------
# Branch / edge coverage (each pins a real-money-relevant branch)
# ---------------------------------------------------------------------------

def test_runner_self_reported_failed_takes_machine_down_even_when_fresh(tmp_path):
    root = _healthy_root(tmp_path)
    _write(root / "park_strategy" / "safety_evidence.json", {
        "checked_at": _iso(NOW - timedelta(minutes=2)),
        "expires_at": _iso(NOW + timedelta(minutes=3)), "status": "blocked",
    })
    payload = _vitals(tmp_path, root)
    runner = _vital(payload, "runner_liveness")
    assert runner["status"] == "down"
    assert "blocked" in runner["message"]
    assert payload["overall"] == "down"


def test_data_feed_down_when_no_gold_bars(tmp_path):
    root = _healthy_root(tmp_path)
    missing_db = tmp_path / "absent.db"  # never created
    payload = SystemVitals(root, market_db=missing_db, now=NOW, registry=_Registry(2), park_paper_authority=True).run(RUN_DATE)
    feed = _vital(payload, "data_feed")
    assert feed["status"] == "down"
    assert "no GOLD bars" in feed["message"]
    assert payload["overall"] == "down"


def test_strategy_summary_missing_is_down_when_enabled_and_warn_when_none(tmp_path):
    root = _healthy_root(tmp_path, open_trade=("protected", "protected"))
    (root / "strategies" / "summary_current.json").unlink()
    down = _vitals(tmp_path, root, registry_n=2)
    assert _vital(down, "strategy_evaluation")["status"] == "down"
    assert down["overall"] == "down"
    warn = _vitals(tmp_path, root, registry_n=0)
    assert _vital(warn, "strategy_evaluation")["status"] == "warn"
    assert warn["overall"] == "alive"  # a warn must not flip overall


def test_warn_vital_maps_to_warn_and_does_not_page(tmp_path):
    # healthy machine, no trade sample -> no_trade_attribution warns (diagnostic)
    root = _healthy_root(tmp_path, open_trade=("protected", "protected"))
    for sid in ["gold_1m_a", "gold_1m_b"]:
        (root / "strategies" / sid / "paper_trades" / "current.json").unlink()
    payload = _vitals(tmp_path, root)
    checks = vitals_to_health_checks(payload)
    nt = next(c for c in checks if c["name"] == "vitals_no_trade_attribution")
    assert nt["status"] == "warn"
    alert = AlertNotifier(root, sender=_FakeSender()).run(RUN_DATE, {"status": "warn", "checks": checks})
    assert not [e for e in alert["events"] if e["name"] == "vitals_no_trade_attribution"]


def test_execution_blocker_down_when_demo_reconciliation_unreconciled(tmp_path):
    root = _healthy_root(tmp_path)
    recon = root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text(json.dumps([{"reconciled": False, "drift_count": 1}]), encoding="utf-8")
    payload = _vitals(tmp_path, root)
    assert _vital(payload, "execution_blocker")["status"] == "down"
    assert payload["overall"] == "down"


def test_execution_blocker_down_when_demo_reconciliation_errors_even_if_reconciled_field_is_wrong(tmp_path):
    root = _healthy_root(tmp_path)
    recon = root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text(json.dumps([{
        "reconciled": True,
        "drift_count": 0,
        "error": "TimeoutError: The read operation timed out",
    }]), encoding="utf-8")

    payload = _vitals(tmp_path, root)
    execution = _vital(payload, "execution_blocker")

    assert execution["status"] == "down"
    assert "reconciliation error" in execution["message"]
    assert payload["overall"] == "down"


def test_execution_blocker_names_suspected_naked_position_on_cannot_confirm(tmp_path):
    root = _healthy_root(tmp_path)
    recon = root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text(json.dumps([{
        "reconciled": False,
        "confirmation_status": "cannot_confirm",
        "system_state": "BLOCKED_RECONCILIATION_UNKNOWN",
        "reason_code": "naked_position_suspected",
        "suspected_naked_position": True,
        "drift_count": 0,
        "error": "TimeoutError: venue read timed out",
    }]), encoding="utf-8")

    payload = _vitals(tmp_path, root)
    execution = _vital(payload, "execution_blocker")

    assert execution["status"] == "down"
    assert "suspected naked position" in execution["message"]
    assert execution["detail"]["system_state"] == "BLOCKED_RECONCILIATION_UNKNOWN"


def test_execution_blocker_up_when_no_reconciliation_artifact(tmp_path):
    root = _healthy_root(tmp_path)
    payload = _vitals(tmp_path, root)
    assert _vital(payload, "execution_blocker")["status"] == "up"


def test_exchange_managed_open_position_counts_as_protected(tmp_path):
    root = _healthy_root(tmp_path)
    _write(root / "strategies" / "gold_1m_a" / "paper_trades" / "current.json", [{
        "trade_id": "t_exch", "status": "open", "opened_at": _iso(NOW - timedelta(hours=1)),
        "stop_loss": None, "target": None, "exchange_managed": True,
    }])
    payload = _vitals(tmp_path, root)
    tp = _vital(payload, "tp_sl_coverage")
    assert tp["status"] == "up"
    assert tp["detail"]["missing"] == []


def test_protective_order_missing_still_counts_as_unprotected(tmp_path):
    root = _healthy_root(tmp_path)
    _write(root / "strategies" / "gold_1m_a" / "paper_trades" / "current.json", [{
        "trade_id": "t_naked", "status": "open", "opened_at": _iso(NOW - timedelta(hours=1)),
        "stop_loss": None, "target": None, "protective_order_missing": True,
    }])
    payload = _vitals(tmp_path, root)
    assert _vital(payload, "tp_sl_coverage")["status"] == "down"


def test_exchange_managed_does_not_mask_protective_order_missing(tmp_path):
    root = _healthy_root(tmp_path)
    _write(root / "strategies" / "gold_1m_a" / "paper_trades" / "current.json", [{
        "trade_id": "t_exchange_naked", "status": "open", "opened_at": _iso(NOW - timedelta(hours=1)),
        "stop_loss": 4242.25, "target": 3992.71, "exchange_managed": True, "protective_order_missing": True,
    }])
    payload = _vitals(tmp_path, root)
    tp = _vital(payload, "tp_sl_coverage")
    assert tp["status"] == "down"
    assert "t_exchange_naked" in tp["detail"]["missing"]


def test_no_trade_attribution_warns_when_no_trade_sample_on_healthy_machine(tmp_path):
    root = _healthy_root(tmp_path, open_trade=("protected", "protected"))
    for sid in ["gold_1m_a", "gold_1m_b"]:
        (root / "strategies" / sid / "paper_trades" / "current.json").unlink()
    payload = _vitals(tmp_path, root)
    nt = _vital(payload, "no_trade_attribution")
    assert nt["status"] == "warn"
    assert nt["detail"]["attribution"] == "market_no_signal"
    assert payload["overall"] == "alive"


def test_rollup_alive_only_when_every_vital_up(tmp_path):
    healthy = _healthy_root(tmp_path, open_trade=("protected", "protected"))
    assert _vitals(tmp_path, healthy)["overall"] == "alive"
    broken = _healthy_root(tmp_path, open_trade=("protected", "unprotected"))
    payload = _vitals(tmp_path, broken)
    assert payload["overall"] == "down"
    down_names = [v["name"] for v in payload["vitals"] if v["status"] == "down"]
    assert "tp_sl_coverage" in down_names  # the primary failure drives the rollup
    # and a primary safety failure cascades: no_trade cannot be trusted when the
    # machine is unsafe (attribution -> system).
    assert _vital(payload, "no_trade_attribution")["detail"]["attribution"] == "system"


def test_historical_run_date_feed_reads_up_even_when_stale(tmp_path):
    root = _healthy_root(tmp_path)
    db = tmp_path / "market.db"
    _make_db(db, 300)  # a 5h-old bar
    payload = SystemVitals(root, market_db=db, now=NOW, registry=_Registry(2), park_paper_authority=True).run("2026-06-19")
    feed = _vital(payload, "data_feed")
    assert feed["status"] == "up"
    assert "historical" in feed["message"]


def test_missing_runner_heartbeat_takes_machine_down(tmp_path):
    root = _healthy_root(tmp_path)
    (root / "park_strategy" / "safety_evidence.json").unlink()
    (root / "runner_status" / "current.json").unlink()
    payload = _vitals(tmp_path, root)
    runner = _vital(payload, "runner_liveness")
    assert runner["status"] == "down"
    assert "missing" in runner["message"]
    assert payload["overall"] == "down"


# ---------------------------------------------------------------------------
# Production wiring (the real entry points, not hand-assembled payloads)
# ---------------------------------------------------------------------------

def test_compact_payloads_retain_system_vitals():
    payload = {"system_vitals": {"overall": "down", "alive": False, "vitals": []}, "contract": {}}
    for compactor in (compact_trader_payload, compact_ops_payload, compact_strategy_payload):
        compact = compactor(payload)
        assert "system_vitals" in compact, compactor.__name__
        assert compact["system_vitals"]["overall"] == "down"


def test_healthcheck_includes_vitals_and_drops_execution_blocker(tmp_path):
    root = tmp_path / "outputs"
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text(
        json.dumps({"updated_at": _real_iso(2), "run_date": "2026-06-23", "state": "ok", "interval_seconds": 300}),
        encoding="utf-8",
    )
    db = tmp_path / "market.db"
    _make_db_real(db, 3)
    checks = HealthCheck(root, db)._system_vitals_checks("2026-06-23")
    names = {c["name"] for c in checks}
    assert "vitals_data_feed" in names
    assert "vitals_runner_liveness" in names
    assert "vitals_execution_blocker" not in names  # deduped: active_demo_reconciliation owns it


def test_stopped_machine_fires_alert_through_healthcheck_pipeline(tmp_path):
    # The full M0 acceptance through the REAL production entry point: HealthCheck.run
    # -> AlertNotifier. Stale feed + stale runner (relative to real now).
    root = tmp_path / "outputs"
    run_date = datetime.now(timezone.utc).date().isoformat()
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text(
        json.dumps({"updated_at": _real_iso(180), "run_date": run_date, "state": "ok", "interval_seconds": 300}),
        encoding="utf-8",
    )
    db = tmp_path / "market.db"
    _make_db_real(db, 180)  # 3h-old feed

    result = HealthCheck(root, db).run(run_date)
    assert result["status"] == "error"
    vital_checks = {c["name"]: c["status"] for c in result["checks"] if c["name"].startswith("vitals_")}
    assert vital_checks.get("vitals_data_feed") == "error" or vital_checks.get("vitals_runner_liveness") == "error"
    assert "vitals_execution_blocker" not in vital_checks
    persisted = json.loads((root / "system_vitals" / "current.json").read_text())[0]
    assert persisted["overall"] == "down"

    alert = AlertNotifier(root, sender=_FakeSender()).run(run_date, result)
    fired = [e for e in alert["events"] if e["kind"] == "firing" and e["name"].startswith("vitals_")]
    assert fired, "a stopped machine must page through HealthCheck -> AlertNotifier"


def test_read_only_recompute_does_not_overwrite_runner_owned_artifact(tmp_path):
    # A dashboard GET recomputes vitals but must NOT clobber the runner-owned
    # current.json (esp. when viewing a historical date).
    root = tmp_path / "outputs"
    frozen = root / "system_vitals" / "current.json"
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_text(json.dumps([{"overall": "alive", "runner_owned": True}]), encoding="utf-8")
    SystemVitals(root, tmp_path / "missing.db", now=NOW).run(RUN_DATE, persist=False)
    assert json.loads(frozen.read_text())[0].get("runner_owned") is True  # untouched
    SystemVitals(root, tmp_path / "missing.db", now=NOW).run(RUN_DATE)  # runner path persists
    assert "runner_owned" not in json.loads(frozen.read_text())[0]


def test_strategy_headcount_coverage_is_registry_gated_off_in_prod_wiring(tmp_path):
    # Prod wiring calls SystemVitals WITHOUT a registry, so the evaluated<enabled
    # headcount check is intentionally inert; a partial fleet with a fresh today
    # summary must read 'up' (liveness comes from presence/run_date/generated_at).
    root = _healthy_root(tmp_path)  # summary: 2 strategies, today, fresh generated_at
    payload = SystemVitals(root, tmp_path / "missing.db", now=NOW).run(RUN_DATE)  # registry=None
    assert _vital(payload, "strategy_evaluation")["status"] == "up"


def test_dashboard_snapshot_computes_system_vitals_live_not_frozen(tmp_path):
    # The M0 red line: a dead machine must never show a frozen green on the
    # human-watched dashboard. snapshot must RECOMPUTE vitals on GET.
    from services.dashboard_state import DashboardState

    root = tmp_path / "outputs"
    run_date = datetime.now(timezone.utc).date().isoformat()
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text(
        json.dumps({"updated_at": _real_iso(180), "run_date": run_date, "state": "ok", "interval_seconds": 300}),
        encoding="utf-8",
    )
    # pre-seed a STALE/frozen 'alive' contract — a naive read would serve this
    frozen = root / "system_vitals" / "current.json"
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_text(json.dumps([{"overall": "alive", "alive": True, "vitals": []}]), encoding="utf-8")

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    assert "system_vitals" in state
    assert state["system_vitals"]["overall"] == "down"  # recomputed (no GOLD bars + stale runner), not the frozen 'alive'
