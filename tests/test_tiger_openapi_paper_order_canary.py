from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import PaperOrder
from services.journal_store import load_json, write_json
from services.tiger_openapi_paper_order_canary import ACKNOWLEDGEMENT, TigerOpenApiPaperOrderCanary


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


def _check_kwargs(**overrides) -> dict:
    data = {
        "ticket_id": "tiger_canary_test",
        "asset": "MGC2608",
        "side": "buy",
        "quantity": 1,
        "entry_price": 0.5,
        "stop_loss": 0.4,
        "take_profit": 0.6,
        "operator": "codex-test",
    }
    data.update(overrides)
    return data


def test_tiger_paper_order_canary_check_blocks_without_m14_readiness(tmp_path: Path):
    root = tmp_path / "outputs"
    canary = TigerOpenApiPaperOrderCanary(root)

    result = canary.check(RUN_DATE, **_check_kwargs())

    assert result["status"] == "blocked"
    assert result["real_tiger_network_call_attempted"] is False
    blockers = {item["name"]: item for item in result["blockers"]}
    assert "paper_order_readiness" in blockers
    assert load_json(root / "tiger_paper_order_canary" / "current.json")[-1]["status"] == "blocked"


def test_tiger_paper_order_canary_check_blocks_real_mgc_ticket_on_current_money_limits(tmp_path: Path):
    root = tmp_path / "outputs"
    _ready_artifacts(root)
    canary = TigerOpenApiPaperOrderCanary(root)

    result = canary.check(
        RUN_DATE,
        **_check_kwargs(entry_price=4186.0, stop_loss=4170.0, take_profit=4200.0),
    )

    assert result["status"] == "blocked"
    checks = {item["name"]: item for item in result["checks"]}
    money = checks["ticket_specific_money_guardrail"]["evidence"]
    assert money["status"] == "BLOCKED_SINGLE_ORDER_NOTIONAL_LIMIT"
    assert money["candidate"]["notional_multiplier"] == 10
    assert money["candidate"]["notional"] == 41860.0


def test_tiger_paper_order_canary_attended_risk_package_allows_one_mgc_check(tmp_path: Path):
    root = tmp_path / "outputs"
    _ready_artifacts(root)
    canary = TigerOpenApiPaperOrderCanary(root)

    result = canary.check(
        RUN_DATE,
        **_check_kwargs(entry_price=4186.0, stop_loss=4170.0, take_profit=4200.0),
        use_attended_canary_risk_limits=True,
    )

    assert result["status"] == "ready_for_operator_authorization"
    assert result["use_attended_canary_risk_limits"] is True
    checks = {item["name"]: item for item in result["checks"]}
    risk_package = checks["attended_canary_risk_package"]["evidence"]
    assert risk_package["requested"] is True
    assert risk_package["enabled"] is True
    assert risk_package["candidate_stop_loss"] == 160.0
    money = checks["ticket_specific_money_guardrail"]["evidence"]
    assert money["status"] == "READY"
    assert money["limits"]["single_order_max_notional"] == 45000.0
    assert money["candidate"]["notional"] == 41860.0


def test_tiger_paper_order_canary_submit_requires_explicit_authorization_before_adapter(tmp_path: Path):
    root = tmp_path / "outputs"
    _ready_artifacts(root)
    calls = []

    def factory(*args, **kwargs):  # noqa: ANN002, ANN003 - captures unexpected adapter creation.
        calls.append((args, kwargs))
        raise AssertionError("adapter must not be created without operator authorization")

    canary = TigerOpenApiPaperOrderCanary(root, adapter_factory=factory)

    result = canary.submit(RUN_DATE, **_check_kwargs(), confirm=False, acknowledgement="")

    assert result["status"] == "blocked_operator_authorization_missing"
    assert result["real_tiger_network_call_attempted"] is False
    assert calls == []
    blockers = {item["name"]: item for item in result["blockers"]}
    assert "operator_authorization" in blockers


