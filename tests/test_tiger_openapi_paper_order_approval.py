from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.journal_store import load_json, write_json
from services.tiger_openapi_paper_order_approval import TigerOpenApiPaperOrderApproval
from services.tiger_openapi_paper_order_canary import ACKNOWLEDGEMENT


RUN_DATE = "2026-07-05"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _utc_day_window_ms(run_date: str) -> tuple[int, int]:
    start = datetime.strptime(run_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _ready_artifacts(root: Path) -> None:
    start_ms, end_ms = _utc_day_window_ms(RUN_DATE)
    write_json(
        root / "tiger_paper_order_readiness" / "current.json",
        [
            {
                "run_date": RUN_DATE,
                "status": "ready_for_attended_paper_order",
                "ready_for_attended_paper_order": True,
                "can_submit_without_explicit_operator_authorization": False,
            }
        ],
    )
    write_json(
        root / "tiger_reconciliation" / "current.json",
        [
            {
                "run_date": RUN_DATE,
                "checked_at": _now(),
                "provider": "tiger_openapi",
                "mode": "paper",
                "confirmation_status": "confirmed_flat",
                "can_open_new_orders": True,
                "exchange_positions": [],
                "exchange_open_orders": [],
            }
        ],
    )
    write_json(
        root / "tiger_account_sync" / "current.json",
        [
            {
                "run_date": RUN_DATE,
                "sync_status": "synced",
                "error": "",
                "account_observation": {
                    "account_observed": True,
                    "balance_present": True,
                    "accounting_observed": True,
                },
                "exchange_balance": {"asset": "USD", "balance": 25000.0, "available": 24000.0, "balance_present": True},
                "exchange_accounting": {
                    "net_realized_pnl_estimate": 0.0,
                    "unrealized_pnl_estimate": 0.0,
                    "utc_trading_day": {"run_date": RUN_DATE, "start_time_ms": start_ms, "end_time_ms": end_ms},
                },
            }
        ],
    )


def _props(tmp_path: Path, mode: int = 0o600) -> Path:
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(mode)
    return props


def _kwargs(props: Path, **overrides) -> dict:
    data = {
        "ticket_id": "tiger_m17_approval_test",
        "asset": "MGC2608",
        "side": "buy",
        "quantity": 1,
        "entry_price": 4186.0,
        "stop_loss": 4170.0,
        "take_profit": 4200.0,
        "operator": "codex-test",
        "props_path": str(props),
        "use_attended_canary_risk_limits": True,
    }
    data.update(overrides)
    return data


def test_tiger_paper_order_approval_blocks_without_ready_canary(tmp_path: Path):
    root = tmp_path / "outputs"
    props = _props(tmp_path)

    result = TigerOpenApiPaperOrderApproval(root).build(RUN_DATE, **_kwargs(props))

    assert result["status"] == "blocked"
    assert result["real_tiger_network_call_attempted"] is False
    blockers = {item["name"]: item for item in result["blockers"]}
    assert "canary_check" in blockers
    assert load_json(root / "tiger_paper_order_approval" / "current.json")[-1]["status"] == "blocked"


def test_tiger_paper_order_approval_blocks_when_props_file_is_not_owner_only(tmp_path: Path):
    root = tmp_path / "outputs"
    _ready_artifacts(root)
    props = _props(tmp_path, mode=0o644)

    result = TigerOpenApiPaperOrderApproval(root).build(RUN_DATE, **_kwargs(props))

    assert result["status"] == "blocked"
    blockers = {item["name"]: item for item in result["blockers"]}
    assert "tiger_props_file" in blockers
    assert blockers["tiger_props_file"]["evidence"]["props_path_owner_only"] is False


def test_tiger_paper_order_approval_builds_ready_runbook_without_network_call(tmp_path: Path):
    root = tmp_path / "outputs"
    _ready_artifacts(root)
    props = _props(tmp_path)

    result = TigerOpenApiPaperOrderApproval(root).build(RUN_DATE, **_kwargs(props))

    assert result["status"] == "ready_for_operator_approval"
    assert result["submit_requested"] is False
    assert result["real_tiger_network_call_attempted"] is False
    assert result["can_submit_without_explicit_operator_authorization"] is False
    assert result["canary_check"]["status"] == "ready_for_operator_authorization"
    assert result["canary_check"]["candidate_notional"] == 41860.0
    assert result["submit_command"].startswith(f"TIGER_OPENAPI_CONFIG_PATH={props} python3 -m pipelines.tiger_openapi_paper_order_canary")
    assert "--submit-tiger-paper-canary" in result["submit_command"]
    assert "--confirm-tiger-paper-canary" in result["submit_command"]
    assert "--use-attended-canary-risk-limits" in result["submit_command"]
    assert ACKNOWLEDGEMENT in result["submit_command"]
    assert "tiger_openapi_reconciliation" in result["pre_submit_refresh_commands"][1]
    assert result["manual_kill_path"]["network_kill_switch_enabled_by_default"] is False
    assert (root / "tiger_paper_order_approval" / f"{RUN_DATE}.md").exists()
