from __future__ import annotations

from types import MappingProxyType

import pytest

from schemas.analysis import Analysis
from schemas.backtest import BACKTEST_EVIDENCE_SCHEMA, BacktestEvidence
from schemas.market_data import Bar
from schemas.signal import Signal
from services.backtest_local_config import LocalBacktestConfig
from services.backtest_plugin_composition import (
    build_backtest_plugin_registry,
    compose_historical_strategy_backtest,
    compose_signal_backtest,
)
from services.backtest_plugin_registry import (
    BacktestPluginRegistry,
    DuplicateBacktestPlugin,
    InvalidBacktestPlugin,
    UnknownBacktestPlugin,
)
from services.backtest_port import (
    HISTORICAL_STRATEGY_KIND,
    SIGNAL_BACKTEST_REQUEST_SCHEMA,
    SIGNAL_EVIDENCE_KIND,
    HistoricalStrategyBacktestRequest,
    SignalBacktestRequest,
)
from services.backtest_service import (
    HistoricalStrategyBacktestService,
    SignalBacktestService,
)
from services.local_backtester import LocalBacktester


def _signal() -> Signal:
    return Signal(
        signal_id="sig-port-test",
        asset="GOLD",
        asset_class="commodity",
        direction="long",
        strength=70,
        confidence=65,
        horizon="intraday",
        thesis="test",
    )


def _analysis() -> Analysis:
    return Analysis("analysis-port-test", "sig-port-test", "GOLD")


def _bar(index: int = 0) -> Bar:
    return Bar(
        "GOLD",
        "5m",
        f"2026-07-18T00:{index:02d}:00+00:00",
        4000.0,
        4002.0,
        3998.0,
        4001.0,
        10.0,
        "test",
        [],
    )


def _request() -> SignalBacktestRequest:
    return SignalBacktestRequest.from_domain(
        _signal(),
        _analysis(),
        [_bar()],
        run_context={"run_date": "2026-07-18", "nested": {"mode": "paper"}},
    )


def _valid_evidence(**overrides) -> BacktestEvidence:
    values = {
        "backtest_id": "custom-1",
        "signal_id": "sig-port-test",
        "asset": "GOLD",
        "sample_size": 20,
        "win_rate": 0.55,
        "avg_r": 0.2,
        "max_drawdown_pct": 3.0,
        "verdict": "mixed",
        "profit_factor": 1.2,
        "evaluated_bars": 100,
        "setup_count": 20,
    }
    values.update(overrides)
    return BacktestEvidence(**values)


def _historical_metrics() -> dict:
    return {
        "trades": 2,
        "wins": 1,
        "losses": 1,
        "win_rate": 0.5,
        "profit_factor": 1.2,
        "net_pnl": 10.0,
        "return_pct": 0.1,
        "max_drawdown_pct": 0.05,
        "avg_r": 0.2,
        "sharpe_per_trade": 0.3,
        "avg_bars_held": 4.0,
        "final_equity": 10_010.0,
    }


def test_signal_request_is_versioned_content_hashed_and_deeply_immutable() -> None:
    context = {"nested": {"values": [1, 2]}}
    request = SignalBacktestRequest.from_domain(
        _signal(),
        _analysis(),
        [_bar()],
        run_context=context,
    )
    original_hash = request.input_hash
    context["nested"]["values"].append(3)

    assert request.schema_version == SIGNAL_BACKTEST_REQUEST_SCHEMA
    assert request.input_hash == original_hash
    assert len(original_hash) == 64
    assert isinstance(request.run_context, MappingProxyType)
    assert request.run_context["nested"]["values"] == (1, 2)
    with pytest.raises(TypeError):
        request.run_context["new"] = True  # type: ignore[index]


def test_registry_is_kind_aware_frozen_fingerprinted_and_fail_closed() -> None:
    class SignalPort:
        def evaluate(self, _request):
            return _valid_evidence()

    class HistoricalPort:
        def run(self, _request):
            return _historical_metrics()

    registry = BacktestPluginRegistry()
    registry.register(
        "signal",
        lambda _context: SignalPort(),
        kind=SIGNAL_EVIDENCE_KIND,
        evidence_tier="test",
    )
    registry.register(
        "historical",
        lambda _context: HistoricalPort(),
        kind=HISTORICAL_STRATEGY_KIND,
        evidence_tier="test",
    )
    with pytest.raises(DuplicateBacktestPlugin):
        registry.register(
            "SIGNAL",
            lambda _context: SignalPort(),
            kind=SIGNAL_EVIDENCE_KIND,
            evidence_tier="test",
        )
    registry.freeze()

    assert len(registry.fingerprint) == 64
    assert registry.build("signal", {}, expected_kind=SIGNAL_EVIDENCE_KIND)
    with pytest.raises(InvalidBacktestPlugin, match="expected signal_evidence"):
        registry.build("historical", {}, expected_kind=SIGNAL_EVIDENCE_KIND)
    with pytest.raises(UnknownBacktestPlugin, match="unknown"):
        registry.build("unknown", {}, expected_kind=SIGNAL_EVIDENCE_KIND)
    with pytest.raises(InvalidBacktestPlugin, match="frozen"):
        registry.register(
            "late",
            lambda _context: SignalPort(),
            kind=SIGNAL_EVIDENCE_KIND,
            evidence_tier="test",
        )


