import json
from pathlib import Path

import pytest

from services.broker_composition import (
    BrokerBuildContext,
    BrokerPlugin,
    BrokerPluginKey,
    BrokerPluginRegistry,
    build_demo_broker_execution_port,
    build_broker_execution_port,
    build_configured_live_broker_execution_port,
    build_broker_reconciliation_port,
)
from services.broker_port import BrokerOrderRequest
from services.park_recording_track import ParkRecordingTrack


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


def test_registry_rejects_plugin_capability_mismatch(tmp_path: Path):
    from services.broker_adapter import PaperBrokerAdapter
    from services.broker_port import BrokerCapabilities, BrokerCapability

    registry = BrokerPluginRegistry()
    registry.register(
        BrokerPlugin(
            BrokerPluginKey("live", "mismatch", "*"),
            execution_factory=lambda context: PaperBrokerAdapter(context.output_root),
            capabilities=BrokerCapabilities(
                frozenset(
                    {
                        BrokerCapability.PREFLIGHT,
                        BrokerCapability.SUBMIT_ORDER,
                        BrokerCapability.CANCEL_ORDER,
                    }
                )
            ),
        )
    )

    with pytest.raises(TypeError, match="capability declaration does not match"):
        registry.build_execution(
            _context(tmp_path, provider="mismatch", environment="live")
        )


@pytest.mark.parametrize(
    ("context_kwargs", "expected_name"),
    [
        ({"mode": "paper", "provider": "binance_usdm", "environment": "live"}, "paper"),
        ({"provider": "tiger_openapi", "environment": "paper"}, "tiger_openapi_paper"),
        ({"provider": "binance_usdm", "environment": "demo"}, "binance_demo"),
        ({"provider": "binance_usdm", "environment": "testnet"}, "binance_usdm_testnet"),
        ({"provider": "binance_usdm", "environment": "live"}, "live"),
        ({"provider": "oanda_rest", "environment": "practice"}, "oanda_rest"),
        ({"provider": "mt5_file_bridge", "environment": "live"}, "mt5_file_bridge"),
        ({"provider": "manual_gateway", "environment": "live"}, "live"),
    ],
)
def test_default_registry_builds_expected_adapter(
    tmp_path: Path,
    context_kwargs: dict,
    expected_name: str,
):
    adapter = build_broker_execution_port(_context(tmp_path, **context_kwargs))

    assert adapter.name == expected_name


def test_standard_broker_paper_composition_is_explicit_and_read_only(tmp_path: Path):
    adapter = build_broker_execution_port(
        _context(
            tmp_path,
            mode="paper",
            provider="standard_broker",
            environment="paper",
            broker_config={"broker_id": "hyperliquid"},
        )
    )

    assert adapter.name == "standard_broker_paper"
    assert adapter.provider == "standard_broker"
    assert adapter.preflight()["network_io"] is False
    assert adapter.preflight()["real_money_eligible"] is False
    receipt = adapter.request("market_data", "read", {"request_id": "host-read-1"})
    assert receipt.network_io is False
    event = adapter.record_receipt(
        ParkRecordingTrack(tmp_path / "recording"),
        record_window_id="2026-08-21_DAY",
        strategy_session_id="session-host",
        strategy_revision_id="revision-host",
        occurred_at="2026-08-21T01:00:00+00:00",
        receipt=receipt,
    )
    assert event["source"] == "standard-broker.paper"
    blocked = adapter.record_capability_gap(
        ParkRecordingTrack(tmp_path / "recording"),
        record_window_id="2026-08-21_DAY",
        strategy_session_id="session-host",
        strategy_revision_id="revision-host",
        occurred_at="2026-08-21T01:00:01+00:00",
        port="order_execution",
        operation="submit",
        reason="capability_gap",
    )
    assert blocked["event_type"] == "standard_broker_capability_gap"
    assert blocked["payload"]["status"] == "blocked"
    with pytest.raises(RuntimeError, match="submit_order"):
        adapter.submit_order(
            BrokerOrderRequest(
                run_date="2026-08-21",
                ticket={"ticket_id": "paper-host-submit"},
            )
        )


@pytest.mark.parametrize("environment", ["paper", "mainnet", "live"])
def test_standard_broker_rejects_unsupported_selection_without_fallback(tmp_path: Path, environment: str):
    with pytest.raises(RuntimeError, match="unsupported standard_broker selection|unsupported broker selection"):
        build_broker_execution_port(
            _context(
                tmp_path,
                mode="live" if environment == "live" else "paper",
                provider="standard_broker",
                environment=environment,
                broker_config={"broker_id": "binance"},
            )
        )


def test_standard_broker_demo_composition_cannot_fall_back_to_legacy(tmp_path: Path):
    with pytest.raises(RuntimeError, match="unsupported standard_broker selection"):
        build_demo_broker_execution_port(
            _context(
                tmp_path,
                mode="live",
                provider="standard_broker",
                environment="demo",
                broker_config={"broker_id": "hyperliquid"},
            )
        )


