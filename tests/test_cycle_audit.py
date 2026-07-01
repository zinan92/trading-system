from pathlib import Path

from services.cycle_audit import JsonCycleAuditSink
from services.journal_store import load_json, write_json
from services.multi_strategy_runner import MultiStrategyRunner
from services.strategy_registry import StrategyRegistry


RUN_DATE = "2026-06-30"
STRATEGY_ID = "gold_1m_chan"
TIMEFRAME = "1m"


def _classification() -> dict:
    return {
        "family": "test",
        "style": "audit",
        "directionality": "long_short",
        "frequency_bucket": "medium",
        "holding_period": "intraday",
        "expected_trades_per_day_min": 1,
        "expected_trades_per_day_max": 3,
        "return_profile": "test",
        "risk_profile": "medium",
    }


def _strategy_config() -> dict:
    return {
        STRATEGY_ID: {
            "symbol": "GOLD",
            "timeframe": TIMEFRAME,
            "classification": _classification(),
            "position_gate": {"enabled": False},
            "signal": {"long_strength_min": 0, "long_confidence_min": 0, "short_strength_max": -1, "event_block_below": 0},
        }
    }


def _namespace(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "outputs"
    namespace = base / "strategies" / STRATEGY_ID
    return base, namespace


def _healthy_vitals() -> list[dict]:
    return [
        {"name": "data_feed", "status": "up", "message": "GOLD feed is fresh", "detail": {}},
        {"name": "strategy_evaluation", "status": "up", "message": "enabled strategies evaluated", "detail": {}},
        {"name": "runner_liveness", "status": "up", "message": "runner heartbeat is fresh", "detail": {}},
        {"name": "execution_blocker", "status": "up", "message": "no hard execution blocker detected", "detail": {}},
        {"name": "tp_sl_coverage", "status": "up", "message": "open positions have TP/SL protection", "detail": {}},
    ]


def _write_system_vitals(base: Path, vitals: list[dict] | None = None) -> None:
    write_json(
        base / "system_vitals" / "current.json",
        [{"run_date": RUN_DATE, "checked_at": "2026-06-30T00:02:00+00:00", "overall": "alive", "vitals": vitals or _healthy_vitals()}],
    )


def _seed_common_artifacts(namespace: Path, *, signal_direction: str = "watch", decision: str = "no_go", no_go_reason: str = "signal is not directional") -> None:
    signal = {
        "signal_id": "sig_cycle",
        "asset": "GOLD",
        "status": "new" if signal_direction in {"long", "short"} else "no_signal",
        "direction": signal_direction,
        "strength": 0.8 if signal_direction in {"long", "short"} else 0.0,
        "confidence": 0.7 if signal_direction in {"long", "short"} else 0.0,
        "regime": "test",
    }
    write_json(namespace / "signals" / f"{RUN_DATE}.json", [signal])
    write_json(
        namespace / "decision_snapshots" / f"{RUN_DATE}.json",
        [
            {
                "run_date": RUN_DATE,
                "strategy_id": STRATEGY_ID,
                "timeframe": TIMEFRAME,
                "signal_id": signal["signal_id"],
                "bar_timestamp": "2026-06-30T00:01:00+00:00",
                "signal": {"direction": signal_direction, "strength": signal["strength"], "confidence": signal["confidence"]},
                "final_decision": decision,
                "no_go_reason": "" if decision == "go" else no_go_reason,
                "risk_block": {},
                "direction_bias": {},
                "position_gate": {},
            }
        ],
    )
    write_json(namespace / "risk_blocks" / f"{RUN_DATE}.json", [])
    write_json(namespace / "trade_tickets" / f"{RUN_DATE}.json", [])
    write_json(namespace / "journal_pending" / f"{RUN_DATE}.json", [])
    write_json(namespace / "journal_decisions" / f"{RUN_DATE}.json", [])
    write_json(namespace / "paper_execution_blocks" / f"{RUN_DATE}.json", [])
    write_json(namespace / "paper_reconciliation" / "current.json", [{"run_date": RUN_DATE, "status": "pass"}])
    write_json(
        namespace / "data_source_preflight" / "current.json",
        [
            {
                "run_date": RUN_DATE,
                "status": "pass",
                "message": "official broker market data is active for GOLD 1m",
                "ready_for_paper": True,
                "ready_for_live": True,
                "latest_timestamp": "2026-06-30T00:01:00+00:00",
                "latest_provider": "broker_csv",
                "latest_record_age_minutes": 1,
                "max_public_quote_age_minutes": 15,
                "latest_record_is_fresh": True,
                "data_quality_allows_trading": True,
                "data_quality_reasons": [],
                "price_sanity": {"passes": True},
            }
        ],
    )
    write_json(
        namespace / "risk_monitor" / "current.json",
        [
            {
                "run_date": RUN_DATE,
                "status": "pass",
                "kill_switch_active": False,
                "allow_paper_auto_approve": True,
                "checks": [{"name": "data_source", "status": "pass", "summary": "data ok"}],
                "summary": {"block_reasons": [], "warning_reasons": []},
            }
        ],
    )


def _finalize(base: Path, namespace: Path, *, result: dict | None = None, error: str = "") -> dict:
    sink = JsonCycleAuditSink(namespace, base_output_root=base)
    context = sink.begin(run_date=RUN_DATE, strategy_id=STRATEGY_ID, timeframe=TIMEFRAME)
    return sink.finalize(context, result=result or {"status": "ok", "executed_ticket": None}, error=error)


def test_cycle_audit_records_stale_data_as_no_trade_reason(tmp_path: Path):
    base, namespace = _namespace(tmp_path)
    _write_system_vitals(base, [{**item, "status": "down", "message": "GOLD feed stale: 45.0m old"} if item["name"] == "data_feed" else item for item in _healthy_vitals()])
    _seed_common_artifacts(namespace)
    write_json(
        namespace / "data_source_preflight" / "current.json",
        [
            {
                "run_date": RUN_DATE,
                "status": "fail",
                "message": "latest GOLD quote/bar is stale by 29.0 minutes",
                "ready_for_paper": False,
                "ready_for_live": False,
                "latest_timestamp": "2026-06-30T00:01:00+00:00",
                "latest_record_age_minutes": 29,
                "max_public_quote_age_minutes": 15,
                "latest_record_is_fresh": False,
            }
        ],
    )
    write_json(
        namespace / "risk_monitor" / "current.json",
        [
            {
                "run_date": RUN_DATE,
                "status": "block",
                "kill_switch_active": True,
                "allow_paper_auto_approve": False,
                "checks": [{"name": "data_source", "status": "block", "summary": "stale quote"}],
                "summary": {"block_reasons": ["stale quote"], "warning_reasons": []},
            }
        ],
    )

    record = _finalize(base, namespace)

    assert record["data"]["freshness"] == "stale"
    assert record["data"]["reason_code"] == "data_stale"
    assert record["risk"]["reason_codes"] == ["data_source"]
    assert record["system_state"]["trade_permission"]["status"] == "BLOCKED_DATA_STALE"


def test_cycle_audit_records_no_signal_after_strategy_evaluation(tmp_path: Path):
    base, namespace = _namespace(tmp_path)
    _write_system_vitals(base)
    _seed_common_artifacts(namespace, signal_direction="watch", no_go_reason="signal is not directional")

    record = _finalize(base, namespace)

    assert record["strategy_evaluation"]["strategies"][0]["signal_count"] == 1
    assert record["strategy_evaluation"]["strategies"][0]["signals"][0]["direction"] == "watch"
    assert record["no_ticket_reasons"][0]["reason_code"] == "no_signal"
    assert record["no_ticket_reasons"][0]["reason"] == "signal is not directional"


def test_cycle_audit_records_risk_block_reason_code(tmp_path: Path):
    base, namespace = _namespace(tmp_path)
    _write_system_vitals(base)
    _seed_common_artifacts(namespace, signal_direction="long", no_go_reason="daily risk cap exceeded")
    risk_block = {
        "asset": "GOLD",
        "ticket_id": "ticket_risk_blocked",
        "signal_id": "sig_cycle",
        "reason": "daily risk cap exceeded",
        "portfolio_risk": {"projected_loss_pct": 1.6, "daily_loss_stop_pct": 1.25},
    }
    snapshot = load_json(namespace / "decision_snapshots" / f"{RUN_DATE}.json")[0]
    snapshot["risk_block"] = risk_block
    write_json(namespace / "decision_snapshots" / f"{RUN_DATE}.json", [snapshot])
    write_json(namespace / "risk_blocks" / f"{RUN_DATE}.json", [risk_block])
    write_json(
        namespace / "risk_monitor" / "current.json",
        [
            {
                "run_date": RUN_DATE,
                "status": "block",
                "kill_switch_active": True,
                "allow_paper_auto_approve": False,
                "checks": [{"name": "daily_loss", "status": "block", "summary": "daily risk cap exceeded"}],
                "summary": {"block_reasons": ["daily risk cap exceeded"], "warning_reasons": []},
            }
        ],
    )

    record = _finalize(base, namespace)

    assert record["risk"]["result"] == "fail"
    assert record["risk"]["risk_blocks"][0]["reason_code"] == "portfolio_risk"
    assert record["no_ticket_reasons"][0]["reason_code"] == "portfolio_risk"


def test_cycle_audit_records_live_money_guardrail_from_canonical_permission(tmp_path: Path):
    base, namespace = _namespace(tmp_path)
    _write_system_vitals(base)
    _seed_common_artifacts(namespace, signal_direction="long", decision="go")
    write_json(
        namespace / "live_money_guardrails" / "current.json",
        [
            {
                "run_date": RUN_DATE,
                "status": "BLOCKED_DAILY_LOSS_LIMIT",
                "allows_new_order": False,
                "primary_blocker": {
                    "status": "BLOCKED_DAILY_LOSS_LIMIT",
                    "code": "daily_loss_limit",
                    "source": "live_money_guardrails.daily_loss",
                    "message": "daily live/testnet loss reached limit",
                },
                "limits": {"daily_loss_limit_pct": 1.25},
            }
        ],
    )

    record = _finalize(base, namespace)

    permission = record["system_state"]["trade_permission"]
    why_not = record["execution"]["why_not_executed"]
    assert permission["status"] == "BLOCKED_DAILY_LOSS_LIMIT"
    assert why_not == [
        {
            "reason_code": "daily_loss_limit",
            "status": "BLOCKED_DAILY_LOSS_LIMIT",
            "reason": "daily live/testnet loss reached limit",
            "source": "trade_permission.primary_blocker",
        }
    ]


def test_cycle_audit_records_each_live_money_guardrail_code_from_canonical_permission(tmp_path: Path):
    cases = [
        ("BLOCKED_SINGLE_ORDER_NOTIONAL_LIMIT", "single_order_notional_limit"),
        ("BLOCKED_TOTAL_NOTIONAL_LIMIT", "total_notional_limit"),
        ("BLOCKED_DAILY_TRADE_LIMIT", "daily_trade_limit"),
    ]

    for status, code in cases:
        base, namespace = _namespace(tmp_path / code)
        _write_system_vitals(base)
        _seed_common_artifacts(namespace, signal_direction="long", decision="go")
        write_json(
            namespace / "live_money_guardrails" / "current.json",
            [
                {
                    "run_date": RUN_DATE,
                    "status": status,
                    "allows_new_order": False,
                    "primary_blocker": {
                        "status": status,
                        "code": code,
                        "source": f"live_money_guardrails.{code}",
                        "message": f"{code} blocked",
                    },
                }
            ],
        )

        record = _finalize(base, namespace)

        assert record["system_state"]["trade_permission"]["status"] == status
        assert record["execution"]["why_not_executed"] == [
            {
                "reason_code": code,
                "status": status,
                "reason": f"{code} blocked",
                "source": "trade_permission.primary_blocker",
            }
        ]


def test_cycle_audit_consumes_trade_permission_for_position_limit(tmp_path: Path):
    base, namespace = _namespace(tmp_path)
    _write_system_vitals(base)
    _seed_common_artifacts(namespace)
    write_json(
        namespace / "paper_trades" / "current.json",
        [
            {"trade_id": "t1", "status": "open", "stop_loss": 4490, "target": 4560},
            {"trade_id": "t2", "status": "open", "stop_loss": 4490, "target": 4560},
            {"trade_id": "t3", "status": "open", "stop_loss": 4490, "target": 4560},
        ],
    )

    record = _finalize(base, namespace)

    permission = record["system_state"]["trade_permission"]
    assert permission["status"] == "PAUSED_POSITION_LIMIT"
    assert permission["primary_blocker"]["code"] == "position_limit"


def test_cycle_audit_uses_canonical_permission_for_flat_only_open_position(tmp_path: Path):
    base, namespace = _namespace(tmp_path)
    _write_system_vitals(base)
    _seed_common_artifacts(namespace, signal_direction="long", decision="go")
    ticket = {
        "ticket_id": "ticket_flat_only_block",
        "signal_id": "sig_cycle",
        "asset": "GOLD",
        "action": "prepare_buy",
        "entry_zone": "4520-4530",
        "stop_loss": 4500,
        "targets": [4560],
        "position_size_pct": 8,
        "max_loss_pct": 0.2,
        "order_type": "market",
        "time_in_force": "day",
    }
    write_json(namespace / "trade_tickets" / f"{RUN_DATE}.json", [ticket])
    write_json(namespace / "journal_pending" / f"{RUN_DATE}.json", [{**ticket, "decision_status": "pending_manual_decision"}])
    write_json(
        namespace / "paper_trades" / "current.json",
        [{"trade_id": "open_existing", "status": "open", "stop_loss": 4490, "target": 4560}],
    )
    write_json(
        namespace / "live_reconciliation" / "current.json",
        [
            {
                "run_date": RUN_DATE,
                "reconciled": False,
                "confirmation_status": "confirmed_open",
                "system_state": "PAUSED_POSITION_LIMIT",
                "reason_code": "position_confirmed_open",
                "can_open_new_orders": False,
            }
        ],
    )

    record = _finalize(
        base,
        namespace,
        result={
            "status": "ok",
            "executed_ticket": None,
            "execution_error": "XAUUSDT demo position is already open; strategy demo adapter will not stack exposure",
        },
    )

    permission = record["system_state"]["trade_permission"]
    why_not = record["execution"]["why_not_executed"]
    assert permission["status"] == "PAUSED_POSITION_LIMIT"
    assert permission["allows_new_order_if_signal"] is False
    assert permission["open_trade_limit"] == 1
    assert permission["requires_flat_before_entry"] is True
    assert why_not == [
        {
            "reason_code": "position_limit",
            "status": "PAUSED_POSITION_LIMIT",
            "reason": "1 open trades reaches the effective strategy limit 1",
            "source": "trade_permission.primary_blocker",
        }
    ]
    assert record["system_state"]["live_reconciliation"]["can_open_new_orders"] is False
    assert record["system_state"]["live_reconciliation"]["reason_code"] == "position_confirmed_open"
    assert record["result_ref"]["execution_error"].startswith("XAUUSDT demo position is already open")
    assert all(item["source"] != "live_reconciliation.current" for item in why_not)


def test_cycle_audit_records_reconciliation_cannot_confirm_block(tmp_path: Path):
    base, namespace = _namespace(tmp_path)
    vitals = [
        {**item, "status": "down", "message": "active demo reconciliation cannot confirm venue state", "detail": {"system_state": "BLOCKED_RECONCILIATION_UNKNOWN", "reason_code": "venue_state_unknown"}}
        if item["name"] == "execution_blocker"
        else item
        for item in _healthy_vitals()
    ]
    _write_system_vitals(base, vitals)
    _seed_common_artifacts(namespace)
    write_json(
        namespace / "live_reconciliation" / "current.json",
        [
            {
                "run_date": RUN_DATE,
                "reconciled": False,
                "confirmation_status": "cannot_confirm",
                "system_state": "BLOCKED_RECONCILIATION_UNKNOWN",
                "reason_code": "venue_state_unknown",
                "can_open_new_orders": False,
                "error": "TimeoutError",
            }
        ],
    )

    record = _finalize(base, namespace)

    assert record["reconciliation"]["confirmation_status"] == "cannot_confirm"
    assert record["system_state"]["live_reconciliation"]["system_state"] == "BLOCKED_RECONCILIATION_UNKNOWN"
    assert record["system_state"]["trade_permission"]["status"] == "BLOCKED_RECONCILIATION_UNKNOWN"
    assert record["execution"]["why_not_executed"][0]["reason_code"] == "venue_state_unknown"


def test_cycle_audit_records_protective_failed_block(tmp_path: Path):
    base, namespace = _namespace(tmp_path)
    vitals = [
        {**item, "status": "down", "message": "1 open position lacks TP/SL protection"}
        if item["name"] == "tp_sl_coverage"
        else item
        for item in _healthy_vitals()
    ]
    _write_system_vitals(base, vitals)
    _seed_common_artifacts(namespace)
    write_json(
        namespace / "order_lifecycle" / f"{RUN_DATE}.json",
        [
            {
                "run_date": RUN_DATE,
                "order_id": "order_protective_failed",
                "ticket_id": "ticket_protective",
                "state": "protective_failed",
                "blocked": True,
            }
        ],
    )
    write_json(
        namespace / "demo_order_requests" / f"{RUN_DATE}.json",
        [{"status": "submitted", "broker_response": {"protective_status": "failed"}}],
    )

    record = _finalize(base, namespace)

    assert record["protective"]["status"] == "failed"
    assert record["protective"]["reason_code"] == "protective_failed"
    assert record["system_state"]["trade_permission"]["status"] == "BLOCKED_PROTECTION_MISSING"


def test_cycle_audit_does_not_mark_exchange_managed_trade_covered_without_resting_stop(tmp_path: Path):
    base, namespace = _namespace(tmp_path)
    _write_system_vitals(base)
    _seed_common_artifacts(namespace, signal_direction="long", decision="go")
    ticket = {
        "ticket_id": "ticket_exchange_managed_unverified",
        "signal_id": "sig_cycle",
        "asset": "GOLD",
        "action": "prepare_buy",
        "entry_zone": "4520-4530",
        "stop_loss": 4500,
        "targets": [4560],
        "position_size_pct": 8,
        "max_loss_pct": 0.2,
        "order_type": "market",
        "time_in_force": "day",
    }
    write_json(namespace / "trade_tickets" / f"{RUN_DATE}.json", [ticket])
    write_json(
        namespace / "paper_trades" / "current.json",
        [
            {
                **ticket,
                "trade_id": "trade_exchange_managed_unverified",
                "status": "open",
                "order_id": "order_exchange_managed_unverified",
                "stop_loss": None,
                "target": None,
                "exchange_managed": True,
                "quality_flags": ["exchange_managed", "live_fill"],
            }
        ],
    )
    write_json(
        namespace / "live_reconciliation" / "current.json",
        [
            {
                "reconciled": False,
                "confirmation_status": "confirmed_drift",
                "system_state": "BLOCKED_NAKED_POSITION_SUSPECTED",
                "reason_code": "naked_position_suspected",
                "suspected_naked_position": True,
                "drift_count": 1,
                "position_protection": [
                    {
                        "exchange_symbol": "XAUUSDT",
                        "position_amt": 0.002,
                        "required_qty": 0.002,
                        "covered": False,
                        "covered_qty": 0.0,
                        "reason_code": "position_without_resting_protective_stop",
                    }
                ],
            }
        ],
    )

    record = _finalize(base, namespace)

    assert record["orders"]["tp_sl_covered"] is False
    assert record["protective"]["status"] == "naked"
    assert record["protective"]["reason_code"] == "position_without_resting_protective_stop"
    assert record["system_state"]["trade_permission"]["status"] == "BLOCKED_NAKED_POSITION_SUSPECTED"


def test_runner_writes_error_cycle_audit_when_cycle_raises(monkeypatch, tmp_path: Path):
    base, namespace = _namespace(tmp_path)
    runner = MultiStrategyRunner(output_root=base, registry=StrategyRegistry(_strategy_config()))
    strategy = runner.registry.get(STRATEGY_ID)

    def fail_daily(*_args, **_kwargs):
        raise RuntimeError("boom during strategy evaluation")

    monkeypatch.setattr("services.multi_strategy_runner.run_daily_pipeline", fail_daily)

    result = runner._run_one(RUN_DATE, strategy, paper_auto_approve=True)
    record = load_json(namespace / "cycle_audit" / "current.json")[0]

    assert result["status"] == "error"
    assert record["status"] == "error"
    assert "boom during strategy evaluation" in record["error"]
    assert record["audit_gaps"]


def test_cycle_audit_records_normal_order_chain_and_is_idempotent(tmp_path: Path):
    base, namespace = _namespace(tmp_path)
    _write_system_vitals(base)
    _seed_common_artifacts(namespace, signal_direction="long", decision="go")
    ticket = {
        "ticket_id": "ticket_clean_order",
        "signal_id": "sig_cycle",
        "asset": "GOLD",
        "action": "prepare_buy",
        "entry_zone": "4520-4530",
        "stop_loss": 4500,
        "targets": [4560],
        "position_size_pct": 8,
        "max_loss_pct": 0.2,
        "order_type": "market",
        "time_in_force": "day",
    }
    order = {
        "order_id": "order_clean_order",
        "ticket_id": ticket["ticket_id"],
        "status": "filled",
        "requested_price": 4525,
        "fill_price": 4525,
        "quantity": 0.002,
        "filled_at": "2026-06-30T00:02:00+00:00",
    }
    write_json(namespace / "trade_tickets" / f"{RUN_DATE}.json", [ticket])
    write_json(
        namespace / "journal_decisions" / f"{RUN_DATE}.json",
        [{**ticket, "decision_status": "executed_paper", "paper_order": order}],
    )
    write_json(namespace / "paper_orders" / f"{RUN_DATE}.json", [order])
    write_json(namespace / "paper_trades" / "current.json", [{**ticket, "trade_id": "trade_clean_order", "status": "open", "order_id": order["order_id"], "stop_loss": 4500, "target": 4560}])
    write_json(
        namespace / "order_lifecycle" / f"{RUN_DATE}.json",
        [{"run_date": RUN_DATE, "order_id": order["order_id"], "ticket_id": ticket["ticket_id"], "state": "protective_attached", "blocked": False}],
    )

    first = _finalize(base, namespace, result={"status": "ok", "executed_ticket": ticket["ticket_id"]})
    second = _finalize(base, namespace, result={"status": "ok", "executed_ticket": ticket["ticket_id"]})
    rows = load_json(namespace / "cycle_audit" / f"{RUN_DATE}.json")

    assert first["execution"]["status"] == "executed"
    assert first["orders"]["entry_generated"] is True
    assert first["orders"]["tp_sl_generated"] is True
    assert first["orders"]["tp_sl_covered"] is True
    assert first["orders"]["order_lifecycle_refs"][0]["order_id"] == order["order_id"]
    assert first["risk"]["result"] == "pass"
    assert second["cycle_id"] == first["cycle_id"]
    assert len(rows) == 1