def test_tiger_paper_order_canary_submit_calls_adapter_only_after_ack(tmp_path: Path):
    root = tmp_path / "outputs"
    _ready_artifacts(root)
    calls = []

    class _FakeAdapter:
        def __init__(self, output_root, config, *, trade_client=None, sdk=None):  # noqa: ANN001
            calls.append({"output_root": output_root, "config": config, "trade_client": trade_client, "sdk": sdk})
            self.output_root = output_root

        def submit_order(self, request):  # noqa: ANN001
            write_json(
                self.output_root / "tiger_order_requests" / f"{request.run_date}.json",
                [{"request": {"network_order_created": True}}],
            )
            return PaperOrder(
                order_id="fake_tiger_canary_order",
                ticket_id=request.ticket["ticket_id"],
                status="submitted_to_tiger_paper",
                requested_price=request.latest_price,
                fill_price=None,
                quantity=request.actual_size,
                filled_at="",
                rejection_reason="submitted to fake Tiger paper adapter",
            )

    canary = TigerOpenApiPaperOrderCanary(
        root,
        adapter_factory=_FakeAdapter,
        trade_client=object(),
        sdk=object(),
    )

    result = canary.submit(
        RUN_DATE,
        **_check_kwargs(),
        confirm=True,
        acknowledgement=ACKNOWLEDGEMENT,
    )

    assert result["status"] == "submitted_to_tiger_paper"
    assert result["real_tiger_network_call_attempted"] is False
    assert result["result"]["network_order_created"] is True
    assert len(calls) == 1
    assert calls[0]["config"]["dry_run"] is False
    assert calls[0]["config"]["network_order_submission"] == "paper_tradeclient"
    assert calls[0]["config"]["confirm_tiger_paper_orders"] is True


def test_tiger_paper_order_canary_submit_uses_attended_risk_package_only_when_requested(tmp_path: Path):
    root = tmp_path / "outputs"
    _ready_artifacts(root)
    calls = []

    class _FakeAdapter:
        def __init__(self, output_root, config, *, trade_client=None, sdk=None):  # noqa: ANN001
            calls.append({"output_root": output_root, "config": config, "trade_client": trade_client, "sdk": sdk})
            self.output_root = output_root

        def submit_order(self, request):  # noqa: ANN001
            write_json(
                self.output_root / "tiger_order_requests" / f"{request.run_date}.json",
                [{"request": {"network_order_created": True}}],
            )
            return PaperOrder(
                order_id="fake_tiger_canary_mgc_order",
                ticket_id=request.ticket["ticket_id"],
                status="submitted_to_tiger_paper",
                requested_price=request.latest_price,
                fill_price=None,
                quantity=request.actual_size,
                filled_at="",
                rejection_reason="submitted to fake Tiger paper adapter",
            )

    canary = TigerOpenApiPaperOrderCanary(
        root,
        adapter_factory=_FakeAdapter,
        trade_client=object(),
        sdk=object(),
    )

    blocked = canary.submit(
        RUN_DATE,
        **_check_kwargs(entry_price=4186.0, stop_loss=4170.0, take_profit=4200.0),
        confirm=True,
        acknowledgement=ACKNOWLEDGEMENT,
    )
    assert blocked["status"] == "blocked"
    assert calls == []

    result = canary.submit(
        RUN_DATE,
        **_check_kwargs(entry_price=4186.0, stop_loss=4170.0, take_profit=4200.0),
        confirm=True,
        acknowledgement=ACKNOWLEDGEMENT,
        use_attended_canary_risk_limits=True,
    )

    assert result["status"] == "submitted_to_tiger_paper"
    assert result["result"]["network_order_created"] is True
    assert len(calls) == 1
    assert calls[0]["config"]["attended_canary_risk_package_requested"] is True
    assert calls[0]["config"]["live_money_guardrails"]["single_order_max_notional"] == 45000.0
