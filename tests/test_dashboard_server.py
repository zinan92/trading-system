from pathlib import Path

import pipelines.dashboard_server as dashboard_server
from pipelines.dashboard_server import (
    build_ops_status_contract,
    build_system_status_contract,
    build_trader_overview_contract,
    build_market_view_intake_response,
    build_public_access_health,
    compact_ops_payload,
    compact_strategy_payload,
    compact_trader_payload,
)
from services.journal_store import load_json, write_json


ORAL_MARKET_VIEW = (
    "今天是十分做空。3D、1D、4H 都偏空，15m/5m 找入场。"
    "关键位 4400 平台，4100/4023 前低，目标位 3950，3877 支撑。"
    "计划只做空，反弹到 EMA50 附近出现顶分型入场，止损放顶分型高点。"
    "观点有效 4 小时，价格偏离 0.8% 失效，上破 4100 后停止使用。"
)


def test_read_model_selects_matching_dca_risk_decision(tmp_path: Path):
    output_root = tmp_path / "outputs"
    write_json(
        output_root / "dca_risk_decisions" / "2026-07-22_DAY.json",
        [
            {"decision_id": "old-dca-risk", "outcome": "approved"},
            {"decision_id": "current-dca-risk", "outcome": "acknowledged"},
        ],
    )
    write_json(
        output_root / "dualtrack" / "risk_decisions" / "current.json",
        [{"decision_id": "grid-risk", "outcome": "allow"}],
    )
    source = {
        "cycle": {"cycle_id": "2026-07-22_DAY"},
        "production_plan": {"strategy_type": "dca", "cycle_id": "2026-07-22_DAY"},
        "runtime": {"risk_decision_id": "current-dca-risk"},
    }

    selected = dashboard_server._current_strategy_risk_decision(output_root, source)

    assert selected == {"decision_id": "current-dca-risk", "outcome": "acknowledged"}


def test_market_view_intake_api_draft_only_does_not_write_artifacts(tmp_path: Path):
    result = build_market_view_intake_response(
        {"date": "2026-06-26", "raw_text": ORAL_MARKET_VIEW, "draft_only": True},
        output_root=tmp_path / "outputs",
    )

    assert result["status"] == "draft"
    assert result["draft_only"] is True
    assert result["draft"]["score"] == 10
    assert result["draft"]["expiry_target_price"] == 3950
    assert result["draft"]["expire_above"] == 4100
    assert not (tmp_path / "outputs" / "market_views" / "2026-06-26.json").exists()


def test_market_view_intake_api_records_structured_market_view(tmp_path: Path):
    output_root = tmp_path / "outputs"

    result = build_market_view_intake_response(
        {"date": "2026-06-26", "raw_text": ORAL_MARKET_VIEW},
        output_root=output_root,
    )

    assert result["status"] == "recorded"
    assert result["draft_only"] is False
    assert result["market_view"]["direction_score"] == 10
    assert result["market_view"]["expiry"]["target_price"] == 3950
    assert result["market_view"]["expiry"]["expire_below"] == 3950
    assert result["market_view"]["expiry"]["expire_above"] == 4100
    assert result["market_view"]["intake"]["parser"] == "market_view_intake_v1"
    saved = load_json(output_root / "market_views" / "current.json")[0]
    assert saved["run_date"] == "2026-06-26"
    assert saved["intake"]["extracted"]["valid_for_hours"] == 4


def test_market_view_intake_api_rejects_bad_payload(tmp_path: Path):
    try:
        build_market_view_intake_response({"date": "2026-6-26", "raw_text": "强空"}, output_root=tmp_path / "outputs")
    except ValueError as exc:
        assert "YYYY-MM-DD" in str(exc)
    else:
        raise AssertionError("expected invalid date to be rejected")

    try:
        build_market_view_intake_response({"date": "2026-06-26", "raw_text": ""}, output_root=tmp_path / "outputs")
    except ValueError as exc:
        assert "raw_text" in str(exc)
    else:
        raise AssertionError("expected empty raw_text to be rejected")


def test_connector_price_feed_refresh_runbook_api_serves_existing_artifact_read_only(tmp_path: Path):
    output_root = tmp_path / "outputs"
    runbook_path = output_root / "connector_config_apply" / "price_feed_refresh_runbook_current.json"
    write_json(
        runbook_path,
        [
            {
                "schema_version": "connector-price-feed-refresh-runbook-v1",
                "status": "ready_for_operator_refresh",
                "runbook_id": "price_feed_refresh_1",
                "command_sequence": [
                    {
                        "name": "preview_tiger_price_feed_acceptance_refresh",
                        "command": "python3 -m pipelines.tiger_price_feed_acceptance --plan-only",
                    }
                ],
                "refresh_window_gate": {
                    "schema_version": "connector-price-feed-refresh-window-gate-v1",
                    "status": "ready_to_run_acceptance_now",
                    "operator_action": "run_price_feed_acceptance_sequence_now",
                    "is_open": True,
                    "next_open": None,
                    "can_preview_now": True,
                    "can_run_quote_client_step": True,
                    "safety": {
                        "read_only": True,
                        "uses_local_session_calendar": True,
                        "opens_network_clients": False,
                        "opens_quote_client": False,
                        "opens_trade_client": False,
                        "submits_orders": False,
                        "writes_runtime_config": False,
                    },
                },
                "status_receipt": {"schema_version": "connector-config-status-v1"},
                "endpoint_safety": {
                    "read_only": False,
                    "generates_runbook": True,
                    "opens_trade_client": True,
                    "submits_orders": True,
                    "writes_runtime_config": True,
                },
                "safety": {
                    "read_only": True,
                    "opens_network_clients": False,
                    "opens_quote_client": False,
                    "opens_trade_client": False,
                    "submits_orders": False,
                    "writes_runtime_config": False,
                    "credential_values_exposed": False,
                },
            }
        ],
    )

    response = dashboard_server.build_connector_price_feed_refresh_runbook_response(output_root=output_root)
    serialized = str(response)

    assert response["status"] == "ready_for_operator_refresh"
    assert response["runbook_id"] == "price_feed_refresh_1"
    assert response["served_from"] == str(runbook_path)
    assert response["command_sequence"][0]["name"] == "preview_tiger_price_feed_acceptance_refresh"
    assert "command" not in response["command_sequence"][0]
    assert response["refresh_window_gate"]["status"] == "ready_to_run_acceptance_now"
    assert response["refresh_window_gate"]["can_run_quote_client_step"] is True
    assert response["refresh_window_gate"]["safety"]["opens_quote_client"] is False
    assert response["refresh_window_gate"]["safety"]["opens_trade_client"] is False
    assert response["refresh_window_gate"]["safety"]["submits_orders"] is False
    assert response["refresh_window_gate"]["safety"]["writes_runtime_config"] is False
    assert response["endpoint_safety"]["read_only"] is True
    assert response["endpoint_safety"]["generates_runbook"] is False
    assert response["endpoint_safety"]["opens_network_clients"] is False
    assert response["endpoint_safety"]["opens_quote_client"] is False
    assert response["endpoint_safety"]["opens_trade_client"] is False
    assert response["endpoint_safety"]["submits_orders"] is False
    assert response["endpoint_safety"]["writes_runtime_config"] is False
    assert response["endpoint_safety"]["credential_values_exposed"] is False
    assert response["endpoint_safety"]["raw_command_text_exposed"] is False
    assert response["redaction"]["raw_command_text_exposed"] is False
    assert "python3 -m" not in serialized


def test_tiger_paper_order_refresh_runbook_api_redacts_raw_commands(tmp_path: Path):
    output_root = tmp_path / "outputs"
    readiness_path = output_root / "tiger_paper_order_readiness" / "current.json"
    runbook_path = output_root / "tiger_paper_order_readiness" / "refresh_runbook_current.json"
    write_json(
        readiness_path,
        [
            {
                "status": "blocked",
                "ready_for_attended_paper_order": False,
                "can_submit_without_explicit_operator_authorization": False,
                "real_tiger_network_call_attempted": False,
                "checks": [{"name": "order_sync", "status": "fail"}],
                "blockers": [
                    {
                        "name": "order_sync",
                        "status": "fail",
                        "evidence": {
                            "checked_at": "2026-07-05T13:21:20+00:00",
                            "required_run_date": "2026-07-06",
                            "current_for_run_date": False,
                        },
                    }
                ],
                "next_commands": ["python3 -m pipelines.tiger_openapi_order_sync --date 2026-07-06 --json"],
                "checked_at": "2026-07-06T09:30:09+00:00",
            }
        ],
    )
    write_json(
        runbook_path,
        [
            {
                "schema_version": "tiger-paper-order-readiness-refresh-runbook-v1",
                "status": "ready_for_operator_refresh",
                "runbook_id": "paper_order_refresh_runbook_test",
                "run_date": "2026-07-06",
                "readiness_status": "blocked",
                "readiness_checked_at": "2026-07-06T09:30:09+00:00",
                "blocker_count": 1,
                "stale_evidence_count": 1,
                "blocker_names": ["order_sync"],
                "command_sequence": [
                    {
                        "name": "refresh_order_sync",
                        "label": "refresh orders/fills",
                        "command": "python3 -m pipelines.tiger_openapi_order_sync --date 2026-07-06 --json",
                        "opens_trade_client": True,
                        "opens_trade_client_mode": "read_only",
                        "submits_orders": False,
                        "writes_runtime_config": False,
                    }
                ],
                "generation_safety": {
                    "artifact_only": True,
                    "opens_quote_client": False,
                    "opens_trade_client": False,
                    "submits_orders": False,
                    "cancels_orders": False,
                    "closes_positions": False,
                    "writes_runtime_config": False,
                    "credential_values_exposed": False,
                },
                "command_sequence_safety": {
                    "opens_trade_client_read_only": True,
                    "submits_orders": False,
                    "writes_runtime_config": False,
                },
                "checked_at": "2026-07-06T09:38:45+00:00",
            }
        ],
    )

    response = dashboard_server.build_tiger_paper_order_refresh_runbook_response(output_root=output_root)
    serialized = str(response)

    assert response["schema_version"] == "tiger-paper-order-refresh-runbook-api-v1"
    assert response["status"] == "ready_for_operator_refresh"
    assert response["served_from"] == str(runbook_path)
    assert response["matches_current_readiness"] is True
    assert response["command_count"] == 1
    assert response["command_steps"] == [
        {
            "name": "refresh_order_sync",
            "label": "refresh orders/fills",
            "opens_trade_client": True,
            "opens_trade_client_mode": "read_only",
            "submits_orders": False,
            "writes_runtime_config": False,
        }
    ]
    assert response["generation_safety"]["opens_trade_client"] is False
    assert response["command_sequence_safety"]["opens_trade_client_read_only"] is True
    assert response["endpoint_safety"]["read_only"] is True
    assert response["endpoint_safety"]["opens_trade_client"] is False
    assert response["endpoint_safety"]["submits_orders"] is False
    assert response["endpoint_safety"]["writes_runtime_config"] is False
    assert response["endpoint_safety"]["raw_command_text_exposed"] is False
    assert response["redaction"]["raw_command_text_exposed"] is False
    assert "python3 -m" not in serialized


