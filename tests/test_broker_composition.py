import json
from pathlib import Path

import pytest

from services.broker_composition import (
    BrokerBuildContext,
    BrokerPlugin,
    BrokerPluginKey,
    BrokerPluginRegistry,
    build_broker_execution_port,
    build_broker_reconciliation_port,
)
from services.broker_port import BrokerOrderRequest


class _FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _context(
    tmp_path: Path,
    *,
    mode: str = "live",
    provider: str,
    environment: str,
    live_trading_enabled: bool = False,
    broker_config: dict | None = None,
    auxiliary_config: dict | None = None,
    opener=None,
) -> BrokerBuildContext:
    return BrokerBuildContext(
        output_root=tmp_path / "outputs",
        execution_mode=mode,
        live_trading_enabled=live_trading_enabled,
        broker_config={
            "provider": provider,
            "environment": environment,
            "dry_run": True,
            **(broker_config or {}),
        },
        auxiliary_config=auxiliary_config or {},
        opener=opener,
    )


def test_registry_prefers_exact_key_then_explicit_fallback(tmp_path: Path):
    registry = BrokerPluginRegistry()
    exact = BrokerPlugin(
        BrokerPluginKey("live", "binance_usdm", "demo"),
        execution_factory=lambda context: "exact",
    )
    fallback = BrokerPlugin(
        BrokerPluginKey("live", "binance_usdm", "*"),
        execution_factory=lambda context: "fallback",
    )
    registry.register(fallback)
    registry.register(exact)

    assert registry.resolve(_context(tmp_path, provider="binance_usdm", environment="demo")) is exact
    assert registry.resolve(_context(tmp_path, provider="binance_usdm", environment="live")) is fallback

    with pytest.raises(ValueError, match="duplicate broker plugin"):
        registry.register(exact)


def test_registry_rejects_nonconforming_execution_port(tmp_path: Path):
    registry = BrokerPluginRegistry()
    registry.register(
        BrokerPlugin(
            BrokerPluginKey("live", "broken", "*"),
            execution_factory=lambda context: object(),
        )
    )

    with pytest.raises(TypeError, match="did not build a BrokerExecutionPort"):
        registry.build_execution(
            _context(tmp_path, provider="broken", environment="live")
        )


@pytest.mark.parametrize(
    ("context_kwargs", "expected_name"),
    [
        ({"mode": "paper", "provider": "binance_usdm", "environment": "live"}, "paper"),
        ({"provider": "tiger_openapi", "environment": "paper"}, "tiger_openapi_paper"),
        ({"provider": "binance_usdm", "environment": "demo"}, "binance_demo"),
        ({"provider": "binance_usdm", "environment": "testnet"}, "binance_usdm_testnet"),
        ({"provider": "binance_usdm", "environment": "live"}, "live"),
    ],
)
def test_default_registry_builds_expected_adapter(
    tmp_path: Path,
    context_kwargs: dict,
    expected_name: str,
):
    adapter = build_broker_execution_port(_context(tmp_path, **context_kwargs))

    assert adapter.name == expected_name


def test_demo_and_testnet_are_distinct_plugins_with_matched_reconciliation_endpoint(tmp_path: Path):
    for environment in ("demo", "testnet"):
        context = _context(
            tmp_path,
            provider="binance_usdm",
            environment=environment,
            broker_config={"base_url": "https://demo-fapi.binance.com"},
        )
        execution = build_broker_execution_port(context)
        reconciliation = build_broker_reconciliation_port(context)

        assert execution.broker_config["environment"] == environment
        assert reconciliation.broker_config["environment"] == environment
        assert reconciliation.broker_config["base_url"] == execution.broker_config["base_url"]

    demo = build_broker_execution_port(
        _context(tmp_path, provider="binance_usdm", environment="demo")
    )
    testnet = build_broker_execution_port(
        _context(tmp_path, provider="binance_usdm", environment="testnet")
    )
    assert demo.name != testnet.name