def test_registry_rejects_invalid_factory_product_and_missing_capability() -> None:
    registry = BacktestPluginRegistry()
    registry.register(
        "invalid",
        lambda _context: object(),
        kind=SIGNAL_EVIDENCE_KIND,
        evidence_tier="test",
    )
    with pytest.raises(InvalidBacktestPlugin, match=r"without evaluate\(\)"):
        registry.build("invalid", {}, expected_kind=SIGNAL_EVIDENCE_KIND)
    with pytest.raises(InvalidBacktestPlugin, match="must declare evaluate"):
        BacktestPluginRegistry().register(
            "missing",
            lambda _context: object(),
            kind=SIGNAL_EVIDENCE_KIND,
            evidence_tier="test",
            capabilities=("explain",),
        )


def test_default_registry_exposes_five_explicit_plugins_without_fallback_selection() -> None:
    registry = build_backtest_plugin_registry()

    assert registry.frozen is True
    assert registry.names() == (
        "event_driven_strategy",
        "local_signal",
        "nautilus_strategy_shadow",
        "remote_signal",
        "synthetic_signal_context",
    )
    synthetic = registry.descriptor("synthetic_signal_context")
    assert synthetic.degraded_by_design is True
    assert synthetic.promotion_evidence_capable is False


def test_signal_service_reasserts_identity_and_provenance_over_adapter_fields() -> None:
    class ForgingPort:
        def evaluate(self, _request):
            return _valid_evidence(
                schema_version="forged-schema",
                backtest_plugin="forged-plugin",
                evidence_tier="forged-tier",
                input_hash="forged-input",
                registry_fingerprint="forged-registry",
                promotion_eligible=False,
            )

    registry = BacktestPluginRegistry()
    registry.register(
        "custom",
        lambda _context: ForgingPort(),
        kind=SIGNAL_EVIDENCE_KIND,
        evidence_tier="verified_test",
        promotion_evidence_capable=True,
    )
    runtime = compose_signal_backtest(
        {"backtest_plugins": {"signal": "custom"}},
        registry=registry,
    )
    request = _request()
    result = SignalBacktestService(runtime).evaluate(request)

    assert result.schema_version == BACKTEST_EVIDENCE_SCHEMA
    assert result.backtest_plugin == "custom"
    assert result.evidence_tier == "verified_test"
    assert result.input_hash == request.input_hash
    assert result.registry_fingerprint == registry.fingerprint
    assert result.promotion_eligible is True
    assert result.degraded is False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"signal_id": "wrong"}, "signal_id"),
        ({"asset": "BTC"}, "asset"),
        ({"verdict": "unknown"}, "unsupported"),
        ({"win_rate": 1.1}, "between 0 and 1"),
        ({"max_drawdown_pct": float("nan")}, "finite"),
        ({"sample_size": -1}, "non-negative integer"),
    ],
)
def test_signal_service_rejects_invalid_plugin_evidence(override, message) -> None:
    class InvalidPort:
        def evaluate(self, _request):
            return _valid_evidence(**override)

    registry = BacktestPluginRegistry()
    registry.register(
        "invalid",
        lambda _context: InvalidPort(),
        kind=SIGNAL_EVIDENCE_KIND,
        evidence_tier="test",
    )
    runtime = compose_signal_backtest(
        {"backtest_plugins": {"signal": "invalid"}},
        registry=registry,
    )

    with pytest.raises(ValueError, match=message):
        SignalBacktestService(runtime).evaluate(_request())


def test_local_empty_history_stays_thin_and_never_falls_through_to_synthetic() -> None:
    runtime = compose_signal_backtest(
        {"backtest_plugins": {"signal": "local_signal"}},
        strategy_config={},
    )
    request = SignalBacktestRequest.from_domain(_signal(), _analysis(), [])

    evidence = SignalBacktestService(runtime).evaluate(request)

    assert evidence.backtest_id.startswith("local5m_")
    assert evidence.verdict == "thin"
    assert evidence.sample_size == 0
    assert evidence.backtest_plugin == "local_signal"
    assert evidence.evidence_tier == "local_historical_signal"
    assert evidence.promotion_eligible is False
    assert evidence.degraded is False