def test_connector_attended_switch_review_api_redacts_write_authorization(monkeypatch):
    status = {
        "schema_version": "connector-config-status-v1",
        "checked_at": "2026-07-06T09:21:12+00:00",
        "current_runtime": {"broker_provider": "binance_usdm", "dualtrack_symbol": "GOLD"},
        "latest_check": {
            "status": "ready_for_attended_config_write",
            "package_id": "pkg_1",
        },
        "latest_authorization": {
            "status": "ready_for_operator_authorization",
            "authorization_id": "auth_1",
            "package_id": "pkg_1",
            "attended_apply_command": "python3 -m pipelines.connector_config_apply apply --acknowledgement SECRET_ACK",
        },
        "latest_readiness_audit": {
            "status": "go_for_attended_config_switch",
            "audit_id": "audit_1",
            "package_id": "pkg_1",
            "checked_at": "2026-07-06T09:20:00+00:00",
        },
        "operator_stage": {
            "stage": "ready_for_attended_config_switch",
            "summary": "ready",
            "next_action": "operator_review_authorization_then_run_attended_config_apply_if_approved",
            "runtime_switched_to_tiger_mgc": False,
            "can_trade_machine_track": False,
            "can_submit_tiger_orders": False,
            "attended_switch_review": {
                "status": "ready_for_operator_review",
                "package_id": "pkg_1",
                "authorization_id": "auth_1",
                "audit_id": "audit_1",
                "can_switch_config_with_operator_authorization": True,
                "requires_operator_command": True,
                "requires_warning_acceptance": True,
                "requires_acknowledgement": True,
                "requires_package_id": True,
                "rollback_required": True,
                "post_apply_validation_count": 4,
                "authorization_markdown": "outputs/connector_config_apply/authorization_current.md",
                "final_readiness_audit_markdown": "outputs/connector_config_apply/final_readiness_audit_current.md",
                "runtime_config_writes_from_status_endpoint": False,
                "opens_network_clients_from_status_endpoint": False,
                "submits_orders_from_status_endpoint": False,
                "can_trade_machine_track_after_switch": False,
                "can_submit_tiger_orders_after_switch": False,
                "not_authorized_after_switch": ["Tiger paper TradeClient order submission"],
            },
        },
    }
    monkeypatch.setattr(dashboard_server, "build_connector_config_status_response", lambda *, output_root=None: status)

    response = dashboard_server.build_connector_attended_switch_review_response()
    serialized = str(response)

    assert response["schema_version"] == "connector-attended-switch-review-api-v1"
    assert response["status"] == "ready_for_operator_review"
    assert response["package_id"] == "pkg_1"
    assert response["authorization_id"] == "auth_1"
    assert response["audit_id"] == "audit_1"
    assert response["runtime_switched_to_tiger_mgc"] is False
    assert response["current_broker_provider"] == "binance_usdm"
    assert response["current_dualtrack_symbol"] == "GOLD"
    assert response["can_trade_machine_track"] is False
    assert response["can_submit_tiger_orders"] is False
    assert response["requires_operator_command"] is True
    assert response["requires_acknowledgement"] is True
    assert response["rollback_required"] is True
    assert response["after_switch_gates"]["can_trade_machine_track"] is False
    assert response["after_switch_gates"]["can_submit_tiger_orders"] is False
    assert response["endpoint_safety"]["read_only"] is True
    assert response["endpoint_safety"]["runs_config_apply"] is False
    assert response["endpoint_safety"]["writes_runtime_config"] is False
    assert response["endpoint_safety"]["opens_trade_client"] is False
    assert response["endpoint_safety"]["submits_orders"] is False
    assert response["endpoint_safety"]["raw_acknowledgement_exposed"] is False
    assert response["endpoint_safety"]["attended_apply_command_exposed"] is False
    assert response["redaction"]["raw_acknowledgement_exposed"] is False
    assert response["redaction"]["attended_apply_command_exposed"] is False
    assert "SECRET_ACK" not in serialized
    assert "python3 -m pipelines.connector_config_apply apply" not in serialized


def test_connector_price_feed_refresh_runbook_api_missing_is_read_only(tmp_path: Path):
    response = dashboard_server.build_connector_price_feed_refresh_runbook_response(output_root=tmp_path / "outputs")

    assert response["status"] == "missing"
    assert response["command_sequence"] == []
    assert response["served_from"].endswith("connector_config_apply/price_feed_refresh_runbook_current.json")
    assert response["safety"]["read_only"] is True
    assert response["safety"]["opens_network_clients"] is False
    assert response["safety"]["opens_quote_client"] is False
    assert response["safety"]["opens_trade_client"] is False
    assert response["safety"]["submits_orders"] is False
    assert response["safety"]["writes_runtime_config"] is False
    assert response["safety"]["credential_values_exposed"] is False
    assert response["endpoint_safety"]["read_only"] is True
    assert response["endpoint_safety"]["generates_runbook"] is False
    assert response["endpoint_safety"]["opens_network_clients"] is False
    assert response["endpoint_safety"]["opens_quote_client"] is False
    assert response["endpoint_safety"]["opens_trade_client"] is False
    assert response["endpoint_safety"]["submits_orders"] is False
    assert response["endpoint_safety"]["writes_runtime_config"] is False
    assert response["endpoint_safety"]["credential_values_exposed"] is False


