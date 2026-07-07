from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from services.journal_store import load_json
from services.tiger_openapi_account_sync import TigerOpenApiAccountSync


class _FakeAccountClient:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def get_prime_assets(self, **kwargs):
        self.calls.append(("get_prime_assets", kwargs))
        if self.error:
            raise self.error
        return self.payload


def _portfolio() -> SimpleNamespace:
    return SimpleNamespace(
        segments={
            "S": SimpleNamespace(currency="USD", net_liquidation=123.0, cash_available_for_trade=100.0, realized_pl=1.0),
            "C": SimpleNamespace(
                currency="USD",
                net_liquidation=25000.5,
                cash_available_for_trade=22000.25,
                realized_pl=-12.75,
                unrealized_pl=3.5,
                buying_power=100000.0,
            ),
        }
    )


def test_tiger_account_sync_normalizes_prime_assets_and_writes_artifacts(tmp_path: Path):
    root = tmp_path / "outputs"
    client = _FakeAccountClient(_portfolio())
    sync = TigerOpenApiAccountSync(
        root,
        broker_config={"provider": "tiger_openapi", "base_currency": "USD", "account_segment_priority": ["C", "F", "S"]},
        trade_client=client,
    )

    report = sync.run("2026-06-09")

    assert client.calls == [("get_prime_assets", {"base_currency": "USD", "consolidated": True})]
    assert report["sync_status"] == "synced"
    assert report["account_observation"]["account_observed"] is True
    assert report["account_observation"]["balance_present"] is True
    assert report["account_observation"]["accounting_observed"] is True
    assert report["account_observation"]["segment_key"] == "C"
    assert report["exchange_balance"]["asset"] == "USD"
    assert report["exchange_balance"]["balance"] == 25000.5
    assert report["exchange_balance"]["available"] == 22000.25
    assert report["exchange_accounting"]["net_realized_pnl_estimate"] == -12.75
    assert report["exchange_accounting"]["unrealized_pnl_estimate"] == 3.5
    assert report["exchange_accounting"]["utc_trading_day"] == {
        "run_date": "2026-06-09",
        "start_time_ms": 1780963200000,
        "end_time_ms": 1781049599999,
    }
    assert report["selected_segment"]["segment_key"] == "C"

    current = load_json(root / "tiger_account_sync" / "current.json")[-1]
    dated = load_json(root / "tiger_account_sync" / "2026-06-09.json")[-1]
    assert current["exchange_balance"]["balance"] == 25000.5
    assert dated["exchange_accounting"]["net_realized_pnl_estimate"] == -12.75


def test_tiger_account_sync_records_cannot_sync_without_secret_material(tmp_path: Path):
    root = tmp_path / "outputs"
    client = _FakeAccountClient(error=RuntimeError("permission denied"))
    sync = TigerOpenApiAccountSync(root, broker_config={"provider": "tiger_openapi"}, trade_client=client)

    report = sync.run("2026-06-09")

    assert report["sync_status"] == "cannot_sync"
    assert "permission denied" in report["error"]
    assert report["account_observation"]["account_observed"] is False
    assert report["exchange_balance"]["balance_present"] is False
    assert "secret" not in str(report).lower()


def test_tiger_account_sync_merges_balance_and_accounting_into_reconciliation(tmp_path: Path):
    root = tmp_path / "outputs"
    sync = TigerOpenApiAccountSync(root, broker_config={"provider": "tiger_openapi"}, trade_client=_FakeAccountClient(_portfolio()))
    account_report = sync.run("2026-06-09")
    reconciliation = {
        "run_date": "2026-06-09",
        "checked_at": account_report["checked_at"],
        "confirmation_status": "confirmed_flat",
        "can_open_new_orders": True,
        "exchange_positions": [],
        "account_observation": {"positions_observed": True, "open_orders_observed": True},
    }

    merged = sync.merge_into_reconciliation(reconciliation, account_report)

    assert merged["account_observation"]["account_observed"] is True
    assert merged["account_observation"]["balance_present"] is True
    assert merged["account_observation"]["accounting_observed"] is True
    assert merged["account_observation"]["accounting_source"] == "tiger_openapi.get_prime_assets"
    assert merged["exchange_balance"]["balance"] == 25000.5
    assert merged["exchange_accounting"]["net_realized_pnl_estimate"] == -12.75
    assert merged["exchange_positions"] == []
    assert "error" not in merged