def test_local_adapter_preserves_variant_config_and_simulation_metrics() -> None:
    config = LocalBacktestConfig(
        stop_pct=0.004,
        target_pct=0.009,
        max_hold_bars=12,
        min_sample_size=5,
        supportive_min_win_rate=0.51,
        supportive_min_avg_r=-0.01,
        supportive_min_profit_factor=1.05,
        supportive_max_drawdown_pct=6.0,
        mixed_min_profit_factor=0.95,
        mixed_min_avg_r=-0.1,
    )
    assert LocalBacktestConfig.from_strategy_config(config.to_strategy_config()) == config
    bars = [_bar(index) for index in range(30)]
    expected = LocalBacktester(config).evaluate(_signal(), _analysis(), bars)
    request = SignalBacktestRequest.from_domain(
        _signal(),
        _analysis(),
        bars,
        backtest_config=config.to_strategy_config(),
    )
    actual = SignalBacktestService(
        compose_signal_backtest({"backtest_plugins": {"signal": "local_signal"}})
    ).evaluate(request)

    for field in (
        "backtest_id",
        "sample_size",
        "win_rate",
        "avg_r",
        "max_drawdown_pct",
        "verdict",
        "profit_factor",
        "evaluated_bars",
        "setup_count",
        "skipped_reason",
    ):
        assert getattr(actual, field) == getattr(expected, field)


def test_explicit_synthetic_plugin_is_always_degraded_and_promotion_ineligible() -> None:
    runtime = compose_signal_backtest(
        {"backtest_plugins": {"signal": "synthetic_signal_context"}}
    )

    evidence = SignalBacktestService(runtime).evaluate(_request())

    assert evidence.backtest_plugin == "synthetic_signal_context"
    assert evidence.evidence_tier == "synthetic_context"
    assert evidence.degraded is True
    assert evidence.promotion_eligible is False
    assert evidence.skipped_reason == "synthetic_context_not_historical_evidence"


def test_degraded_by_design_overrides_capable_high_sample_plugin() -> None:
    class CapableButDegradedPort:
        def evaluate(self, _request):
            return _valid_evidence(sample_size=100, verdict="supportive")

    registry = BacktestPluginRegistry()
    registry.register(
        "capable_but_degraded",
        lambda _context: CapableButDegradedPort(),
        kind=SIGNAL_EVIDENCE_KIND,
        evidence_tier="degraded_research",
        promotion_evidence_capable=True,
        degraded_by_design=True,
    )
    runtime = compose_signal_backtest(
        {"backtest_plugins": {"signal": "capable_but_degraded"}},
        registry=registry,
    )

    evidence = SignalBacktestService(runtime).evaluate(_request())

    assert evidence.sample_size == 100
    assert evidence.degraded is True
    assert evidence.promotion_eligible is False


def test_historical_service_rejects_invalid_plugin_result_before_callers_write() -> None:
    class InvalidHistoricalPort:
        def run(self, _request):
            return {"trades": 1}

    registry = BacktestPluginRegistry()
    registry.register(
        "invalid_history",
        lambda _context: InvalidHistoricalPort(),
        kind=HISTORICAL_STRATEGY_KIND,
        evidence_tier="test",
    )
    runtime = compose_historical_strategy_backtest(
        {"backtest_plugins": {"historical_strategy": "invalid_history"}},
        registry=registry,
    )
    request = HistoricalStrategyBacktestRequest(
        strategy={"strategy_id": "test"},
        bars=(_bar().to_dict(),),
        signals=({"index": 0, "direction": "long"},),
        backtest_config={},
        cost_rules={},
    )

    with pytest.raises(ValueError, match="missing metrics"):
        HistoricalStrategyBacktestService(runtime).run(request)


def test_historical_service_preserves_infinite_profit_factor_for_zero_loss_runs() -> None:
    class ZeroLossHistoricalPort:
        def run(self, _request):
            return {**_historical_metrics(), "profit_factor": float("inf")}

    registry = BacktestPluginRegistry()
    registry.register(
        "zero_loss",
        lambda _context: ZeroLossHistoricalPort(),
        kind=HISTORICAL_STRATEGY_KIND,
        evidence_tier="test",
    )
    runtime = compose_historical_strategy_backtest(
        {"backtest_plugins": {"historical_strategy": "zero_loss"}},
        registry=registry,
    )
    request = HistoricalStrategyBacktestRequest(
        strategy={"strategy_id": "test"},
        bars=(_bar().to_dict(),),
        signals=({"index": 0, "direction": "long"},),
        backtest_config={},
        cost_rules={},
    )

    result = HistoricalStrategyBacktestService(runtime).run(request)

    assert result["profit_factor"] == float("inf")