def test_compact_trader_payload_keeps_reader_fields_and_drops_ops_bulk():
    payload = {
        "contract": {"version": 1},
        "run_date": "2026-06-22",
        "bars": [{"close": 4200.0}],
        "orders": [{"order_id": "order_1"}],
        "strategy_config": {"raw": True},
        "performance_board": {"strategies": [{"strategy_id": "gold_1m_macd"}]},
        "review_loop": {"morning_plan": {}},
        "dashboard_health": {"checks": []},
        "market_data_gate": {"mode": "replay_only"},
        "market_view_status": {
            "status": "expired",
            "filter_effect": "expired_direction_filter_disabled",
            "operator_message": "口述方向已失效；系统不再按这条观点过滤多空信号。",
        },
        "ohlc_quality": {"provider": "binance_usdm"},
        "backend_maturity": {
            "status": "warn",
            "checks": [
                {"name": "M0_system_vitals", "status": "fail", "summary": "vitals need attention", "evidence": {"board": {"bottlenecks": list(range(20))}}},
                {"name": "M1_trade_record_cards", "status": "pass", "summary": "cards complete", "evidence": {}},
            ],
        },
        "strategy_frequency": {
            "status": "within_portfolio_sample_target",
            "summary": {"portfolio_executed_count": 9, "below_min_strategy_count": 8},
            "strategies": [
                {
                    "strategy_id": "gold_1m_grid",
                    "stage": "effective",
                    "classification": {"family": "grid", "expected_trades_per_day_min": 2},
                    "signal_count": 15,
                    "candidate_count": 5,
                    "ticket_count": 3,
                    "executed_trade_count": 2,
                    "min_daily_executed_trades": 2,
                    "sample_statuses": ["no_signal"] * 30,
                    "recommendation": {"action": "keep_running", "reason": "met target"},
                    "attribution": {
                        "limiting_reason_label": "回测样本薄（不挡paper）",
                        "reason_counts": {"executed": 2},
                        "evidence": [
                            {
                                "reason": "backtest_thin_context",
                                "reason_label": "回测样本薄（不挡paper）",
                                "detail": "thin backtest verdict sample_size=0",
                                "sample_id": "sample_1",
                                "signal_id": "sig_1",
                                "signal_generated_at": "2026-06-25T08:01:00+00:00",
                                "decision_cursor": "2026-06-25T08:01:00+00:00",
                                "ticket_id": "",
                                "execution_status": "candidate_without_ticket",
                                "large": "dropped",
                            }
                        ],
                    },
                }
            ],
        },
        "strategy_daily_reviews": {
            "strategy_count": 1,
            "status_counts": {"low_frequency_review": 1},
            "strategies": [{
                "strategy_id": "gold_1m_grid",
                "pm_verdict": "low_frequency_review",
                "frequency": {"executed_today": 1},
                "pm_action": "inspect_signal_to_ticket",
                "pm_summary": "有方向信号但未形成可执行订单。",
                "review_priority": 75,
                "next_review_cursor": "2026-06-25T08:01:00+00:00",
                "replay_context": {"strategy_id": "gold_1m_grid", "cursor": "2026-06-25T08:01:00+00:00"},
            }],
        },
        "daily_trade_samples": {
            "run_date": "2026-06-22",
            "status": "below_minimum_executed_trades",
            "active_strategy_id": "gold_1m_macd",
            "summary": {
                "observation_count": 132,
                "candidate_count": 17,
                "ticket_count": 1,
                "quality_pass_count": 1,
                "executed_count": 0,
                "candidate_without_ticket_count": 15,
                "blocked_count": 1,
                "funnel": {"observations": 132, "directional_candidates": 17, "tickets": 1, "executed_or_requested": 0},
            },
            "sample_requirements": {
                "effective_leverage": 5,
                "min_target_equity_return_pct": 1,
                "daily_min_trade_samples": 3,
            },
            "samples": [{"large": True} for _ in range(50)],
        },
        "trade_reviews": {"run_date": "2026-06-22", "summary": {"sample_summary": {"executed_count": 9}}},
        "strategy_promotion_gate": {
            "status": "blocked",
            "promotion_allowed": False,
            "blockers": [{"name": "closed_trade_sample", "summary": "Need more closed trades"} for _ in range(12)],
        },
        "live_submission_safety": {
            "status": "pass",
            "network_call_attempted": False,
            "blocked_by_activation_gate": True,
            "provider": "oanda_rest",
        },
        "alerts": {"overall_status": "ok", "alert_count": 0, "channel": "feishu"},
        "operation_runbook": {"status": "paper_manual_only", "mode": "paper"},
        "live_reconciliation": {
            "truth_scope": "active_demo_strategy",
            "strategy_id": "gold_1m_macd",
            "reconciled": True,
            "drift_count": 0,
        },
        "legacy_live_reconciliation": {
            "truth_scope": "legacy_global",
            "run_date": "2026-06-21",
            "reconciled": True,
        },
    }

    compact = compact_trader_payload(payload)

    assert compact["performance_board"]["strategies"][0]["strategy_id"] == "gold_1m_macd"
    assert compact["market_data_gate"]["mode"] == "replay_only"
    assert compact["market_view_status"]["filter_effect"] == "expired_direction_filter_disabled"
    assert compact["ohlc_quality"]["provider"] == "binance_usdm"
    assert compact["backend_maturity"]["status"] == "warn"
    assert compact["backend_maturity"]["checks"][0]["name"] == "M0_system_vitals"
    assert len(compact["backend_maturity"]["checks"][0]["evidence"]["board"]["bottlenecks"]) == 8
    assert compact["strategy_frequency"]["summary"]["portfolio_executed_count"] == 9
    assert compact["strategy_frequency"]["strategies"][0]["strategy_id"] == "gold_1m_grid"
    assert compact["strategy_frequency"]["strategies"][0]["signal_count"] == 15
    assert compact["strategy_frequency"]["strategies"][0]["candidate_count"] == 5
    assert compact["strategy_frequency"]["strategies"][0]["ticket_count"] == 3
    assert compact["strategy_frequency"]["strategies"][0]["executed_trade_count"] == 2
    assert compact["strategy_frequency"]["strategies"][0]["recommendation"]["action"] == "keep_running"
    assert len(compact["strategy_frequency"]["strategies"][0]["sample_statuses"]) == 20
    assert compact["strategy_frequency"]["strategies"][0]["attribution"]["limiting_reason_label"] == "回测样本薄（不挡paper）"
    assert compact["strategy_frequency"]["strategies"][0]["attribution"]["evidence"][0]["decision_cursor"] == "2026-06-25T08:01:00+00:00"
    assert compact["strategy_frequency"]["strategies"][0]["attribution"]["evidence"][0]["signal_id"] == "sig_1"
    assert "large" not in compact["strategy_frequency"]["strategies"][0]["attribution"]["evidence"][0]
    assert compact["strategy_daily_reviews"]["status_counts"]["low_frequency_review"] == 1
    assert compact["strategy_daily_reviews"]["strategies"][0]["pm_action"] == "inspect_signal_to_ticket"
    assert compact["strategy_daily_reviews"]["strategies"][0]["review_priority"] == 75
    assert compact["strategy_daily_reviews"]["strategies"][0]["replay_context"]["cursor"] == "2026-06-25T08:01:00+00:00"
    assert compact["daily_trade_samples"]["summary"]["observation_count"] == 132
    assert compact["daily_trade_samples"]["summary"]["candidate_count"] == 17
    assert compact["daily_trade_samples"]["summary"]["ticket_count"] == 1
    assert compact["daily_trade_samples"]["summary"]["executed_count"] == 0
    assert compact["daily_trade_samples"]["sample_requirements"]["daily_min_trade_samples"] == 3
    assert "samples" not in compact["daily_trade_samples"]
    assert compact["trade_reviews"]["summary"]["sample_summary"]["executed_count"] == 9
    assert compact["strategy_promotion_gate"]["promotion_allowed"] is False
    assert len(compact["strategy_promotion_gate"]["blockers"]) == 8
    assert compact["live_submission_safety"]["network_call_attempted"] is False
    assert compact["live_submission_safety"]["blocked_by_activation_gate"] is True
    assert compact["alerts"]["channel"] == "feishu"
    assert compact["operation_runbook"]["mode"] == "paper"
    assert compact["live_reconciliation"]["truth_scope"] == "active_demo_strategy"
    assert compact["live_reconciliation"]["strategy_id"] == "gold_1m_macd"
    assert compact["legacy_live_reconciliation"]["truth_scope"] == "legacy_global"
    assert "bars" not in compact
    assert "orders" not in compact
    assert "strategy_config" not in compact


def _contract_payload() -> dict:
    return {
        "contract": {"schema_version": "dashboard-v2.1", "generated_at": "2026-06-30T00:00:00Z"},
        "run_date": "2026-06-30",
        "latest": {"timestamp": "2026-06-30T00:00:00+00:00", "close": 4200},
        "latest_quote": {"timestamp": "2026-06-30T00:00:05+00:00", "close": 4200.5, "provider": "binance_usdm"},
        "system_vitals": {
            "overall": "alive",
            "checked_at": "2026-06-30T00:00:10+00:00",
            "vitals": [
                {"name": "data_feed", "status": "up", "message": "GOLD feed is fresh", "detail": {"age_minutes": 1}},
                {"name": "strategy_evaluation", "status": "up", "message": "strategy evaluated", "detail": {}},
                {"name": "runner_liveness", "status": "up", "message": "runner heartbeat is fresh", "detail": {}},
                {"name": "execution_blocker", "status": "up", "message": "no reconciliation drift", "detail": {"drift_count": 0}},
                {"name": "tp_sl_coverage", "status": "up", "message": "all open trades protected", "detail": {}},
            ],
        },
        "performance_board": {
            "active_strategy_id": "gold_1m_chan",
            "active_demo_blocker": {},
            "gold_nav": {"points": [{"timestamp": "2026-06-30T00:00:00+00:00", "close": 4200}]},
            "strategies": [
                {
                    "strategy_id": "gold_1m_chan",
                    "timeframe": "1m",
                    "status": "ok",
                    "today_trade_count": 0,
                    "open_trades": 1,
                    "closed_trades": 4,
                    "current_equity": 10012,
                    "return_pct": 0.12,
                    "gold_return_pct": -0.3,
                    "vs_gold_pct": 0.42,
                    "position": {"status": "open", "summary": "long qty=1"},
                    "daily_execution": {"executed_trade_count": 0},
                    "performance_confidence": {"status": "paper_active"},
                    "nav_quality": {"status": "ok"},
                    "nav_points": [{"timestamp": "2026-06-30T00:00:00+00:00", "equity": 10012}],
                    "demo_blocker": {"raw": "must not leak to trader overview"},
                }
            ],
        },
        "strategy_detail": {
            "open_trades": [{"trade_id": "open1", "status": "open", "entry_price": 4200, "source_artifacts": {"debug": True}}],
            "closed_trades": [{"trade_id": "closed1", "status": "closed", "realized_pnl": 12}],
            "trade_record_cards": [{"trade_id": "closed1", "status": "closed", "audit": {"ok": True}}],
        },
        "market_view_status": {"status": "active", "direction_bias": "strong_short", "operator_message": "only short"},
        "ohlc_quality": {"status": "pass", "provider": "binance_usdm", "promotion_ready": True, "trust_label": "execution venue"},
        "market_data_gate": {"mode": "promotion_ready", "promotion_ready": True, "trader_label": "ready", "blockers": []},
        "data_provenance": {"mode": "execution_venue", "allows_paper": True, "allows_live": False},
        "daily_trade_samples": {"status": "ok", "summary": {"executed_count": 1}, "sample_requirements": {"daily_min_trade_samples": 3}},
        "trade_reviews": {"status": "ok", "summary": {"sample_summary": {"executed_count": 1}}},
        "review_loop": {"status": "ok", "today_focus": "review latest trade"},
        "dashboard_health": {"status": "ok", "checks": [{"name": "api", "status": "ok", "message": "dashboard snapshot generated"}]},
        "backend_maturity": {"status": "warn", "checks": [{"name": "M0_system_vitals", "status": "pass", "summary": "ok"}]},
        "source_contracts": {"performance_board": {"section": "performance_board"}},
        "live_reconciliation": {"reconciled": True, "drift_count": 0, "truth_scope": "active_demo_strategy"},
        "legacy_live_reconciliation": {"truth_scope": "legacy_global"},
        "live_submission_safety": {"status": "pass", "blocked_by_activation_gate": True},
        "paper_reconciliation": {"status": "pass"},
        "paper_trade_attribution": {"status": "pass"},
        "paper_exit_monitor": {"status": "pass"},
        "runner": {"state": "ok"},
        "schedule_install_plan": {"status": "ready"},
        "schedule_install": {"status": "blocked", "blocker": "missing_acknowledgement"},
        "schedule_rollback_plan": {"status": "blocked", "blocker": "missing_backup_records"},
        "schedule_rollback": {"status": "blocked", "blocker": "missing_backup_records"},
        "schedule_post_install_verify": {"status": "blocked", "checks": [{"name": "schedule_current_active", "status": "fail"}]},
        "schedule_takeover_package": {"status": "ready_for_attended_install"},
        "schedule_takeover_package_check": {"status": "ready_for_attended_install", "operator_next_action": {"action": "authorize_attended_install"}},
        "alerts": {"overall_status": "ok"},
        "operation_runbook": {"status": "paper_auto_ready"},
    }