def test_unknown_provider_fallback_is_unarmed_even_when_caller_requests_live(tmp_path: Path):
    adapter = build_broker_execution_port(
        _context(
            tmp_path,
            provider="future_broker",
            environment="demo",
            live_trading_enabled=True,
            broker_config={"dry_run": False},
        )
    )

    assert adapter.live_trading_enabled is False
    assert adapter.dry_run is True


def test_live_binance_registry_path_keeps_real_money_activation_gate(tmp_path: Path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setenv("BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")

    def opener(request, timeout):
        seen.append({"method": request.get_method(), "url": request.full_url})
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(
                {
                    "symbols": [
                        {
                            "symbol": "XAUUSDT",
                            "status": "TRADING",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected network call: {request.full_url}")

    adapter = build_broker_execution_port(
        _context(
            tmp_path,
            provider="binance_usdm",
            environment="live",
            live_trading_enabled=True,
            broker_config={
                "dry_run": False,
                "allowed_symbols": ["GOLD"],
                "instrument_map": {"GOLD": "XAUUSDT"},
            },
            opener=opener,
        )
    )
    from services.broker_port import BrokerOrderRequest

    with pytest.raises(RuntimeError, match="live activation gate is not real_money_ready"):
        adapter.submit_order(
            BrokerOrderRequest(
                "2026-07-18",
                {
                    "ticket_id": "ticket-live-gate",
                    "asset": "GOLD",
                    "action": "prepare_buy",
                    "entry_zone": "3999-4001",
                    "stop_loss": 3990,
                    "targets": [4010],
                    "position_size_pct": 1,
                    "order_type": "market",
                },
                latest_price=4000,
                actual_size=0.001,
            )
        )

    assert not [item for item in seen if item["method"] == "POST"]


def test_broker_modules_import_without_facade_recursion():
    import services.broker_adapter as facade
    import services.broker_composition as composition

    assert callable(facade.build_broker_adapter)
    assert callable(composition.build_broker_execution_port)


@pytest.mark.parametrize(
    ("provider", "environment"),
    [
        ("binance_usdm", "live"),
        ("tiger_openapi", "paper"),
    ],
)
def test_binance_and_tiger_share_execution_port_conformance(
    tmp_path: Path,
    provider: str,
    environment: str,
):
    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(
                {
                    "symbols": [
                        {
                            "symbol": "XAUUSDT",
                            "status": "TRADING",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected network call: {request.full_url}")

    is_tiger = provider == "tiger_openapi"
    asset = "MGC2608" if is_tiger else "GOLD"
    config = {
        "dry_run": True,
        "request_dir": f"{provider}_requests",
        "allowed_symbols": [asset],
        "contract_map": {asset: asset},
        "instrument_map": {asset: "XAUUSDT"},
        "api_key_env": f"{provider.upper()}_KEY",
        "api_secret_env": f"{provider.upper()}_SECRET",
        "api_key": "must-not-leak-key",
        "api_secret": "must-not-leak-secret",
    }
    adapter = build_broker_execution_port(
        _context(
            tmp_path,
            provider=provider,
            environment=environment,
            live_trading_enabled=True,
            broker_config=config,
            opener=opener,
        )
    )

    readiness = adapter.preflight()
    receipt = adapter.submit_order(
        BrokerOrderRequest(
            "2026-07-18",
            {
                "ticket_id": f"ticket-{provider}",
                "asset": asset,
                "action": "prepare_buy",
                "entry_zone": "3999-4001",
                "stop_loss": 3990,
                "targets": [4010],
                "position_size_pct": 1,
                "order_type": "limit",
                "time_in_force": "day",
            },
            latest_price=4000,
            actual_size=1 if is_tiger else 0.001,
        )
    )
    descriptor = adapter.descriptor.to_dict()

    for key in ("provider", "ready", "dry_run", "block_reason", "checked_at"):
        assert key in readiness
    assert receipt.ticket_id == f"ticket-{provider}"
    assert receipt.order_id
    assert receipt.status.endswith("dry_run")
    assert set(descriptor["capabilities"]) >= {"preflight", "submit_order"}
    assert "must-not-leak" not in json.dumps(
        {"descriptor": descriptor, "receipt": receipt.to_dict()},
        sort_keys=True,
    )
