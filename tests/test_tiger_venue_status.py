from __future__ import annotations

from pathlib import Path

from services.journal_store import write_json
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


def test_tiger_venue_status_reports_ready_and_redacts_raw_order_fields(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)

    payload = TigerVenueStatus(root).snapshot()

    assert payload["status"] == "ready"
    assert payload["can_open_new_orders"] is True
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
    assert "attended paper order readiness" in payload["headline"]


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