def test_system_status_contract_exposes_canonical_trade_permission():
    payload = _contract_payload()

    contract = build_system_status_contract(payload)

    assert contract["contract"]["schema_version"] == "system-status-v1"
    assert contract["status"] == "READY_TO_TRADE"
    assert contract["trade_permission"]["allows_new_order_if_signal"] is True
    assert contract["trade_permission"]["strategy_id"] == "gold_1m_chan"
    assert contract["health"]["overall"] == "up"

    payload["system_vitals"]["vitals"][0]["status"] = "down"
    payload["system_vitals"]["vitals"][0]["message"] = "GOLD feed stale: 45.0m old"
    blocked = build_system_status_contract(payload)

    assert blocked["status"] == "BLOCKED_DATA_STALE"
    assert blocked["trade_permission"]["primary_blocker"]["source"] == "system_vitals.data_feed"
    assert blocked["trade_permission"]["is_system_blocker"] is True


def test_system_status_contract_prioritizes_position_limit_after_system_health():
    payload = _contract_payload()
    payload["performance_board"]["strategies"][0]["open_trades"] = 3

    contract = build_system_status_contract(payload)

    assert contract["status"] == "PAUSED_POSITION_LIMIT"
    assert contract["trade_permission"]["is_position_limit"] is True
    assert contract["trade_permission"]["primary_blocker"]["code"] == "position_limit"


def test_system_status_contract_blocks_on_always_on_critical_stale():
    payload = _contract_payload()
    payload["system_vitals"]["always_on"] = {
        "status": "BLOCKED_ALWAYS_ON_STALE",
        "blocks_new_orders": True,
        "critical_blockers": [
            {"name": "runner", "message": "runner heartbeat stale: 20.0m old", "freshness": {"age_seconds": 1200}}
        ],
    }

    contract = build_system_status_contract(payload)

    permission = contract["trade_permission"]
    assert contract["status"] == "BLOCKED_ALWAYS_ON_STALE"
    assert permission["allows_new_order_if_signal"] is False
    assert permission["primary_blocker"]["code"] == "runner"
    assert permission["primary_blocker"]["source"] == "system_vitals.always_on"


def test_system_status_contract_does_not_suppress_stale_strategy_heartbeat():
    payload = _contract_payload()
    payload["system_vitals"]["vitals"][1]["status"] = "down"
    payload["system_vitals"]["vitals"][1]["message"] = "strategies summary stale: 16.0m old"

    contract = build_system_status_contract(payload)

    assert contract["status"] == "BLOCKED_ALWAYS_ON_STALE"
    assert contract["trade_permission"]["allows_new_order_if_signal"] is False
    assert contract["trade_permission"]["primary_blocker"]["source"] == "system_vitals.strategy_evaluation"


def test_system_status_contract_allows_new_orders_when_only_noncritical_liveness_degrades():
    payload = _contract_payload()
    payload["system_vitals"]["always_on"] = {
        "status": "DEGRADED",
        "blocks_new_orders": False,
        "degraded_jobs": [
            {"name": "cycle_audit", "message": "cycle_audit heartbeat stale: 60.0m old"}
        ],
    }

    contract = build_system_status_contract(payload)

    permission = contract["trade_permission"]
    assert contract["status"] == "DEGRADED"
    assert permission["allows_new_order_if_signal"] is True
    assert permission["primary_blocker"]["status"] == "DEGRADED"


def test_system_status_contract_uses_flat_only_limit_for_active_demo_strategy(monkeypatch):
    payload = _contract_payload()
    payload["performance_board"]["active_strategy_id"] = "gold_1m_chan"
    payload["performance_board"]["strategies"][0]["strategy_id"] = "gold_1m_chan"
    payload["performance_board"]["strategies"][0]["open_trades"] = 1
    monkeypatch.setattr(
        "pipelines.dashboard_server.load_pipeline_config",
        lambda: {
            "demo_trading": {
                "enabled": True,
                "active_strategy_id": "gold_1m_chan",
                "require_flat_before_entry": True,
            }
        },
    )

    contract = build_system_status_contract(payload)

    permission = contract["trade_permission"]
    assert contract["status"] == "PAUSED_POSITION_LIMIT"
    assert permission["allows_new_order_if_signal"] is False
    assert permission["open_trade_limit"] == 1
    assert permission["requires_flat_before_entry"] is True
    assert permission["open_trade_limit_source"] == "pipeline.demo_trading.require_flat_before_entry"
    assert permission["primary_blocker"]["code"] == "position_limit"


def test_system_status_contract_uses_structured_reconciliation_unknown_blocker():
    payload = _contract_payload()
    payload["performance_board"]["active_demo_blocker"] = {
        "blocked": True,
        "status": "reconciliation_unknown",
        "system_state": "BLOCKED_RECONCILIATION_UNKNOWN",
        "reason_code": "venue_state_unknown",
        "reason": "Binance demo reconciliation cannot confirm venue state: TimeoutError",
    }

    contract = build_system_status_contract(payload)

    assert contract["status"] == "BLOCKED_RECONCILIATION_UNKNOWN"
    assert contract["trade_permission"]["allows_new_order_if_signal"] is False
    assert contract["trade_permission"]["primary_blocker"]["code"] == "venue_state_unknown"
    assert "cannot confirm" in contract["trade_permission"]["primary_blocker"]["message"]