@pytest.mark.parametrize("environment", ["mainnet", "live"])
def test_standard_broker_environment_gate_is_explicit_and_non_networked(
    tmp_path: Path,
    environment: str,
):
    adapter = build_broker_execution_port(
        _context(
            tmp_path,
            mode="live",
            provider="standard_broker",
            environment=environment,
            broker_config={
                "broker_id": "hyperliquid",
                "environment_fingerprint": f"hyperliquid:{environment}:fingerprint",
                "account_id": f"{environment}-account",
                "credential_source": f"HL_{environment.upper()}_CREDENTIAL",
                "runtime_id": f"runtime-{environment}",
                "ledger_namespace": f"ledger.standard-broker.{environment}",
                "release_sha": "a" * 40,
            },
        )
    )

    assert adapter.provider == "standard_broker"
    assert adapter.preflight()["ready"] is False
    assert adapter.preflight()["network_io"] is False
    assert adapter.preflight()["real_money_eligible"] is False
    assert adapter.preflight()["blocker"] == "capability_gate_pending"
    assert adapter.descriptor.environment == ("mainnet" if environment == "live" else environment)


def test_standard_broker_requires_explicit_environment_identity(tmp_path: Path):
    with pytest.raises(ValueError, match="explicit environment"):
        BrokerBuildContext(
            output_root=tmp_path / "outputs",
            execution_mode="live",
            live_trading_enabled=False,
            broker_config={
                "provider": "standard_broker",
                "broker_id": "hyperliquid",
            },
        )


@pytest.mark.parametrize(
    ("selection_environment", "configured_environment"),
    [("testnet", "mainnet"), ("paper", "mainnet")],
)
def test_standard_broker_rejects_contradictory_environment_sources(
    tmp_path: Path,
    selection_environment: str,
    configured_environment: str,
):
    with pytest.raises(ValueError, match="contradictory.*environment"):
        BrokerBuildContext(
            output_root=tmp_path / "outputs",
            execution_mode="live",
            live_trading_enabled=False,
            selection_environment=selection_environment,
            broker_config={
                "provider": "standard_broker",
                "broker_id": "hyperliquid",
                "environment": configured_environment,
            },
        )


def test_standard_broker_accepts_live_mainnet_alias_pair(tmp_path: Path):
    context = BrokerBuildContext(
        output_root=tmp_path / "outputs",
        execution_mode="live",
        live_trading_enabled=False,
        selection_environment="live",
        broker_config={
            "provider": "standard_broker",
            "broker_id": "hyperliquid",
            "environment": "mainnet",
        },
    )

    assert context.environment == "live"


@pytest.mark.parametrize("environment", ["mainnet", "live"])
def test_configured_standard_broker_preserves_environment_selection(
    tmp_path: Path,
    environment: str,
):
    adapter = build_configured_live_broker_execution_port(
        tmp_path / "outputs",
        live_trading_enabled=False,
        broker_config={
            "provider": "standard_broker",
            "broker_id": "hyperliquid",
            "environment": environment,
            "environment_fingerprint": f"hyperliquid:{environment}:fingerprint",
            "account_id": f"{environment}-account",
            "credential_source": f"HL_{environment.upper()}_CREDENTIAL",
            "runtime_id": f"runtime-{environment}",
            "ledger_namespace": f"ledger.standard-broker.{environment}",
            "release_sha": "a" * 40,
            "dry_run": True,
        },
    )

    assert adapter.preflight()["environment"] == (
        "mainnet" if environment == "live" else environment
    )


def test_standard_broker_testnet_requires_local_fixture_and_approval(tmp_path: Path):
    from services.standard_broker_testnet import StandardBrokerTestnetHostError

    with pytest.raises(StandardBrokerTestnetHostError, match="missing backend"):
        build_broker_execution_port(
            _context(
                tmp_path,
                mode="live",
                provider="standard_broker",
                environment="testnet",
                broker_config={"broker_id": "hyperliquid"},
            )
        )


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


@pytest.mark.parametrize("environment", ["demo", "testnet"])
def test_non_mainnet_adapter_overrides_a_malicious_mainnet_base_url(
    tmp_path: Path,
    environment: str,
):
    adapter = build_broker_execution_port(
        _context(
            tmp_path,
            provider="binance_usdm",
            environment=environment,
            broker_config={"base_url": "https://fapi.binance.com"},
        )
    )

    assert adapter.broker_config["base_url"] == "https://demo-fapi.binance.com"
    assert adapter.broker_config["environment"] == environment


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

    with pytest.raises(RuntimeError, match="source-bound Live activation/canary"):
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


@pytest.mark.parametrize("stored_environment", ["demo", "testnet"])
def test_configured_live_path_does_not_treat_stored_binance_environment_as_authority(
    tmp_path: Path,
    monkeypatch,
    stored_environment: str,
):
    from services.binance_usdm_broker_adapter import BinanceUsdmBrokerAdapter

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
                                {
                                    "filterType": "PRICE_FILTER",
                                    "tickSize": "0.01",
                                },
                                {
                                    "filterType": "MARKET_LOT_SIZE",
                                    "stepSize": "0.001",
                                    "minQty": "0.001",
                                },
                                {
                                    "filterType": "MIN_NOTIONAL",
                                    "notional": "5",
                                },
                            ],
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected network call: {request.full_url}")

    adapter = build_configured_live_broker_execution_port(
        tmp_path / "outputs",
        True,
        {
            "provider": "binance_usdm",
            "environment": stored_environment,
            "dry_run": False,
            "allowed_symbols": ["GOLD"],
            "instrument_map": {"GOLD": "XAUUSDT"},
        },
        opener=opener,
    )

    assert type(adapter) is BinanceUsdmBrokerAdapter
    with pytest.raises(
        RuntimeError,
            match="source-bound Live activation/canary",
    ):
        adapter.submit_order(
            BrokerOrderRequest(
                "2026-07-18",
                {
                    "ticket_id": f"configured-{stored_environment}",
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
