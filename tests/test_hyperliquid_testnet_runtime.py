from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from services.hyperliquid_testnet_runtime import (
    HyperliquidTestnetRuntimeConfig,
    build_park_account_reader,
    build_testnet_start_handler,
)


ACCOUNT = "0x" + "11" * 20
RELEASE = "a" * 40


def _raw_account() -> dict[str, object]:
    return {
        "schema_version": "hyperliquid-testnet-account-facts-v1",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "account_fingerprint": "sha256:" + "b" * 64,
        "equity": 995.0,
        "positions": [],
        "open_orders": [],
        "fills": [],
        "fresh": True,
        "coherent": True,
        "source_cursor": "sha256:" + "c" * 64,
        "observed_at": "2026-08-31T00:00:00+00:00",
        "capabilities": {
            "account_read": True,
            "positions_read": True,
            "open_orders_read": True,
            "fills_read": True,
            "fees_read": True,
            "protection": False,
        },
    }


class _Reader:
    def read(self, instrument_id: str | None = None) -> dict[str, object]:
        value = _raw_account()
        value["selected_instrument_id"] = instrument_id
        return value


def test_park_account_reader_projects_public_facts_and_opt_in_protection() -> None:
    config = HyperliquidTestnetRuntimeConfig(
        account_address=ACCOUNT,
        instrument_id="BTC-USD-PERP",
        protected_profile_enabled=True,
        secret_file=Path("/opaque/testnet-secret"),
        runtime_id="runtime-testnet",
        release_sha=RELEASE,
        approved_by="park",
    )

    reader = build_park_account_reader(config, public_reader=_Reader())
    result = reader(Path("/tmp/unused"), "cycle-1")

    assert result["reconciliation_healthy"] is True
    assert result["open_positions"] == 0
    assert result["open_or_accepted_orders"] == 0
    assert result["unresolved_runtime"] is False
    assert result["pending_terminal_actions"] is False
    assert result["capabilities"]["protection"] is True
    assert result["snapshot"]["positions"] == []
    assert result["snapshot"]["orders"] == []


def test_park_account_reader_keeps_default_protection_disabled() -> None:
    config = HyperliquidTestnetRuntimeConfig(
        account_address=ACCOUNT,
        protected_profile_enabled=False,
        secret_file=None,
        runtime_id="runtime-testnet",
        release_sha=RELEASE,
    )

    reader = build_park_account_reader(config, public_reader=_Reader())
    result = reader(Path("/tmp/unused"), "cycle-1")

    assert result["capabilities"]["protection"] is False


def test_start_handler_routes_confirmed_testnet_plan_to_guarded_control_plane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = HyperliquidTestnetRuntimeConfig(
        account_address=ACCOUNT,
        instrument_id="BTC-USD-PERP",
        protected_profile_enabled=True,
        secret_file=tmp_path / "opaque-secret",
        runtime_id="runtime-testnet",
        release_sha=RELEASE,
        approved_by="park",
    )
    calls: list[object] = []

    class _Control:
        def start_testnet_dca(self, plan, **kwargs):
            calls.append((plan, kwargs))
            return {"runtime": {"actual_state": "running"}, "lifecycle": {"status": "waiting_entry"}}

    monkeypatch.setattr(
        "services.hyperliquid_testnet_runtime.StrategyControlPlane",
        lambda *_args, **_kwargs: _Control(),
    )
    monkeypatch.setattr(
        "services.hyperliquid_testnet_runtime.build_broker_execution_port",
        lambda context: SimpleNamespace(
            name="standard_broker_external_testnet_protected",
            context=context,
            preflight=lambda **_kwargs: {
                "ready": True,
                "environment": "testnet",
                "real_money_eligible": False,
            },
            close=lambda: None,
        ),
    )

    output_root = tmp_path / "outputs"
    plan = {
        "plan_digest": "sha256:" + "d" * 64,
        "strategy_session_id": "session-1",
        "strategy_revision_id": "revision-1",
        "normalized_input": {
            "strategy_type": "dca",
            "direction": "long",
            "upper_price_boundary": 110.0,
            "lower_price_boundary": 90.0,
            "maximum_leverage": 2.0,
            "stop_price": 80.0,
            "take_profit_price": 120.0,
            "order_count": 1,
        },
        "market": {"price": 100.0, "source": "hyperliquid.external_testnet", "observed_at": "2026-08-31T00:00:00+00:00", "instrument_id": "BTC-USD-PERP", "symbol": "BTC"},
        "risk": {"maximum_notional": 100.0, "effective_leverage": 1.0, "theoretical_max_loss": 20.0, "order_count": 1, "per_order_quantity": 1.0, "per_order_notional": 100.0, "account_equity": 1000.0},
    }
    from services.journal_store import write_json

    write_json(output_root / "park_strategy" / "plans.jsonl", [{"event": "plan_proposed", **plan}])
    handler = build_testnet_start_handler(
        output_root,
        config=config,
        park_user_id="park",
        chat_id="chat",
        market_reader=lambda _instrument: {**plan["market"], "trusted": True, "fresh": True},
    )

    result = handler({
        "proposal": {"plan_digest": plan["plan_digest"], "proposal_id": "proposal-1"},
        "decision": {
            "execution_authorized": True,
            "execution_environment": "testnet",
            "proposal_id": "proposal-1",
            "receipt_digest": "sha256:" + "e" * 64,
        },
        "plan_digest": plan["plan_digest"],
        "environment": "testnet",
    })

    assert result["status"] == "testnet_started"
    assert calls and calls[0][0]["strategy_type"] == "dca"
    assert calls[0][1]["confirmation"]["execution_authorized"] is True
    assert calls[0][1]["market"]["execution_ready"] is True
