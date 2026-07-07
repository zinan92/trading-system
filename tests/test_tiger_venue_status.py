from __future__ import annotations

from pathlib import Path

from services.journal_store import load_json, write_json
from services.tiger_venue_status import TigerVenueStatus


def _write_ready_artifacts(root: Path) -> None:
    write_json(
        root / "tiger_reconciliation" / "current.json",
        [
            {
                "confirmation_status": "confirmed_flat",
                "system_state": "READY",
                "reason_code": "confirmed_flat",
                "drift_count": 0,
                "can_open_new_orders": True,
                "exchange_positions": [],
                "exchange_open_orders": [],
                "checked_at": "2026-07-05T11:16:11+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_order_sync" / "current.json",
        [
            {
                "sync_status": "synced",
                "error": "",
                "open_order_count": 0,
                "filled_order_count": 1,
                "exchange_open_orders": [],
                "exchange_filled_orders": [
                    {
                        "symbol": "MGC2608",
                        "order_id": "8001",
                        "side": "BUY",
                        "type": "LMT",
                        "status": "FILLED",
                        "quantity": 1.0,
                        "filled_quantity": 1.0,
                        "average_fill_price": 4186.5,
                        "filled_at": "2026-07-05T01:02:03+00:00",
                        "source": "tiger_filled_orders",
                        "raw": {"account": "DU123456"},
                    }
                ],
                "checked_at": "2026-07-05T11:17:14+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_kill_switch" / "current.json",
        [
            {
                "status": "dry_run",
                "network_order_created": False,
                "network_cancel_created": False,
                "network_actions": {"enabled": False, "ready": False},
                "checked_at": "2026-07-05T11:16:11+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_contracts" / "current.json",
        [
            {
                "resolution": {
                    "requested_symbol": "MGCmain",
                    "execution_symbol": "MGC2608",
                    "status": "ready",
                    "ready": True,
                    "days_to_contract_month": 27,
                    "block_reason": "",
                },
                "checked_at": "2026-07-05T11:18:00+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_account_sync" / "current.json",
        [
            {
                "sync_status": "synced",
                "error": "",
                "account_observation": {
                    "account_observed": True,
                    "balance_present": True,
                    "accounting_observed": True,
                    "segment_key": "C",
                },
                "exchange_balance": {"asset": "USD", "balance": 25000.0, "available": 24000.0, "balance_present": True},
                "exchange_accounting": {"net_realized_pnl_estimate": 0.0, "unrealized_pnl_estimate": 0.0},
                "checked_at": "2026-07-05T11:18:30+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_paper_order_drill" / "current.json",
        [
            {
                "status": "pass",
                "run_id": "drilltest",
                "mode": "local_fake_tradeclient",
                "real_tiger_network_call_attempted": False,
                "scenarios": [{"name": "default_guardrail_block", "status": "pass"}, {"name": "simulated_green_order", "status": "pass"}],
                "finished_at": "2026-07-05T11:19:00+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_paper_order_readiness" / "current.json",
        [
            {
                "status": "ready_for_attended_paper_order",
                "ready_for_attended_paper_order": True,
                "can_submit_without_explicit_operator_authorization": False,
                "real_tiger_network_call_attempted": False,
                "checks": [{"name": "paper_order_drill", "status": "pass"}],
                "blockers": [],
                "checked_at": "2026-07-05T11:19:30+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_paper_order_approval" / "current.json",
        [
            {
                "status": "ready_for_operator_approval",
                "ticket_id": "tiger_m17_mgc2608_approval",
                "operator": "codex",
                "submit_requested": False,
                "can_submit_without_explicit_operator_authorization": False,
                "real_tiger_network_call_attempted": False,
                "use_attended_canary_risk_limits": True,
                "checks": [{"name": "canary_check", "status": "pass"}],
                "blockers": [],
                "canary_check": {
                    "status": "ready_for_operator_authorization",
                    "candidate_notional": 41860.0,
                    "candidate_stop_loss": 160.0,
                    "candidate_stop_loss_pct_of_equity": 2.14242579,
                },
                "submit_command": "redacted in venue summary",
                "checked_at": "2026-07-05T11:20:00+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_realtime_validation" / "current.json",
        [
            {
                "schema_version": "tiger-realtime-validation-v1",
                "status": "pending_market_open",
                "message": "COMEX futures session is not trading at validation time; rerun during the next trading window.",
                "contract": "MGCmain",
                "output_symbol": "MGCmain",
                "timeframe": "1m",
                "checked_at": "2026-07-05T14:49:22+00:00",
                "poll_seconds": 0.0,
                "max_lag_seconds": 180.0,
                "next_trading_window": {
                    "start": "2026-07-05T22:00:00+00:00",
                    "end": "2026-07-06T21:00:00+00:00",
                    "trading_date": "2026-07-06",
                },
                "market_hours_gate": {
                    "required": True,
                    "ready_for_price_feed_promotion": False,
                    "market_hours_observed": False,
                    "exit_code": 75,
                    "operator_action": "rerun_after_next_trading_window",
                    "next_trading_window": {
                        "start": "2026-07-05T22:00:00+00:00",
                        "end": "2026-07-06T21:00:00+00:00",
                        "trading_date": "2026-07-06",
                    },
                },
                "preflight": {"checks": {"quote_permission": {"names": ["aStockQuoteLv1"], "has_futures_realtime": False}}},
                "safety": {
                    "read_only": True,
                    "writes_market_db": False,
                    "opens_trade_client": False,
                    "submits_orders": False,
                },
            }
        ],
    )
    write_json(
        root / "tiger_price_feed_readiness" / "current.json",
        [
            {
                "schema_version": "tiger-price-feed-readiness-v1",
                "run_date": "2026-07-05",
                "checked_at": "2026-07-05T15:44:29+00:00",
                "provider": "tiger_openapi",
                "venue": "COMEX",
                "contract": "MGCmain",
                "status": "blocked",
                "ready_for_price_feed": False,
                "can_enable_broker_orders_from_this_gate": False,
                "blockers": [{"name": "realtime_market_hours_gate", "status": "fail"}],
                "summary": {
                    "feed_status": "pass",
                    "imported_rows": 500,
                    "latest_timestamp": "2026-07-03T16:59:00+00:00",
                    "realtime_status": "pending_market_open",
                    "market_hours_gate": {
                        "exit_code": 75,
                        "operator_action": "rerun_after_next_trading_window",
                        "ready_for_price_feed_promotion": False,
                    },
                },
                "safety": {
                    "artifact_only": True,
                    "opens_tiger_sdk_clients": False,
                    "writes_market_db": False,
                    "submits_orders": False,
                },
            }
        ],
    )
    write_json(
        root / "tiger_price_feed_acceptance" / "current.json",
        [
            {
                "schema_version": "tiger-price-feed-acceptance-v1",
                "run_date": "2026-07-05",
                "checked_at": "2026-07-05T16:18:30+00:00",
                "provider": "tiger_openapi",
                "venue": "COMEX",
                "contract": "MGCmain",
                "status": "pending_market_open",
                "exit_code": 75,
                "ready_for_price_feed": False,
                "can_enable_broker_orders_from_this_gate": False,
                "operator_next_action": {
                    "status": "waiting_market_open",
                    "summary": "Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance.",
                    "next_command": "python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-05 --contract MGCmain --poll-seconds 75 --json",
                    "next_trading_window": {
                        "start": "2026-07-05T22:00:00+00:00",
                        "end": "2026-07-06T21:00:00+00:00",
                        "trading_date": "2026-07-06",
                    },
                },
                "steps": {
                    "realtime_validation": {
                        "status": "pending_market_open",
                        "market_hours_gate": {
                            "required": True,
                            "ready_for_price_feed_promotion": False,
                            "market_hours_observed": False,
                            "exit_code": 75,
                            "operator_action": "rerun_after_next_trading_window",
                            "next_trading_window": {
                                "start": "2026-07-05T22:00:00+00:00",
                                "end": "2026-07-06T21:00:00+00:00",
                                "trading_date": "2026-07-06",
                            },
                        },
                    },
                    "price_feed_readiness": {"status": "blocked", "ready_for_price_feed": False, "blocker_count": 1},
                    "connector_catalog": {
                        "tiger_price_feed_status": "blocked",
                        "tiger_broker_order_status": "ready",
                    },
                },
                "blockers": [{"name": "realtime_market_hours_gate", "status": "fail"}],
                "safety": {
                    "read_only": True,
                    "opens_quote_client": True,
                    "opens_trade_client": False,
                    "submits_orders": False,
                    "writes_market_db": False,
                    "credential_values_exposed": False,
                },
            }
        ],
    )
    write_json(
        root / "data_source_preflight" / "MGCmain_1m" / "current.json",
        [
            {
                "run_date": "2026-07-05",
                "checked_at": "2026-07-05T16:50:00+00:00",
                "symbol": "MGCmain",
                "timeframe": "1m",
                "source_key": "MGCmain_1m",
                "status": "fail",
                "ready_for_paper": False,
                "ready_for_live": False,
                "live_data_mode": "not_live_ready",
                "latest_provider": "tiger_openapi:COMEX",
                "latest_timestamp": "2026-07-03T16:59:00+00:00",
                "latest_price": 4186.9,
                "latest_record_age_minutes": 2871.5,
                "execution_venue_rows": 500,
                "message": "latest MGCmain quote/bar is stale by 2871.5 minutes; refresh market data before paper or live trading",
                "execution_venue_readiness_gate": {
                    "required": True,
                    "provider": "tiger_openapi",
                    "status": "pending_market_open",
                    "allows_live": False,
                    "summary": "Tiger OpenAPI price feed is waiting for market-hours acceptance; rerun after 2026-07-05T22:00:00+00:00.",
                    "evidence": {
                        "acceptance": {
                            "next_trading_window": {
                                "start": "2026-07-05T22:00:00+00:00",
                                "end": "2026-07-06T21:00:00+00:00",
                                "trading_date": "2026-07-06",
                            }
                        }
                    },
                },
            }
        ],
    )


def test_tiger_venue_status_reports_ready_and_redacts_raw_order_fields(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)

    payload = TigerVenueStatus(root, checked_at="2026-07-05T16:00:00+00:00").snapshot()

    assert payload["status"] == "ready"
    assert payload["can_open_new_orders"] is True
    assert payload["can_enter_attended_paper_order"] is True
    assert payload["reconciliation"]["confirmation_status"] == "confirmed_flat"
    assert payload["order_sync"]["sync_status"] == "synced"
    assert payload["order_sync"]["filled_order_count"] == 1
    assert payload["kill_switch"]["status"] == "dry_run"
    assert payload["safety"]["dashboard_read_only"] is True
    assert payload["safety"]["network_modification_observed"] is False
    assert payload["contract"]["execution_symbol"] == "MGC2608"
    assert payload["contract"]["status"] == "ready"
    assert payload["account_sync"]["sync_status"] == "synced"
    assert payload["account_sync"]["balance_present"] is True
    assert payload["account_sync"]["segment_key"] == "C"
    assert payload["paper_order_drill"]["status"] == "pass"
    assert payload["paper_order_drill"]["scenario_count"] == 2
    assert payload["paper_order_drill"]["real_tiger_network_call_attempted"] is False
    assert payload["paper_order_readiness"]["status"] == "ready_for_attended_paper_order"
    assert payload["paper_order_readiness"]["ready_for_attended_paper_order"] is True
    assert payload["paper_order_readiness"]["can_submit_without_explicit_operator_authorization"] is False
    assert payload["paper_order_readiness"]["operator_next_action"]["status"] == "ready_for_operator_review"
    assert payload["paper_order_readiness"]["operator_next_action"]["submits_orders"] is False
    assert payload["paper_order_approval"]["status"] == "ready_for_operator_approval"
    assert payload["paper_order_approval"]["ticket_id"] == "tiger_m17_mgc2608_approval"
    assert payload["paper_order_approval"]["submit_requested"] is False
    assert payload["paper_order_approval"]["real_tiger_network_call_attempted"] is False
    assert payload["paper_order_approval"]["can_submit_without_explicit_operator_authorization"] is False
    assert payload["paper_order_approval"]["runbook_available"] is True
    assert payload["paper_order_approval"]["candidate_notional"] == 41860.0
    assert payload["realtime_validation"]["status"] == "pending_market_open"
    assert payload["realtime_validation"]["next_trading_window"]["start"] == "2026-07-05T22:00:00+00:00"
    assert payload["realtime_validation"]["market_hours_gate"]["required"] is True
    assert payload["realtime_validation"]["market_hours_gate"]["exit_code"] == 75
    assert payload["realtime_validation"]["market_hours_gate"]["operator_action"] == "rerun_after_next_trading_window"
    assert payload["realtime_validation"]["market_hours_gate"]["next_trading_window"]["start"] == "2026-07-05T22:00:00+00:00"
    assert payload["realtime_validation"]["quote_permission"]["names"] == ["aStockQuoteLv1"]
    assert payload["realtime_validation"]["safety"]["opens_trade_client"] is False
    assert payload["realtime_validation"]["safety"]["submits_orders"] is False
    assert payload["price_feed_readiness"]["status"] == "blocked"
    assert payload["price_feed_readiness"]["ready_for_price_feed"] is False
    assert payload["price_feed_readiness"]["blocker_count"] == 1
    assert payload["price_feed_readiness"]["imported_rows"] == 500
    assert payload["price_feed_readiness"]["market_hours_gate"]["exit_code"] == 75
    assert payload["price_feed_readiness"]["safety"]["artifact_only"] is True
    assert payload["price_feed_readiness"]["safety"]["opens_tiger_sdk_clients"] is False
    assert payload["price_feed_readiness"]["safety"]["submits_orders"] is False
    assert payload["price_feed_acceptance"]["status"] == "pending_market_open"
    assert payload["price_feed_acceptance"]["exit_code"] == 75
    assert payload["price_feed_acceptance"]["ready_for_price_feed"] is False
    assert payload["price_feed_acceptance"]["can_enable_broker_orders_from_this_gate"] is False
    assert payload["price_feed_acceptance"]["blocker_count"] == 1
    assert payload["price_feed_acceptance"]["operator_action"] == "rerun_after_next_trading_window"
    assert payload["price_feed_acceptance"]["next_trading_window"]["start"] == "2026-07-05T22:00:00+00:00"
    assert payload["price_feed_acceptance"]["readiness_status"] == "blocked"
    assert payload["price_feed_acceptance"]["catalog_price_feed_status"] == "blocked"
    assert payload["price_feed_acceptance"]["catalog_broker_order_status"] == "ready"
    assert payload["price_feed_acceptance"]["operator_status"] == "waiting_market_open"
    assert "Wait until 2026-07-05T22:00:00+00:00" in payload["price_feed_acceptance"]["operator_summary"]
    assert payload["price_feed_acceptance"]["next_command"].startswith("python3 -m pipelines.tiger_price_feed_acceptance")
    assert payload["price_feed_acceptance"]["safety"]["opens_trade_client"] is False
    assert payload["price_feed_acceptance"]["safety"]["submits_orders"] is False
    assert payload["price_feed_acceptance"]["safety"]["writes_market_db"] is False
    assert payload["price_feed_acceptance"]["safety"]["credential_values_exposed"] is False
    assert payload["data_source_preflight"]["source_key"] == "MGCmain_1m"
    assert payload["data_source_preflight"]["latest_provider"] == "tiger_openapi:COMEX"
    assert payload["data_source_preflight"]["latest_timestamp"] == "2026-07-03T16:59:00+00:00"
    assert payload["data_source_preflight"]["gate"]["status"] == "pending_market_open"
    assert payload["data_source_preflight"]["gate"]["allows_live"] is False
    assert payload["data_source_preflight"]["gate"]["next_trading_window"]["start"] == "2026-07-05T22:00:00+00:00"
    assert payload["data_source_preflight"]["can_enable_broker_orders_from_this_gate"] is False
    assert "submit_command" not in payload["paper_order_approval"]
    assert payload["recent_fills"] == [
        {
            "symbol": "MGC2608",
            "order_id": "8001",
            "side": "BUY",
            "type": "LMT",
            "status": "FILLED",
            "quantity": 1.0,
            "filled_quantity": 1.0,
            "average_fill_price": 4186.5,
            "filled_at": "2026-07-05T01:02:03+00:00",
            "source": "tiger_filled_orders",
        }
    ]
    assert "raw" not in payload["recent_fills"][0]
    assert "account" not in str(payload["recent_fills"])


def test_tiger_venue_status_tells_operator_to_rerun_acceptance_during_window(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_price_feed_acceptance" / "current.json",
        [
            {
                "status": "pending_market_open",
                "operator_next_action": {
                    "status": "rerun_acceptance_now",
                    "summary": "COMEX window is open; rerun Tiger price-feed acceptance now.",
                    "next_command": "python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-05 --contract MGCmain --poll-seconds 75 --json",
                },
                "steps": {},
                "blockers": [],
                "safety": {"read_only": True},
            }
        ],
    )

    payload = TigerVenueStatus(root, checked_at="2026-07-05T22:05:00+00:00").snapshot()

    assert payload["price_feed_acceptance"]["operator_status"] == "rerun_acceptance_now"
    assert payload["price_feed_acceptance"]["operator_summary"] == "COMEX window is open; rerun Tiger price-feed acceptance now."


def test_tiger_venue_status_marks_acceptance_window_expired(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_price_feed_acceptance" / "current.json",
        [
            {
                "status": "pending_market_open",
                "operator_next_action": {
                    "status": "window_expired",
                    "summary": "The recorded COMEX validation window has expired; rerun acceptance to compute the next window.",
                    "next_command": "python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-05 --contract MGCmain --poll-seconds 75 --json",
                },
                "steps": {},
                "blockers": [],
                "safety": {"read_only": True},
            }
        ],
    )

    payload = TigerVenueStatus(root, checked_at="2026-07-06T21:05:00+00:00").snapshot()

    assert payload["price_feed_acceptance"]["operator_status"] == "window_expired"
    assert "expired" in payload["price_feed_acceptance"]["operator_summary"]


def test_tiger_venue_status_falls_back_for_legacy_acceptance_without_operator_next_action(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    legacy = load_json(root / "tiger_price_feed_acceptance" / "current.json")[-1]
    legacy.pop("operator_next_action", None)
    write_json(root / "tiger_price_feed_acceptance" / "current.json", [legacy])

    payload = TigerVenueStatus(root, checked_at="2026-07-05T22:05:00+00:00").snapshot()

    assert payload["price_feed_acceptance"]["operator_status"] == "rerun_acceptance_now"
    assert payload["price_feed_acceptance"]["next_command"].startswith("python3 -m pipelines.tiger_price_feed_acceptance")


def test_tiger_venue_status_blocks_on_reconciliation_drift(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_reconciliation" / "current.json",
        [
            {
                "confirmation_status": "confirmed_drift",
                "system_state": "BLOCKED_RECONCILIATION_DRIFT",
                "reason_code": "reconciliation_drift",
                "drift_count": 1,
                "can_open_new_orders": False,
                "exchange_positions": [{"symbol": "MGC2608"}],
                "exchange_open_orders": [],
            }
        ],
    )

    payload = TigerVenueStatus(root).snapshot()

    assert payload["status"] == "blocked"
    assert payload["can_open_new_orders"] is False
    assert payload["can_enter_attended_paper_order"] is False
    assert payload["headline"] == "reconciliation_drift"


def test_tiger_venue_status_degrades_when_order_sync_is_missing_but_reconciliation_flat(tmp_path: Path):
    root = tmp_path / "outputs"
    write_json(
        root / "tiger_reconciliation" / "current.json",
        [{"confirmation_status": "confirmed_flat", "can_open_new_orders": True}],
    )
    write_json(
        root / "tiger_kill_switch" / "current.json",
        [{"status": "dry_run", "network_order_created": False, "network_cancel_created": False}],
    )

    payload = TigerVenueStatus(root).snapshot()

    assert payload["status"] == "degraded"
    assert payload["can_open_new_orders"] is True
    assert payload["can_enter_attended_paper_order"] is False
    assert payload["order_sync"]["available"] is False


def test_tiger_venue_status_degrades_when_account_sync_present_but_failed(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_account_sync" / "current.json",
        [
            {
                "sync_status": "cannot_sync",
                "error": "RuntimeError: permission denied",
                "account_observation": {"account_observed": False, "balance_present": False, "accounting_observed": False},
                "exchange_balance": {"balance_present": False},
                "exchange_accounting": {},
            }
        ],
    )

    payload = TigerVenueStatus(root).snapshot()

    assert payload["status"] == "degraded"
    assert payload["account_sync"]["sync_status"] == "cannot_sync"
    assert "permission denied" in payload["headline"]


def test_tiger_venue_status_degrades_when_paper_order_drill_present_but_failed(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_paper_order_drill" / "current.json",
        [
            {
                "status": "fail",
                "run_id": "drilltest",
                "mode": "local_fake_tradeclient",
                "real_tiger_network_call_attempted": False,
                "scenarios": [{"name": "simulated_green_order", "status": "fail"}],
            }
        ],
    )

    payload = TigerVenueStatus(root).snapshot()

    assert payload["status"] == "degraded"
    assert payload["paper_order_drill"]["status"] == "fail"
    assert "paper order drill" in payload["headline"]


def test_tiger_venue_status_degrades_when_paper_order_readiness_present_but_blocked(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_paper_order_readiness" / "current.json",
        [
            {
                "status": "blocked",
                "ready_for_attended_paper_order": False,
                "can_submit_without_explicit_operator_authorization": False,
                "real_tiger_network_call_attempted": False,
                "checks": [{"name": "paper_order_drill", "status": "fail"}],
                "blockers": [{"name": "paper_order_drill", "status": "fail"}],
            }
        ],
    )

    payload = TigerVenueStatus(root).snapshot()

    assert payload["status"] == "degraded"
    assert payload["paper_order_readiness"]["status"] == "blocked"
    assert payload["paper_order_readiness"]["blocker_count"] == 1
    assert payload["can_open_new_orders"] is True
    assert payload["can_enter_attended_paper_order"] is False
    assert payload["paper_order_readiness"]["operator_next_action"]["status"] == "resolve_blockers"
    assert "attended paper order readiness" in payload["headline"]


def test_tiger_venue_status_summarizes_stale_paper_order_refresh_path(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_paper_order_readiness" / "current.json",
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
                "next_commands": [
                    "python3 -m pipelines.tiger_openapi_order_sync --date 2026-07-06 --json",
                    "python3 -m pipelines.tiger_openapi_paper_order_readiness --date 2026-07-06 --json",
                ],
                "checked_at": "2026-07-06T09:30:09+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_paper_order_readiness" / "refresh_runbook_current.json",
        [
            {
                "status": "ready_for_operator_refresh",
                "runbook_id": "paper_order_refresh_runbook_test",
                "run_date": "2026-07-06",
                "readiness_status": "blocked",
                "readiness_checked_at": "2026-07-06T09:30:09+00:00",
                "blocker_count": 1,
                "stale_evidence_count": 1,
                "blocker_names": ["order_sync"],
                "command_sequence": [
                    {"name": "refresh_order_sync", "opens_trade_client": True, "submits_orders": False, "writes_runtime_config": False},
                    {"name": "refresh_readiness", "opens_trade_client": False, "submits_orders": False, "writes_runtime_config": False},
                ],
                "generation_safety": {
                    "opens_trade_client": False,
                    "submits_orders": False,
                    "writes_runtime_config": False,
                },
                "command_sequence_safety": {
                    "opens_trade_client_read_only": True,
                    "submits_orders": False,
                    "writes_runtime_config": False,
                },
                "checked_at": "2026-07-06T09:31:00+00:00",
            }
        ],
    )

    payload = TigerVenueStatus(root).snapshot()
    action = payload["paper_order_readiness"]["operator_next_action"]

    assert payload["paper_order_readiness"]["stale_evidence_count"] == 1
    assert payload["paper_order_readiness"]["blocker_names"] == ["order_sync"]
    assert action["status"] == "refresh_stale_evidence"
    assert action["stale_evidence_count"] == 1
    assert action["blocker_names"] == ["order_sync"]
    assert action["refresh_step_count"] == 2
    assert action["opens_trade_client_read_only"] is True
    assert action["submits_orders"] is False
    assert action["writes_runtime_config"] is False
    assert action["refresh_steps"][0]["name"] == "refresh_order_sync"
    assert action["refresh_steps"][0]["opens_trade_client_mode"] == "read_only"
    assert "tiger_openapi_order_sync" not in str(action["refresh_steps"])
    assert payload["paper_order_refresh_runbook"]["status"] == "ready_for_operator_refresh"
    assert payload["paper_order_refresh_runbook"]["matches_current_readiness"] is True
    assert payload["paper_order_refresh_runbook"]["source_readiness_checked_at"] == "2026-07-06T09:30:09+00:00"
    assert payload["paper_order_refresh_runbook"]["current_readiness_checked_at"] == "2026-07-06T09:30:09+00:00"
    assert payload["paper_order_refresh_runbook"]["command_count"] == 2
    assert payload["paper_order_refresh_runbook"]["opens_trade_client_read_only"] is True
    assert payload["paper_order_refresh_runbook"]["generation_opens_trade_client"] is False
    assert payload["paper_order_refresh_runbook"]["submits_orders"] is False
    assert payload["paper_order_refresh_runbook"]["writes_runtime_config"] is False


def test_tiger_venue_status_marks_paper_refresh_runbook_stale_when_readiness_changes(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_paper_order_readiness" / "current.json",
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
        root / "tiger_paper_order_readiness" / "refresh_runbook_current.json",
        [
            {
                "status": "ready_for_operator_refresh",
                "runbook_id": "paper_order_refresh_runbook_old",
                "run_date": "2026-07-06",
                "readiness_status": "blocked",
                "readiness_checked_at": "2026-07-06T08:00:00+00:00",
                "blocker_count": 1,
                "stale_evidence_count": 1,
                "blocker_names": ["order_sync"],
                "command_sequence": [{"name": "refresh_order_sync", "opens_trade_client": True}],
                "generation_safety": {"opens_trade_client": False, "submits_orders": False, "writes_runtime_config": False},
                "command_sequence_safety": {"opens_trade_client_read_only": True, "submits_orders": False, "writes_runtime_config": False},
                "checked_at": "2026-07-06T08:01:00+00:00",
            }
        ],
    )

    payload = TigerVenueStatus(root).snapshot()

    assert payload["paper_order_refresh_runbook"]["matches_current_readiness"] is False
    assert payload["paper_order_refresh_runbook"]["source_readiness_checked_at"] == "2026-07-06T08:00:00+00:00"
    assert payload["paper_order_refresh_runbook"]["current_readiness_checked_at"] == "2026-07-06T09:30:09+00:00"


def test_tiger_venue_status_degrades_when_paper_order_approval_present_but_blocked(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_paper_order_approval" / "current.json",
        [
            {
                "status": "blocked",
                "submit_requested": False,
                "real_tiger_network_call_attempted": False,
                "blockers": [{"name": "tiger_props_file", "status": "fail"}],
            }
        ],
    )

    payload = TigerVenueStatus(root).snapshot()

    assert payload["status"] == "degraded"
    assert payload["paper_order_approval"]["status"] == "blocked"
    assert payload["paper_order_approval"]["blocker_count"] == 1
    assert "approval runbook" in payload["headline"]


def test_tiger_venue_status_manual_review_when_approval_indicates_network_activity(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_paper_order_approval" / "current.json",
        [
            {
                "status": "ready_for_operator_approval",
                "submit_requested": True,
                "real_tiger_network_call_attempted": True,
                "blockers": [],
            }
        ],
    )

    payload = TigerVenueStatus(root).snapshot()

    assert payload["status"] == "manual_review"
    assert payload["paper_order_approval"]["submit_requested"] is True
    assert payload["paper_order_approval"]["real_tiger_network_call_attempted"] is True
    assert "submit/network activity" in payload["headline"]


def test_tiger_venue_status_degrades_when_realtime_validation_fails(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_realtime_validation" / "current.json",
        [
            {
                "schema_version": "tiger-realtime-validation-v1",
                "status": "fail",
                "message": "Tiger futures latest bar is stale while COMEX session is trading.",
                "contract": "MGCmain",
                "output_symbol": "MGCmain",
                "checked_at": "2026-07-05T22:10:00+00:00",
                "latest_bar_age_seconds": 600.0,
                "bar_advanced": False,
                "fresh": False,
                "polls": {
                    "first": {"timestamp": "2026-07-05T22:00:00+00:00"},
                    "second": {"timestamp": "2026-07-05T22:00:00+00:00"},
                },
                "preflight": {"checks": {"quote_permission": {"names": ["aStockQuoteLv1"], "has_futures_realtime": False}}},
                "safety": {
                    "read_only": True,
                    "writes_market_db": False,
                    "opens_trade_client": False,
                    "submits_orders": False,
                },
            }
        ],
    )

    payload = TigerVenueStatus(root).snapshot()

    assert payload["status"] == "degraded"
    assert payload["realtime_validation"]["status"] == "fail"
    assert payload["realtime_validation"]["latest_bar_age_seconds"] == 600.0
    assert payload["realtime_validation"]["polls"]["second_timestamp"] == "2026-07-05T22:00:00+00:00"
    assert "latest bar is stale" in payload["headline"]


def test_tiger_venue_status_blocks_when_contract_rollover_required(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    write_json(
        root / "tiger_contracts" / "current.json",
        [
            {
                "resolution": {
                    "requested_symbol": "MGCmain",
                    "execution_symbol": "MGC2608",
                    "status": "rollover_required",
                    "ready": False,
                    "days_to_contract_month": 7,
                    "block_reason": "Tiger contract MGC2608 is inside rollover window",
                },
                "checked_at": "2026-07-25T00:00:00+00:00",
            }
        ],
    )

    payload = TigerVenueStatus(root).snapshot()

    assert payload["status"] == "blocked"
    assert payload["contract"]["status"] == "rollover_required"
    assert "inside rollover window" in payload["headline"]