def test_system_status_contract_prioritizes_live_money_guardrail_before_position_limit():
    payload = _contract_payload()
    payload["performance_board"]["strategies"][0]["open_trades"] = 3
    payload["live_money_guardrails"] = {
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

    contract = build_system_status_contract(payload)

    permission = contract["trade_permission"]
    assert contract["status"] == "BLOCKED_DAILY_LOSS_LIMIT"
    assert permission["allows_new_order_if_signal"] is False
    assert permission["primary_blocker"]["code"] == "daily_loss_limit"
    assert permission["is_position_limit"] is False


def test_system_status_contract_surfaces_each_live_money_guardrail_code():
    cases = [
        ("BLOCKED_SINGLE_ORDER_NOTIONAL_LIMIT", "single_order_notional_limit"),
        ("BLOCKED_TOTAL_NOTIONAL_LIMIT", "total_notional_limit"),
        ("BLOCKED_DAILY_TRADE_LIMIT", "daily_trade_limit"),
    ]

    for status, code in cases:
        payload = _contract_payload()
        payload["live_money_guardrails"] = {
            "status": status,
            "allows_new_order": False,
            "primary_blocker": {
                "status": status,
                "code": code,
                "source": f"live_money_guardrails.{code}",
                "message": f"{code} blocked",
            },
        }

        contract = build_system_status_contract(payload)

        assert contract["status"] == status
        assert contract["trade_permission"]["allows_new_order_if_signal"] is False
        assert contract["trade_permission"]["primary_blocker"]["code"] == code


def test_system_status_contract_prioritizes_operator_halt_before_system_health():
    payload = _contract_payload()
    payload["system_vitals"]["vitals"][0]["status"] = "down"
    payload["system_vitals"]["vitals"][0]["message"] = "GOLD feed stale"
    payload["live_money_guardrails"] = {
        "status": "BLOCKED_OPERATOR_HALT",
        "allows_new_order": False,
        "primary_blocker": {
            "status": "BLOCKED_OPERATOR_HALT",
            "code": "operator_halt",
            "source": "live_halt.current",
            "message": "operator HALT is active",
        },
    }

    contract = build_system_status_contract(payload)

    assert contract["status"] == "BLOCKED_OPERATOR_HALT"
    assert contract["trade_permission"]["primary_blocker"]["code"] == "operator_halt"


def test_trader_overview_contract_is_reader_facing_and_drops_ops_only_sections():
    payload = _contract_payload()

    overview = build_trader_overview_contract(payload)

    assert overview["contract"]["schema_version"] == "trader-overview-v1"
    assert overview["strategy_id"] == "gold_1m_chan"
    assert overview["trade_permission"]["status"] == "READY_TO_TRADE"
    assert overview["current_strategy"]["current_equity"] == 10012
    assert overview["nav"]["strategy_points"][0]["equity"] == 10012
    assert overview["trades"]["open"][0]["trade_id"] == "open1"
    assert "source_artifacts" not in overview["trades"]["open"][0]
    for ops_key in (
        "backend_maturity",
        "dashboard_health",
        "source_contracts",
        "live_reconciliation",
        "legacy_live_reconciliation",
        "alerts",
        "operation_runbook",
        "schedule_post_install_verify",
        "schedule_takeover_package",
        "schedule_takeover_package_check",
        "schedule_install",
        "schedule_rollback",
    ):
        assert ops_key not in overview
    assert "demo_blocker" not in overview["current_strategy"]


def test_ops_status_contract_keeps_diagnostics_outside_trader_contract():
    payload = _contract_payload()

    ops = build_ops_status_contract(payload)

    assert ops["contract"]["schema_version"] == "ops-status-v1"
    assert ops["status"] == "ok"
    assert ops["backend"]["backend_maturity"]["status"] == "warn"
    assert ops["backend"]["dashboard_health"]["checks"][0]["name"] == "api"
    assert ops["runner"]["schedule_install"]["blocker"] == "missing_acknowledgement"
    assert ops["runner"]["schedule_rollback_plan"]["blocker"] == "missing_backup_records"
    assert ops["runner"]["schedule_post_install_verify"]["checks"][0]["name"] == "schedule_current_active"
    assert ops["runner"]["schedule_takeover_package"]["status"] == "ready_for_attended_install"
    assert ops["runner"]["schedule_takeover_package_check"]["operator_next_action"]["action"] == "authorize_attended_install"
    assert ops["data"]["source_contracts"]["performance_board"]["section"] == "performance_board"
    assert ops["execution"]["live_reconciliation"]["truth_scope"] == "active_demo_strategy"
    assert ops["diagnostics"]["full_diagnostics_query"] == "/api/dashboard?view=full"


def test_compact_ops_payload_keeps_first_paint_and_trims_bulk_sections():
    payload = {
        "contract": {"version": 1},
        "run_date": "2026-06-22",
        "latest_quote": {"close": 4182.3},
        "bars": [
            {"timestamp": f"2026-06-22T00:{idx:02d}:00+00:00", "open": idx, "high": idx + 1, "low": idx - 1, "close": idx, "volume": 99}
            for idx in range(700)
        ],
        "signals": [{"asset": "GOLD", "direction": "watch"}],
        "health": {"status": "warn"},
        "doctor": {"status": "pass"},
        "runner": {"state": "ok"},
        "data_provenance": {"mode": "execution_venue"},
        "evening_review": {
            "status": "review_ready",
            "generated_at": "2026-06-22T12:00:00+00:00",
            "previous_plan": {"large": "x" * 1000},
            "adherence": {"executed_count": 1},
            "improvement_queue": [{"item": str(idx)} for idx in range(10)],
        },
        "trading_plan": {
            "status": "blocked",
            "mode": "no_trade",
            "previous_review": {"large": True},
            "trade_plan": {"blocks": ["no signal"]},
            "signals": [{"signal_id": str(idx)} for idx in range(12)],
        },
        "strategy_detail": {"large": True},
        "nav_curve_intraday": [{"large": True}],
        "performance_board": {"large": True},
        "alerts": {"overall_status": "warn", "alert_count": 2, "events": [{"large": True}]},
        "data_health": {
            "status": "warn",
            "summary": {"issue_count": 3},
            "suspicious_price_jumps": [{"id": idx} for idx in range(12)],
            "issues": [{"id": idx} for idx in range(12)],
        },
        "collector_runs": [
            {"symbol": "GOLD", "timeframe": "5m", "collected_at": f"2026-06-22T00:{idx:02d}:00+00:00", "fetch_status": "fresh", "provider": "binance_usdm"}
            for idx in range(14)
        ],
        "live_reconciliation": {"reconciled": True, "drift_count": 0, "checked_at": "2026-06-22T00:00:00+00:00"},
    }

    compact = compact_ops_payload(payload)

    assert compact["latest_quote"]["close"] == 4182.3
    assert compact["health"]["status"] == "warn"
    assert compact["doctor"]["status"] == "pass"
    assert compact["runner"]["state"] == "ok"
    assert len(compact["bars"]) == 500
    assert compact["bars"][0]["timestamp"] == "2026-06-22T00:200:00+00:00"
    assert compact["ops_payload"]["mode"] == "compact"
    assert compact["ops_payload"]["bars_total"] == 700
    assert "strategy_detail" not in compact
    assert "nav_curve_intraday" not in compact
    assert "performance_board" not in compact
    assert "previous_plan" not in compact["evening_review"]
    assert len(compact["evening_review"]["improvement_queue"]) == 6
    assert "previous_review" not in compact["trading_plan"]
    assert len(compact["trading_plan"]["signals"]) == 8
    assert compact["alerts"]["overall_status"] == "warn"
    assert compact["data_health"]["status"] == "warn"
    assert len(compact["data_health"]["suspicious_price_jumps"]) == 8
    assert compact["collector_runs"]["total_runs"] == 14
    assert len(compact["collector_runs"]["latest"]) <= 12
    assert compact["live_reconciliation"]["reconciled"] is True


def test_public_access_health_distinguishes_tunnel_failure_from_local_dashboard(monkeypatch, tmp_path: Path):
    def fake_probe(url, timeout):
        if "127.0.0.1" in url:
            return {"ok": True, "status_code": 200, "reason": "OK", "elapsed_ms": 1}
        return {"ok": False, "status_code": 530, "reason": "cloudflare tunnel unavailable", "elapsed_ms": 2}

    monkeypatch.setattr(dashboard_server, "_probe_http", fake_probe)
    monkeypatch.setattr(
        dashboard_server,
        "_cloudflared_process_summary",
        lambda: {"running": True, "processes": ["123 cloudflared tunnel run topic-workbench"]},
    )
    log = tmp_path / "cloudflared.log"
    log.write_text("2026 ERR Failed to dial a quic connection: no route to host on port 7844\n", encoding="utf-8")

    health = build_public_access_health(
        public_url="https://goldbot.park-ai-intel.com/dashboard-v4.html",
        local_url="http://127.0.0.1:8766/dashboard-v4.html",
        log_path=log,
    )

    assert health["status"] == "fail"
    assert health["diagnosis"] == "cloudflare_edge_unreachable"
    assert "Local dashboard is healthy" not in health["operator_action"]
    assert "port 7844" in health["operator_action"]
    assert health["local_gateway"]["status_code"] == 200
    assert health["public_domain"]["status_code"] == 530


def test_public_access_health_warns_when_public_deployment_is_stale(monkeypatch, tmp_path: Path):
    def fake_probe(_url, _timeout):
        return {"ok": True, "status_code": 200, "reason": "OK", "elapsed_ms": 1}

    monkeypatch.setattr(dashboard_server, "_probe_http", fake_probe)
    monkeypatch.setattr(
        dashboard_server,
        "_deployment_feature_summary",
        lambda _public_url, _timeout: {
            "status": "warn",
            "missing_features": ["trader:replay_source_timeframe_scope", "ops:ops_command_copy"],
            "checks": [],
        },
    )
    monkeypatch.setattr(dashboard_server, "_cloudflared_process_summary", lambda: {"running": True, "processes": []})

    health = build_public_access_health(
        public_url="https://goldbot.park-ai-intel.com/dashboard-v4.html",
        local_url="http://127.0.0.1:8766/dashboard-v4.html",
        log_path=tmp_path / "cloudflared.log",
    )

    assert health["status"] == "warn"
    assert health["diagnosis"] == "public_deployment_stale"
    assert health["deployment_features"]["status"] == "warn"
    assert "trader:replay_source_timeframe_scope" in health["operator_action"]
    assert health["public_domain"]["status_code"] == 200


def test_public_access_health_reports_public_ops_forbidden(monkeypatch, tmp_path: Path):
    def fake_probe(_url, _timeout):
        return {"ok": True, "status_code": 200, "reason": "OK", "elapsed_ms": 1}

    monkeypatch.setattr(dashboard_server, "_probe_http", fake_probe)
    monkeypatch.setattr(
        dashboard_server,
        "_deployment_feature_summary",
        lambda _public_url, _timeout: {
            "status": "warn",
            "reason": "ops_access_forbidden",
            "ops_access": "protected",
            "missing_features": [],
            "checks": [],
            "ops_url": "https://goldbot.park-ai-intel.com/ops-dashboard.html",
        },
    )
    monkeypatch.setattr(dashboard_server, "_cloudflared_process_summary", lambda: {"running": True, "processes": []})

    health = build_public_access_health(
        public_url="https://goldbot.park-ai-intel.com/dashboard-v4.html",
        local_url="http://127.0.0.1:8766/dashboard-v4.html",
        log_path=tmp_path / "cloudflared.log",
    )

    assert health["status"] == "warn"
    assert health["diagnosis"] == "public_ops_protected"
    assert "OPS dashboard returns 403" in health["operator_action"]
    assert health["deployment_features"]["ops_access"] == "protected"


def test_public_access_health_reports_public_ops_probe_failure(monkeypatch, tmp_path: Path):
    def fake_probe(_url, _timeout):
        return {"ok": True, "status_code": 200, "reason": "OK", "elapsed_ms": 1}

    monkeypatch.setattr(dashboard_server, "_probe_http", fake_probe)
    monkeypatch.setattr(
        dashboard_server,
        "_deployment_feature_summary",
        lambda _public_url, _timeout: {
            "status": "warn",
            "reason": "ops_probe_failed",
            "ops_access": "public",
            "missing_features": [],
            "checks": [],
            "ops_url": "https://goldbot.park-ai-intel.com/ops-dashboard.html",
        },
    )
    monkeypatch.setattr(dashboard_server, "_cloudflared_process_summary", lambda: {"running": True, "processes": []})

    health = build_public_access_health(
        public_url="https://goldbot.park-ai-intel.com/dashboard-v4.html",
        local_url="http://127.0.0.1:8766/dashboard-v4.html",
        log_path=tmp_path / "cloudflared.log",
    )

    assert health["status"] == "warn"
    assert health["diagnosis"] == "public_ops_probe_failed"
    assert "could not be fetched" in health["operator_action"]


def test_probe_failures_return_json_serializable_reason(monkeypatch):
    class TimeoutLike(Exception):
        reason = TimeoutError("timed out")

    def fake_urlopen(*_args, **_kwargs):
        raise TimeoutLike()

    monkeypatch.setattr(dashboard_server, "urlopen", fake_urlopen)

    probe = dashboard_server._probe_http("http://127.0.0.1:9999/dashboard-v4.html", 0.1)
    feature_probe = dashboard_server._probe_html_features(
        "http://127.0.0.1:9999/dashboard-v4.html",
        0.1,
        {"nav_detail_preload": "navDetailPreloadIds"},
    )

    assert probe["reason"] == "timed out"
    assert feature_probe["reason"] == "timed out"
    assert isinstance(probe["reason"], str)
    assert isinstance(feature_probe["reason"], str)


def test_deployment_feature_summary_reports_missing_trader_and_ops_features(monkeypatch):
    def fake_feature_probe(url, _timeout, features):
        checks = []
        for name in features:
            missing = "ops-dashboard.html" in url and name == "ops_command_copy"
            checks.append({"name": name, "ok": not missing})
        return {"ok": True, "status_code": 200, "checks": checks}

    monkeypatch.setattr(dashboard_server, "_probe_html_features", fake_feature_probe)
    monkeypatch.setattr(dashboard_server, "_probe_http", lambda _url, _timeout: {"ok": True, "status_code": 200})

    summary = dashboard_server._deployment_feature_summary("https://goldbot.park-ai-intel.com/dashboard-v4.html", 1)

    assert summary["status"] == "warn"
    assert summary["ops_url"] == "https://goldbot.park-ai-intel.com/ops-dashboard.html"
    assert summary["trader_vendor_url"] == "https://goldbot.park-ai-intel.com/data/vendor/echarts.min.js?v=20260627-gateway"
    assert summary["replay_url"] == "https://goldbot.park-ai-intel.com/dashboard-replay-v4.html"
    assert summary["replay_vendor_url"] == "https://goldbot.park-ai-intel.com/data/vendor/lightweight-charts.standalone.production.js"
    assert "ops:ops_command_copy" in summary["missing_features"]
    assert any(item["name"] == "nav_detail_preload" for item in summary["checks"])
    assert any(item["name"] == "trader_vendor_echarts" for item in summary["checks"])
    assert any(item["name"] == "replay_source_timeframe_scope" for item in summary["checks"])
    assert any(item["name"] == "dashboard_v4_title" for item in summary["checks"])
    assert any(item["name"] == "replay_v4_route" for item in summary["checks"])
    assert any(item["name"] == "replay_v4_body" for item in summary["checks"])
    assert any(item["name"] == "replay_vendor_lightweight_charts" for item in summary["checks"])
    assert any(item["name"] == "gold_cadence_nav_sampling" for item in summary["checks"])
    assert all(item["surface"] in {"trader", "replay", "ops"} for item in summary["checks"])


def test_deployment_feature_summary_recognizes_public_v5_console(monkeypatch):
    def fake_feature_probe(_url, _timeout, features):
        return {
            "ok": True,
            "status_code": 200,
            "checks": [{"name": name, "ok": True} for name in features],
        }

    monkeypatch.setattr(dashboard_server, "_probe_html_features", fake_feature_probe)
    monkeypatch.setattr(dashboard_server, "_probe_http", lambda _url, _timeout: {"ok": True, "status_code": 200})

    summary = dashboard_server._deployment_feature_summary("https://goldbot.park-ai-intel.com/dashboard-v5.html", 1)

    assert summary["status"] == "ok"
    assert summary["trader_vendor_url"] == "https://goldbot.park-ai-intel.com/packages/standard-kline/standard-kline.js"
    assert any(item["name"] == "dashboard_v5_title" for item in summary["checks"])
    assert any(item["name"] == "strategy_console_api" for item in summary["checks"])
    assert any(item["name"] == "access_session_api" for item in summary["checks"])
    assert any(item["name"] == "authenticated_control_gate" for item in summary["checks"])
    assert any(item["name"] == "trader_vendor_standard_kline" for item in summary["checks"])


def test_deployment_feature_summary_warns_when_replay_page_is_missing(monkeypatch):
    def fake_feature_probe(url, _timeout, features):
        if "dashboard-replay-v4.html" in url:
            return {
                "ok": False,
                "status_code": 404,
                "reason": "Not Found",
                "checks": [{"name": name, "ok": False} for name in features],
            }
        return {
            "ok": True,
            "status_code": 200,
            "reason": "OK",
            "checks": [{"name": name, "ok": True} for name in features],
        }

    monkeypatch.setattr(dashboard_server, "_probe_html_features", fake_feature_probe)
    monkeypatch.setattr(dashboard_server, "_probe_http", lambda _url, _timeout: {"ok": True, "status_code": 200})

    summary = dashboard_server._deployment_feature_summary("https://goldbot.park-ai-intel.com/dashboard-v4.html", 1)

    assert summary["status"] == "warn"
    assert summary["reason"] == "replay_probe_failed"
    assert summary["replay"]["status_code"] == 404
    assert not any(item.startswith("replay:") for item in summary["missing_features"])


def test_deployment_feature_summary_warns_when_replay_vendor_is_missing(monkeypatch):
    def fake_feature_probe(_url, _timeout, features):
        return {
            "ok": True,
            "status_code": 200,
            "reason": "OK",
            "checks": [{"name": name, "ok": True} for name in features],
        }

    monkeypatch.setattr(dashboard_server, "_probe_html_features", fake_feature_probe)
    monkeypatch.setattr(
        dashboard_server,
        "_probe_http",
        lambda url, _timeout: {"ok": False, "status_code": 404} if "lightweight-charts" in url else {"ok": True, "status_code": 200},
    )

    summary = dashboard_server._deployment_feature_summary("https://goldbot.park-ai-intel.com/dashboard-v4.html", 1)

    assert summary["status"] == "warn"
    assert summary["reason"] == "replay_vendor_probe_failed"
    assert summary["replay_vendor"]["status_code"] == 404
    assert not any(item == "replay:replay_vendor_lightweight_charts" for item in summary["missing_features"])


def test_deployment_feature_summary_warns_when_trader_vendor_is_missing(monkeypatch):
    def fake_feature_probe(_url, _timeout, features):
        return {
            "ok": True,
            "status_code": 200,
            "reason": "OK",
            "checks": [{"name": name, "ok": True} for name in features],
        }

    monkeypatch.setattr(dashboard_server, "_probe_html_features", fake_feature_probe)
    monkeypatch.setattr(
        dashboard_server,
        "_probe_http",
        lambda url, _timeout: {"ok": False, "status_code": 404} if "echarts.min.js" in url else {"ok": True, "status_code": 200},
    )

    summary = dashboard_server._deployment_feature_summary("https://goldbot.park-ai-intel.com/dashboard-v4.html", 1)

    assert summary["status"] == "warn"
    assert summary["reason"] == "trader_vendor_probe_failed"
    assert summary["trader_vendor"]["status_code"] == 404
    assert not any(item == "trader:trader_vendor_echarts" for item in summary["missing_features"])


def test_deployment_feature_summary_treats_public_ops_403_as_protected(monkeypatch):
    def fake_feature_probe(url, _timeout, features):
        if "ops-dashboard.html" in url:
            return {
                "ok": False,
                "status_code": 403,
                "reason": "Forbidden",
                "checks": [{"name": name, "ok": False} for name in features],
            }
        return {
            "ok": True,
            "status_code": 200,
            "reason": "OK",
            "checks": [{"name": name, "ok": True} for name in features],
        }

    monkeypatch.setattr(dashboard_server, "_probe_html_features", fake_feature_probe)
    monkeypatch.setattr(dashboard_server, "_probe_http", lambda _url, _timeout: {"ok": True, "status_code": 200})

    summary = dashboard_server._deployment_feature_summary("https://goldbot.park-ai-intel.com/dashboard-v4.html", 1)

    assert summary["status"] == "warn"
    assert summary["reason"] == "ops_access_forbidden"
    assert summary["ops_access"] == "protected"
    assert summary["ops"]["protected"] is True
    assert summary["ops"]["status_code"] == 403
    assert not any(item.startswith("ops:") for item in summary["missing_features"])


def test_deployment_feature_summary_treats_public_ops_timeout_as_probe_failure(monkeypatch):
    def fake_feature_probe(url, _timeout, features):
        if "ops-dashboard.html" in url:
            return {
                "ok": False,
                "status_code": None,
                "reason": "_ssl.c:1112: The handshake operation timed out",
                "checks": [{"name": name, "ok": False} for name in features],
            }
        return {
            "ok": True,
            "status_code": 200,
            "reason": "OK",
            "checks": [{"name": name, "ok": True} for name in features],
        }

    monkeypatch.setattr(dashboard_server, "_probe_html_features", fake_feature_probe)
    monkeypatch.setattr(dashboard_server, "_probe_http", lambda _url, _timeout: {"ok": True, "status_code": 200})

    summary = dashboard_server._deployment_feature_summary("https://goldbot.park-ai-intel.com/dashboard-v4.html", 1)

    assert summary["status"] == "warn"
    assert summary["reason"] == "ops_probe_failed"
    assert summary["ops_access"] == "public"
    assert not any(item.startswith("ops:") for item in summary["missing_features"])


def test_dashboard_handler_disables_cache_for_dashboard_html():
    handler = object.__new__(dashboard_server.DashboardHandler)

    for path in [
        "/dashboard-v2.html",
        "/dashboard-v3.html",
        "/dashboard-v4.html",
        "/dashboard-v5.html",
        "/dashboard.html",
        "/dashboard-replay.html",
        "/dashboard-replay-v4.html",
        "/ops-dashboard.html?v=123",
        "/assets/shell.js",
        "/assets/shell.css?v=20260710",
        "/packages/standard-kline/standard-kline.js",
    ]:
        handler.path = path
        assert handler._should_disable_static_cache() is True

    for path in ["/api/dashboard", "/api/public-access-health", "/outputs/state.json", "/"]:
        handler.path = path
        assert handler._should_disable_static_cache() is False


def test_dashboard_v5_is_a_stable_alias_for_the_production_strategy_console():
    source = Path(dashboard_server.__file__).read_text(encoding="utf-8")

    assert 'if parsed.path == "/dashboard-v5.html":' in source
    assert 'self._serve_static_alias("/dashboard-gridmind.html")' in source
    assert 'https://goldbot.park-ai-intel.com/dashboard-v5.html' in source
    assert 'http://127.0.0.1:8766/dashboard-v5.html' in source


def test_strategy_console_production_history_keeps_prior_versioned_trades(tmp_path: Path):
    output = tmp_path / "outputs"
    cycle_id = "2026-07-04_NIGHT"
    write_json(output / "dualtrack" / "fills" / f"{cycle_id}_human.json", [
        {
            "fill_id": "entry-1",
            "cycle_id": cycle_id,
            "trade_id": "trade-1",
            "event": "entry",
            "side": "buy",
            "ts": "2026-07-04T13:10:00+00:00",
            "price": 100.0,
            "pnl_units": 10.0,
            "notional": 1000.0,
            "cost": 0.05,
            "realized_pnl": -0.05,
            "remaining_units": 0.0,
            "position_status": "closed",
            "strategy_plan_id": "strategy-plan-2026-07-04_NIGHT-2-test",
            "strategy_plan_version": 2,
            "source": "strategy_production_console",
        },
        {
            "fill_id": "exit-1",
            "cycle_id": cycle_id,
            "trade_id": "trade-1",
            "event": "target",
            "side": "sell",
            "ts": "2026-07-04T13:20:00+00:00",
            "price": 101.0,
            "pnl_units": 10.0,
            "notional": 1010.0,
            "cost": 0.05,
            "realized_pnl": 9.95,
            "matched_entries": [{"trade_id": "trade-1", "units": 10.0, "gross_pnl": 10.0, "realized_pnl": 9.95}],
        },
        {
            "fill_id": "legacy-only",
            "cycle_id": cycle_id,
            "trade_id": "legacy-trade",
            "event": "entry",
            "side": "sell",
            "ts": "2026-07-04T13:30:00+00:00",
            "price": 102.0,
            "pnl_units": 1.0,
            "notional": 102.0,
            "source": "split_canvas",
        },
    ])

    result = dashboard_server.build_strategy_console_production_history(
        output_root=output,
        mark_price=101.0,
        mark_fresh=True,
    )

    assert result["summary"]["trade_count"] == 1
    assert result["summary"]["completed_trade_count"] == 1
    assert result["summary"]["fill_count"] == 2
    assert result["summary"]["entry_fill_count"] == 1
    assert result["summary"]["exit_fill_count"] == 1
    assert result["summary"]["total_notional"] == 2010.0
    assert result["summary"]["realized_pnl"] == 9.9
    assert result["account"]["starting_cash"] == 10_000
    assert result["account"]["ending_cash"] == 10_009.9
    assert result["trades"][0]["strategy_plan_version"] == 2
    assert result["trades"][0]["source_cycle_id"] == cycle_id
    assert result["accounting_snapshot"]["schema_version"] == "accounting-snapshot-v1"
    assert result["accounting_snapshot"]["counts"]["trade_count"] == 1
    assert result["accounting_snapshot"]["counts"]["completed_trade_count"] == 1
    assert result["accounting_projection_receipt"]["status"] == "pass"
    assert result["accounting_projection_receipt"]["compatibility_fields_source"] == "accounting-snapshot-v1"


def test_strategy_console_history_combines_legacy_archive_with_authoritative_nautilus_only(tmp_path: Path):
    output = tmp_path / "outputs"
    legacy_cycle = "2026-07-04_NIGHT"
    write_json(output / "dualtrack" / "fills" / f"{legacy_cycle}_human.json", [
        {
            "fill_id": "legacy-entry",
            "cycle_id": legacy_cycle,
            "trade_id": "legacy-trade",
            "event": "entry",
            "side": "buy",
            "ts": "2026-07-04T13:10:00+00:00",
            "price": 100.0,
            "pnl_units": 1.0,
            "notional": 100.0,
            "cost": 0.1,
            "realized_pnl": -0.1,
            "strategy_plan_id": "legacy-plan",
            "strategy_plan_version": 1,
        },
        {
            "fill_id": "legacy-exit",
            "cycle_id": legacy_cycle,
            "trade_id": "legacy-trade",
            "event": "target",
            "side": "sell",
            "ts": "2026-07-04T13:20:00+00:00",
            "price": 110.0,
            "pnl_units": 1.0,
            "notional": 110.0,
            "cost": 0.1,
            "realized_pnl": 9.9,
        },
    ])
    active_cycle = "2026-07-05_DAY"
    write_json(
        output / "dualtrack" / "nautilus_authoritative" / "snapshots" / f"{active_cycle}.json",
        [{
            "engine": "nautilus_paper",
            "cycle_id": active_cycle,
            "fills": [
                {
                    "fill_id": "nautilus-entry",
                    "trade_id": "nautilus-open",
                    "event": "entry",
                    "side": "buy",
                    "ts": "2026-07-05T01:00:00+00:00",
                    "price": 100.0,
                    "quantity": 2.0,
                    "strategy_plan_id": "nautilus-plan",
                    "strategy_plan_version": 2,
                },
            ],
            "positions": [
                {
                    "trade_id": "nautilus-open",
                    "position_id": "POS-nautilus-open",
                    "status": "open",
                    "side": "long",
                    "remaining_units": 2.0,
                    "entry_price": 100.0,
                    "entry_ts": "2026-07-05T01:00:00+00:00",
                    "realized_pnl": 4.0,
                    "strategy_plan_id": "nautilus-plan",
                    "strategy_plan_version": 2,
                },
            ],
        }],
    )
    write_json(
        output / "dualtrack" / "nautilus_paper" / "snapshots" / f"{active_cycle}.json",
        [{
            "engine": "nautilus_paper",
            "cycle_id": active_cycle,
            "fills": [{
                "fill_id": "shadow-must-not-leak",
                "strategy_plan_id": "shadow-plan",
            }],
            "positions": [],
        }],
    )

    result = dashboard_server.build_strategy_console_production_history(
        output_root=output,
        mark_price=105.0,
        mark_fresh=True,
        authoritative_engine="nautilus_paper",
    )

    assert result["summary"]["trade_count"] == 2
    assert result["summary"]["completed_trade_count"] == 1
    assert result["summary"]["open_trade_count"] == 1
    assert result["summary"]["fill_count"] == 3
    assert result["summary"]["realized_pnl"] == 13.8
    assert result["summary"]["unrealized_pnl"] == 10.0
    assert result["account"]["ending_cash"] == 10_013.8
    assert result["account"]["equity"] == 10_023.8
    assert {row["fill_id"] for row in result["fills"]} == {
        "legacy-entry", "legacy-exit", "nautilus-entry",
    }
    assert result["history_contract"]["source"] == "versioned_strategy_plan_and_nautilus_authoritative"
    assert result["history_contract"]["nautilus_shadow_excluded"] is True


def test_strategy_console_history_repairs_uniquely_matched_legacy_nautilus_flatten(tmp_path: Path):
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    entry_trade_id = "nautilus-command-entry"
    flatten_command_id = "nautilus-command-flatten"
    write_json(
        output / "dualtrack" / "nautilus_authoritative" / "snapshots" / f"{cycle_id}.json",
        [{
            "engine": "nautilus_paper",
            "cycle_id": cycle_id,
            "fills": [
                {
                    "fill_id": "nautilus-entry",
                    "order_id": entry_trade_id,
                    "trade_id": entry_trade_id,
                    "event": "entry",
                    "side": "buy",
                    "ts": "2026-07-05T01:00:00+00:00",
                    "price": 100.0,
                    "quantity": 2.0,
                    "strategy_plan_id": "nautilus-plan",
                    "strategy_plan_version": 2,
                },
                {
                    "fill_id": "nautilus-flatten",
                    "order_id": flatten_command_id,
                    "trade_id": flatten_command_id,
                    "event": "flatten",
                    "side": "sell",
                    "ts": "2026-07-05T02:00:00+00:00",
                    "price": 105.0,
                    "quantity": 2.0,
                    "gross_pnl": 10.0,
                    "cost": 0.2,
                    "realized_pnl": 9.8,
                    "strategy_plan_id": "nautilus-plan",
                    "strategy_plan_version": 2,
                },
            ],
            "positions": [{
                "trade_id": entry_trade_id,
                "position_id": f"POS-{entry_trade_id}",
                "status": "closed",
                "side": "long",
                "quantity": 2.0,
                "remaining_units": 0.0,
                "entry_price": 100.0,
                "exit_price": 105.0,
                "entry_ts": "2026-07-05T01:00:00+00:00",
                "exit_ts": "2026-07-05T02:00:00+00:00",
                "realized_pnl": 9.8,
                "strategy_plan_id": "nautilus-plan",
                "strategy_plan_version": 2,
            }],
        }],
    )

    result = dashboard_server.build_strategy_console_production_history(
        output_root=output,
        mark_price=105.0,
        mark_fresh=True,
        authoritative_engine="nautilus_paper",
    )

    accounting = result["accounting_snapshot"]
    assert accounting["source_name"] == "nautilus_paper"
    assert accounting["reconciliation"]["status"] == "pass"
    assert accounting["reconciliation"]["issues"] == []
    flatten = next(row for row in accounting["fills"] if row["event"] == "flatten")
    assert flatten["trade_id"] == entry_trade_id
    assert flatten["source_trade_id"] == flatten_command_id
    assert flatten["identity_resolution"] == "legacy_flatten_unique_closed_position"


def test_strategy_console_production_accounting_keeps_partial_close_open_and_unknown_mark_unknown(tmp_path: Path):
    output = tmp_path / "outputs"
    cycle_id = "2026-07-04_NIGHT"
    write_json(output / "dualtrack" / "fills" / f"{cycle_id}_human.json", [
        {
            "fill_id": "entry-1",
            "cycle_id": cycle_id,
            "trade_id": "trade-1",
            "event": "entry",
            "side": "buy",
            "ts": "2026-07-04T13:10:00+00:00",
            "price": 100.0,
            "pnl_units": 2.0,
            "notional": 200.0,
            "cost": 0.2,
            "realized_pnl": -0.2,
            "remaining_units": 1.0,
            "position_status": "open",
            "strategy_plan_id": "plan-1",
            "strategy_plan_version": 1,
        },
        {
            "fill_id": "exit-1",
            "cycle_id": cycle_id,
            "trade_id": "trade-1",
            "event": "exit",
            "side": "sell",
            "ts": "2026-07-04T13:20:00+00:00",
            "price": 105.0,
            "pnl_units": 1.0,
            "notional": 105.0,
            "cost": 0.1,
            "gross_pnl": 5.0,
            "realized_pnl": 4.9,
            "matched_entries": [{"trade_id": "trade-1", "units": 1.0, "gross_pnl": 5.0, "realized_pnl": 4.9}],
        },
    ])

    result = dashboard_server.build_strategy_console_production_history(
        output_root=output,
        mark_price=None,
        mark_fresh=False,
    )

    assert result["summary"]["trade_count"] == 1
    assert result["summary"]["open_trade_count"] == 1
    assert result["summary"]["completed_trade_count"] == 0
    assert result["summary"]["fill_count"] == 2
    assert result["summary"]["unrealized_pnl"] is None
    assert result["account"]["equity"] is None
    assert result["accounting_snapshot"]["pnl"]["unrealized_pnl"] is None
    assert result["accounting_snapshot"]["completeness"]["status"] == "partial"


def test_compact_strategy_payload_keeps_replay_fields_and_drops_ops_bulk():
    payload = {
        "contract": {"version": 1},
        "run_date": "2026-06-22",
        "strategy_id": "gold_1m_bollinger_reversion",
        "performance_board": {"large": True},
        "signals": [{"raw": True}],
        "risk_monitor": {"ops": True},
        "strategy_detail": {
            "strategy_id": "gold_1m_bollinger_reversion",
            "timeframe": "1m",
            "classification": {"family": "mean_reversion"},
            "summary": {"open_trade_count": 1},
            "bars": [
                {
                    "symbol": "GOLD",
                    "timeframe": "1m",
                    "timestamp": "2026-06-22T00:00:00+00:00",
                    "open": 4200,
                    "high": 4210,
                    "low": 4195,
                    "close": 4204,
                    "volume": 99,
                    "provider": "binance_usdm",
                    "quality_flags": ["execution_venue"],
                }
            ],
            "replay_ohlc": {
                "source": "market_data_db",
                "requested_timeframe": "1m",
                "timeframe": "5m",
                "bar_count": 900,
            },
            "ohlc_quality": {"provider": "binance_usdm"},
            "nav_points": [{"equity": 10001}],
            "nav_quality": {"status": "low_confidence", "source": "paper_equity_curve_checkpoints"},
            "nav_curve_intraday": {
                "status": "pass",
                "source": "5m_mark_to_market",
                "point_count": 2,
                "starting_equity": 10000,
                "current_equity": 10006,
                "current_drawdown_pct": 0,
                "max_drawdown_pct": -0.1,
                "points": [
                    {
                        "timestamp": "2026-06-22T00:00:00+00:00",
                        "close": 4204,
                        "equity": 10000,
                        "realized_pnl": 0,
                        "unrealized_pnl": 0,
                        "active_trade_count": 0,
                        "drawdown_pct": 0,
                        "raw_debug": True,
                    },
                    {
                        "timestamp": "2026-06-22T00:05:00+00:00",
                        "close": 4210,
                        "equity": 10006,
                        "realized_pnl": 0,
                        "unrealized_pnl": 6,
                        "active_trade_count": 1,
                        "drawdown_pct": -0.1,
                    },
                ],
                "raw_debug": True,
            },
            "gold_nav": {"points": [{"close": 4204}]},
            "orders": [
                {
                    "order_id": "order_1",
                    "ticket_id": "ticket_1",
                    "status": "filled",
                    "fill_price": 4204,
                    "commission": 1.23,
                    "cost_model": {"debug": True},
                }
            ],
            "open_orders": [],
            "open_trades": [],
            "closed_trades": [],
            "trades": [
                {
                    "trade_id": "trade_1",
                    "order_id": "order_1",
                    "ticket_id": "ticket_1",
                    "side": "long",
                    "status": "open",
                    "entry_price": 4204,
                    "target": 4300,
                    "stop_loss": 4180,
                    "opened_at": "2026-06-22T00:01:00+00:00",
                    "source_artifacts": {"debug": True},
                    "strategy_signal": {
                        "signal_id": "signal_1",
                        "regime": "bollinger_reversion",
                        "thesis": "mean reversion",
                        "evidence": ["close below lower band"],
                        "artifact_provenance": {"status": "repaired_from_paper_trade", "raw_debug": True},
                        "raw_debug": {"large": True},
                    },
                    "decision": {"risk_snapshot": {"max_loss_pct": -0.5}, "raw": True},
                    "exit_decision": {"required_user_action": "hold", "latest_price": 4210, "raw": True},
                }
            ],
            "explainability_gaps": [{"raw": True}],
            "explainability_gap_groups": [{"key": "missing_signal"}],
            "explainability_status": "warn",
            "explainability_root_cause_groups": [{
                "key": "artifact_provenance_missing",
                "root_cause": "artifact_provenance_missing",
                "label": "artifact provenance missing",
                "severity": "warn",
                "count": 3,
                "warn_count": 3,
                "next_action": "repair_same_day_signal_ticket_order_artifacts",
                "gap_types": ["missing_signal_artifact", "missing_ticket_artifact"],
                "sample_trade_ids": ["trade_1", "trade_2", "trade_3", "trade_4"],
                "sample_ticket_ids": ["ticket_1", "ticket_2", "ticket_3", "ticket_4"],
                "sample_signal_ids": ["signal_1", "signal_2", "signal_3", "signal_4"],
            }],
            "performance_confidence": {"status": "open_pnl_only"},
            "unrealized_pnl": 6,
            "entry_reason": "bollinger_reversion",
            "exit_reason": "manual_exit",
            "strategy_signal": {"signal_id": "signal_latest", "raw_debug": True},
            "latest_decision_snapshot": {
                "strategy_id": "gold_1m_bollinger_reversion",
                "bar_timestamp": "2026-06-22T00:06:00+00:00",
                "generated_at": "2026-06-22T00:06:02+00:00",
                "final_decision": "no_go",
                "signal": {"direction": "watch", "confidence": 0, "raw_debug": True},
                "execution_plan": {"raw_debug": True},
                "no_go_reason": "no trigger",
                "raw_debug": True,
            },
            "latest_go_decision_snapshot": {
                "strategy_id": "gold_1m_bollinger_reversion",
                "bar_timestamp": "2026-06-22T00:01:00+00:00",
                "generated_at": "2026-06-22T00:01:02+00:00",
                "final_decision": "go",
                "signal": {"direction": "long", "confidence": 61, "strength": 72, "regime": "bollinger_reversion", "raw_debug": True},
                "execution_plan": {"ticket_id": "ticket_1", "entry_zone": "4200-4205", "take_profit": 4300, "stop_loss": 4180, "target_equity_return_pct": 10, "raw_debug": True},
                "raw_debug": True,
            },
            "decision_snapshot_summary": {"count": 2, "go_count": 1, "no_go_count": 1, "latest_go_timestamp": "2026-06-22T00:01:00+00:00"},
            "risk_block": {"status": "clear", "raw_debug": True},
            "risk_monitor": {"ops": True},
            "paper_exit_decisions": {"ops": True},
            "review_events": [{"ops": True}],
            "source_contract": {"section": "strategy_detail"},
        },
    }

    compact = compact_strategy_payload(payload)
    detail = compact["strategy_detail"]

    assert compact["strategy_id"] == "gold_1m_bollinger_reversion"
    assert "performance_board" not in compact
    assert "signals" not in compact
    assert detail["bars"][0] == {
        "timestamp": "2026-06-22T00:00:00+00:00",
        "open": 4200,
        "high": 4210,
        "low": 4195,
        "close": 4204,
        "provider": "binance_usdm",
        "quality_flags": ["execution_venue"],
    }
    assert detail["replay_ohlc"] == {
        "source": "market_data_db",
        "requested_timeframe": "1m",
        "timeframe": "5m",
        "bar_count": 900,
    }
    assert "gold_nav" not in detail
    assert detail["nav_quality"] == {"status": "low_confidence", "source": "paper_equity_curve_checkpoints"}
    assert detail["nav_curve_intraday"]["source"] == "5m_mark_to_market"
    assert detail["nav_curve_intraday"]["point_count"] == 2
    assert detail["nav_curve_intraday"]["points"][1] == {
        "timestamp": "2026-06-22T00:05:00+00:00",
        "close": 4210,
        "equity": 10006,
        "realized_pnl": 0,
        "unrealized_pnl": 6,
        "active_trade_count": 1,
        "drawdown_pct": -0.1,
    }
    assert detail["decision_snapshot_summary"]["go_count"] == 1
    assert detail["latest_go_decision_snapshot"] == {
        "strategy_id": "gold_1m_bollinger_reversion",
        "bar_timestamp": "2026-06-22T00:01:00+00:00",
        "generated_at": "2026-06-22T00:01:02+00:00",
        "final_decision": "go",
        "signal": {"direction": "long", "confidence": 61, "strength": 72, "regime": "bollinger_reversion"},
        "execution_plan": {
            "ticket_id": "ticket_1",
            "entry_zone": "4200-4205",
            "take_profit": 4300,
            "stop_loss": 4180,
            "target_equity_return_pct": 10,
        },
        "no_go_reason": "",
    }
    assert detail["latest_decision_snapshot"]["bar_timestamp"] == "2026-06-22T00:06:00+00:00"
    assert "raw_debug" not in detail["latest_go_decision_snapshot"]["signal"]
    assert "raw_debug" not in detail["latest_go_decision_snapshot"]["execution_plan"]
    assert "raw_debug" not in detail["nav_curve_intraday"]
    assert "raw_debug" not in detail["nav_curve_intraday"]["points"][0]
    assert "risk_monitor" not in detail
    assert "paper_exit_decisions" not in detail
    assert "review_events" not in detail
    assert "explainability_gaps" not in detail
    assert "explainability_gap_groups" not in detail
    assert "explainability_root_cause_groups" not in detail
    assert detail["explainability_summary"] == {
        "status": "warn",
        "verdict": "not_promotion_ready",
        "gap_count": 3,
        "warn_count": 3,
        "info_count": 0,
        "root_cause_count": 1,
        "groups": [{
            "root_cause": "artifact_provenance_missing",
            "label": "artifact provenance missing",
            "severity": "warn",
            "count": 3,
            "warn_count": 3,
            "info_count": 0,
            "next_action": "repair_same_day_signal_ticket_order_artifacts",
            "gap_types": ["missing_signal_artifact", "missing_ticket_artifact"],
            "sample_trade_ids": ["trade_1", "trade_2", "trade_3"],
            "sample_ticket_ids": ["ticket_1", "ticket_2", "ticket_3"],
            "sample_signal_ids": ["signal_1", "signal_2", "signal_3"],
        }],
    }
    assert "source_contract" not in detail
    assert detail["orders"][0] == {
        "order_id": "order_1",
        "ticket_id": "ticket_1",
        "status": "filled",
        "fill_price": 4204,
    }
    trade = detail["trades"][0]
    assert trade["entry_price"] == 4204
    assert trade["strategy_signal"]["thesis"] == "mean reversion"
    assert trade["strategy_signal"]["artifact_provenance"]["status"] == "repaired_from_paper_trade"
    assert "raw_debug" not in trade["strategy_signal"]["artifact_provenance"]
    assert "raw_debug" not in trade["strategy_signal"]
    assert trade["decision"] == {"risk_snapshot": {"max_loss_pct": -0.5}}
    assert trade["exit_decision"] == {"required_user_action": "hold", "latest_price": 4210}
    assert "source_artifacts" not in trade
