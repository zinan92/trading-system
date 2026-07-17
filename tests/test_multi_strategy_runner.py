import json
from pathlib import Path

from schemas.market_data import PaperOrder
from services.broker_port import execution_capabilities_for
from services.journal_store import load_json, write_json
from services.multi_strategy_runner import MultiStrategyRunner
from services.order_lifecycle import OrderLifecycleStore
from services.strategy_registry import StrategyRegistry


def _classification(family: str = "test", role: str = "test_strategy") -> dict:
    return {
        "family": family,
        "style": "test_style",
        "directionality": "long_short",
        "frequency_bucket": "medium",
        "role": role,
        "holding_period": "intraday",
        "expected_trades_per_day_min": 1,
        "expected_trades_per_day_max": 3,
        "return_profile": "test_profile",
        "risk_profile": "medium",
    }


# Two DIVERGENT strategies (identical twins would prove nothing): one is forced
# long (thresholds at 0), the other can never trade (thresholds impossibly high).
_DIVERGENT = {
    "strat_long": {
        "symbol": "GOLD",
        "classification": _classification("test_long"),
        "position_gate": {"enabled": False},
        "signal": {"long_strength_min": 0, "long_confidence_min": 0, "short_strength_max": -1, "event_block_below": 0},
    },
    "strat_watch": {
        "symbol": "GOLD",
        "classification": _classification("test_watch"),
        "position_gate": {"enabled": False},
        "signal": {"long_strength_min": 999, "long_confidence_min": 999, "short_strength_max": -1, "event_block_below": 0},
    },
}


def _offline_env(monkeypatch, root: Path, db: Path) -> None:
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4569.34")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-05-10T00:00:00+00:00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")
    # Offline synthetic data would otherwise be blocked by the data-quality gate
    # (correctly) — disable it so the strategies' own thresholds drive the
    # divergent long/watch outcome we're testing isolation against.
    monkeypatch.setenv("TRADING_ORCHESTRATOR_DISABLE_DATA_QUALITY_GATE", "1")


_LIVE_FLAGGED = {
    "strat_live": {
        "symbol": "GOLD",
        "live": True,
        "classification": _classification("test_live"),
        "position_gate": {"enabled": False},
        "signal": {"long_strength_min": 0, "long_confidence_min": 0, "short_strength_max": -1, "event_block_below": 0},
    },
}

_ACTIVE_DEMO_LONG = {
    "gold_1m_macd": {
        "symbol": "GOLD",
        "classification": _classification("chan", "active_position_gated_strategy"),
        "position_gate": {"enabled": False},
        "signal": {"long_strength_min": 0, "long_confidence_min": 0, "short_strength_max": -1, "event_block_below": 0},
    },
}

_BINANCE_DEMO_ENABLED_CONFIG = {
    "output_root": "outputs",
    "live_trading_enabled": False,
    "broker": {"provider": "binance_usdm"},
    "demo_trading": {
        "enabled": True,
        "active_strategy_id": "gold_1m_macd",
        "broker_profile": "binance_usdm",
        "request_dir": "demo_order_requests",
        "max_order_quantity": 2.0,
        "require_flat_before_entry": True,
    },
}


def _enable_binance_demo(monkeypatch) -> None:
    monkeypatch.setattr("services.multi_strategy_runner.load_pipeline_config", lambda: _BINANCE_DEMO_ENABLED_CONFIG)


def _demo_ticket(run_date: str, suffix: str = "recover") -> dict:
    compact = run_date.replace("-", "")
    return {
        "ticket_id": f"ticket_gold_{compact}_{suffix}",
        "signal_id": f"sig_gold_{suffix}",
        "asset": "GOLD",
        "asset_class": "commodity",
        "action": "prepare_buy",
        "entry_zone": "4520-4530",
        "stop_loss": 4500,
        "targets": [4560],
        "position_size_pct": 8,
        "max_loss_pct": 0.2,
        "order_type": "market",
        "time_in_force": "day",
        "paper_only": True,
    }


def _demo_limit_ticket(run_date: str, suffix: str = "limit_expire") -> dict:
    ticket = _demo_ticket(run_date, suffix)
    ticket.update(
        {
            "order_type": "limit",
            "time_in_force": "gtc",
            "entry_zone": "99.80-99.80",
            "entry_order_limit_price": 99.8,
            "entry_order_ttl_bars": 10,
            "entry_order_timeframe": "1m",
            "entry_order_created_bar_timestamp": f"{run_date}T00:00:00+00:00",
            "stop_loss": 99.6,
            "targets": [100.6],
        }
    )
    return ticket


def _seed_demo_pending(scoped: Path, run_date: str, ticket: dict) -> None:
    write_json(scoped / "trade_tickets" / f"{run_date}.json", [ticket])
    write_json(
        scoped / "journal_pending" / f"{run_date}.json",
        [{**ticket, "journal_id": f"j_{ticket['ticket_id']}", "decision_status": "pending_manual_decision"}],
    )


def _seed_submitting_intent(
    scoped: Path,
    run_date: str,
    ticket: dict,
    *,
    order_id: str = "demo_order_recovery",
    requested_price: float = 4525.5,
    requested_quantity: float = 0.002,
) -> OrderLifecycleStore:
    store = OrderLifecycleStore(scoped)
    store.write_intent(
        run_date,
        order_id=order_id,
        ticket_id=ticket["ticket_id"],
        idempotency_key=order_id[:36],
        requested_quantity=requested_quantity,
        requested_price=requested_price,
        source="binance_usdm:demo",
        metadata={"test": "runner_restart_recovery", "ticket": ticket},
    )
    store.transition(run_date, order_id, "submitting", reason="crash_after_write_ahead_intent")
    return store


def test_live_flagged_strategy_records_block_when_live_disabled(monkeypatch, tmp_path: Path):
    """A live-flagged strategy routes to the live adapter, but with
    live_trading_enabled false it records the block and keeps running — no real
    order, no hard fleet error."""
    root = tmp_path / "outputs"
    _offline_env(monkeypatch, root, tmp_path / "market.db")

    summary = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_LIVE_FLAGGED)).run("2026-05-10", paper_auto_approve=True)

    strat = summary["strategies"][0]
    assert strat["status"] == "ok"  # graceful, not "error"
    assert strat["executed_ticket"] is None
    assert "live trading is disabled" in (strat["execution_error"] or "")


def test_live_strategy_routes_to_live_broker_adapter(tmp_path: Path):
    from services.strategy_registry import Strategy

    runner = MultiStrategyRunner(output_root=tmp_path / "out", registry=StrategyRegistry(_DIVERGENT))
    scoped = tmp_path / "out" / "strategies" / "x"

    assert runner._broker_adapter_for(Strategy("p", "GOLD", {"signal": {}}, live=False), scoped) is None
    live_adapter = runner._broker_adapter_for(Strategy("l", "GOLD", {"signal": {}}, live=True), scoped)
    assert live_adapter is not None and live_adapter.name == "live"


def test_only_configured_chan2_strategy_routes_to_binance_demo_adapter(monkeypatch, tmp_path: Path):
    from services.strategy_registry import Strategy

    _enable_binance_demo(monkeypatch)
    runner = MultiStrategyRunner(output_root=tmp_path / "out", registry=StrategyRegistry(_DIVERGENT))
    scoped = tmp_path / "out" / "strategies" / "x"

    active = runner._broker_adapter_for(Strategy("gold_1m_macd", "GOLD", {"signal": {}}, live=False), scoped)
    inactive = runner._broker_adapter_for(Strategy("gold_1m_chan", "GOLD", {"signal": {}}, live=False), scoped)

    assert active is not None and active.name == "binance_demo"
    assert inactive is None


def test_configured_demo_strategy_can_route_to_tiger_profile_adapter(monkeypatch, tmp_path: Path):
    from services.strategy_registry import Strategy

    config = {
        "output_root": "outputs",
        "live_trading_enabled": False,
        "broker": {"provider": "binance_usdm"},
        "demo_trading": {
            "enabled": True,
            "active_strategy_id": "gold_1m_macd",
            "broker_profile": "tiger_openapi_paper",
        },
        "broker_profiles": {
            "tiger_openapi_paper": {
                "provider": "tiger_openapi",
                "environment": "paper",
                "dry_run": True,
                "props_path_env": "TIGER_OPENAPI_CONFIG_PATH",
                "request_dir": "tiger_order_requests",
                "network_order_submission": "not_implemented_fail_closed",
                "confirm_tiger_paper_orders": False,
            }
        },
    }
    monkeypatch.setattr("services.multi_strategy_runner.load_pipeline_config", lambda: config)
    runner = MultiStrategyRunner(output_root=tmp_path / "out", registry=StrategyRegistry(_DIVERGENT))

    active = runner._broker_adapter_for(Strategy("gold_1m_macd", "GOLD", {"signal": {}}, live=False), tmp_path / "out" / "s")
    inactive = runner._broker_adapter_for(Strategy("gold_1m_chan", "GOLD", {"signal": {}}, live=False), tmp_path / "out" / "s")

    assert active is not None and active.name == "tiger_openapi_paper"
    assert active.broker_config["provider"] == "tiger_openapi"
    assert active.broker_config["dry_run"] is True
    assert inactive is None


def test_unsupported_demo_provider_falls_back_to_paper_instead_of_arming_live(monkeypatch, tmp_path: Path):
    from services.strategy_registry import Strategy

    config = {
        "output_root": "outputs",
        "live_trading_enabled": True,
        "broker": {"provider": "oanda_rest", "environment": "live", "dry_run": False},
        "demo_trading": {
            "enabled": True,
            "active_strategy_id": "gold_1m_macd",
            "broker_profile": "oanda_live",
        },
        "broker_profiles": {
            "oanda_live": {
                "provider": "oanda_rest",
                "environment": "live",
                "dry_run": False,
            }
        },
    }
    monkeypatch.setattr("services.multi_strategy_runner.load_pipeline_config", lambda: config)
    runner = MultiStrategyRunner(output_root=tmp_path / "out", registry=StrategyRegistry(_DIVERGENT))
    strategy = Strategy("gold_1m_macd", "GOLD", {"signal": {}}, live=False)

    assert runner._active_demo_broker_config(strategy, config=config) is None
    assert runner._broker_adapter_for(strategy, tmp_path / "out" / "s") is None


def test_tiger_demo_execution_profile_is_guarded_and_secret_safe(monkeypatch, tmp_path: Path):
    from services.strategy_registry import Strategy

    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(tmp_path / "tiger_openapi_config.properties"))
    config = {
        "output_root": "outputs",
        "live_trading_enabled": False,
        "broker": {"provider": "binance_usdm"},
        "demo_trading": {
            "enabled": True,
            "active_strategy_id": "gold_1m_macd",
            "broker_profile": "tiger_openapi_paper",
        },
        "broker_profiles": {
            "tiger_openapi_paper": {
                "provider": "tiger_openapi",
                "environment": "paper",
                "dry_run": True,
                "props_path_env": "TIGER_OPENAPI_CONFIG_PATH",
                "request_dir": "tiger_order_requests",
                "network_order_submission": "not_implemented_fail_closed",
                "confirm_tiger_paper_orders": False,
            }
        },
    }
    monkeypatch.setattr("services.multi_strategy_runner.load_pipeline_config", lambda: config)
    runner = MultiStrategyRunner(output_root=tmp_path / "out", registry=StrategyRegistry(_DIVERGENT))

    profile = runner._execution_profile_for(Strategy("gold_1m_macd", "GOLD", {"signal": {}}, live=False))

    assert profile["adapter"] == "tiger_openapi_paper"
    assert profile["mode"] == "paper_broker_port"
    assert profile["provider"] == "tiger_openapi"
    assert profile["profile"] == "tiger_openapi_paper"
    assert profile["dry_run"] is True
    assert profile["armed"] is False
    assert profile["credential_env_names"] == ["TIGER_OPENAPI_CONFIG_PATH"]
    assert str(tmp_path) not in json.dumps(profile)


def test_tiger_demo_reconciliation_uses_tiger_read_only_service(monkeypatch, tmp_path: Path):
    from services.strategy_registry import Strategy
    import services.tiger_openapi_reconciliation as tiger_recon

    config = {
        "output_root": "outputs",
        "broker": {"provider": "binance_usdm"},
        "demo_trading": {
            "enabled": True,
            "active_strategy_id": "gold_1m_macd",
            "broker_profile": "tiger_openapi_paper",
        },
        "broker_profiles": {
            "tiger_openapi_paper": {
                "provider": "tiger_openapi",
                "environment": "paper",
                "dry_run": True,
                "props_path_env": "TIGER_OPENAPI_CONFIG_PATH",
            }
        },
    }
    captured = {}

    class FakeTigerReconciliation:
        def __init__(self, output_root, broker_config):
            captured["output_root"] = output_root
            captured["broker_config"] = broker_config

        def run(self, run_date):
            return {"provider": "tiger_openapi", "run_date": run_date, "reconciled": True, "confirmation_status": "confirmed_flat"}

    monkeypatch.setattr("services.multi_strategy_runner.load_pipeline_config", lambda: config)
    monkeypatch.setattr(tiger_recon, "TigerOpenApiPaperReconciliation", FakeTigerReconciliation)
    runner = MultiStrategyRunner(output_root=tmp_path / "out", registry=StrategyRegistry(_DIVERGENT))

    report = runner._demo_reconciliation_for(
        Strategy("gold_1m_macd", "GOLD", {"signal": {}}, live=False),
        tmp_path / "out" / "strategies" / "gold_1m_macd",
        "2026-07-05",
    )

    assert report["provider"] == "tiger_openapi"
    assert captured["broker_config"]["provider"] == "tiger_openapi"
    assert runner._demo_reconciliation_block_reason(
        {"provider": "tiger_openapi", "drifts": [{"reason": "drift"}]}
    ) == "broker reconciliation drift: drift"


def test_demo_execution_profile_surfaces_armed_state_without_secrets(tmp_path: Path, monkeypatch):
    from services.strategy_registry import Strategy

    _enable_binance_demo(monkeypatch)
    monkeypatch.setenv("BINANCE_API_KEY", "demo-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "demo-secret")
    runner = MultiStrategyRunner(output_root=tmp_path / "out", registry=StrategyRegistry(_DIVERGENT))

    active = runner._execution_profile_for(Strategy("gold_1m_macd", "GOLD", {"signal": {}}, live=False))
    inactive = runner._execution_profile_for(Strategy("gold_1m_macd_ungated", "GOLD", {"signal": {}}, live=False))

    assert active["adapter"] == "binance_demo"
    assert active["mode"] == "demo_broker_port"
    assert active["armed"] is True
    assert active["credentials_present"] is True
    assert active["endpoint"] == "https://demo-fapi.binance.com"
    assert active["live_endpoint_allowed"] is False
    assert active["symbol"] == "XAUUSDT"
    assert "demo-secret" not in json.dumps(active)
    assert inactive["adapter"] == "paper"
    assert inactive["armed"] is False


def test_runner_summary_stamps_generated_at_for_vitals_age_check(monkeypatch, tmp_path: Path):
    # SystemVitals' mid-session-death detection ages against summary.generated_at;
    # pin that the producer actually emits a fresh, parseable, tz-aware timestamp.
    from datetime import datetime

    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    _offline_env(monkeypatch, root, db)
    summary = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_DIVERGENT)).run("2026-05-10", paper_auto_approve=True)

    assert summary.get("generated_at")
    assert datetime.fromisoformat(summary["generated_at"]).tzinfo is not None
    persisted = json.loads((root / "strategies" / "summary_current.json").read_text())[-1]
    assert persisted["generated_at"] == summary["generated_at"]


def test_runner_refreshes_backend_maturity_after_strategy_frequency(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    _offline_env(monkeypatch, root, db)
    run_date = "2026-05-10"

    class FakeBackendMaturityAudit:
        def __init__(self, output_root: Path) -> None:
            self.output_root = output_root

        def run(self, date: str) -> dict:
            frequency = json.loads((self.output_root / "strategy_frequency" / "current.json").read_text())[-1]
            payload = {
                "run_date": date,
                "generated_at": "2026-05-10T00:00:00+00:00",
                "status": "pass",
                "summary": {
                    "portfolio_executed_count": frequency["summary"]["portfolio_executed_count"],
                },
            }
            (self.output_root / "backend_maturity").mkdir(parents=True, exist_ok=True)
            (self.output_root / "backend_maturity" / "current.json").write_text(json.dumps([payload]))
            return payload

    monkeypatch.setattr("services.multi_strategy_runner.BackendMaturityAudit", FakeBackendMaturityAudit)

    summary = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_DIVERGENT)).run(run_date, paper_auto_approve=True)

    frequency = json.loads((root / "strategy_frequency" / "current.json").read_text())[-1]
    maturity = json.loads((root / "backend_maturity" / "current.json").read_text())[-1]
    assert summary["backend_maturity"]["status"] == "pass"
    assert maturity["summary"]["portfolio_executed_count"] == frequency["summary"]["portfolio_executed_count"]


def test_enabled_strategy_without_complete_classification_is_skipped(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    _offline_env(monkeypatch, root, tmp_path / "market_data.db")
    bad = {
        "missing_classification": {
            "symbol": "GOLD",
            "signal": {"long_strength_min": 0, "long_confidence_min": 0, "short_strength_max": -1, "event_block_below": 0},
        }
    }

    summary = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(bad)).run("2026-05-10", paper_auto_approve=True)

    assert summary["classification_audit"]["status"] == "fail"
    assert summary["strategies"][0]["strategy_id"] == "missing_classification"
    assert summary["strategies"][0]["status"] == "skipped"
    assert summary["strategies"][0]["reason"] == "classification_incomplete"
    assert not (root / "strategies" / "missing_classification" / "signals" / "2026-05-10.json").exists()


def test_runner_ignores_disabled_strategy_entries(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    _offline_env(monkeypatch, root, tmp_path / "market_data.db")
    config = {
        **_DIVERGENT,
        "disabled_swing": {
            "symbol": "GOLD",
            "enabled": False,
            "classification": _classification("disabled"),
            "signal": {"long_strength_min": 0, "long_confidence_min": 0, "short_strength_max": -1, "event_block_below": 0},
        },
    }

    summary = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(config)).run("2026-05-10", paper_auto_approve=True)

    assert summary["strategy_count"] == 2
    assert "disabled_swing" not in {row["strategy_id"] for row in summary["strategies"]}
    assert not (root / "strategies" / "disabled_swing").exists()


def test_runner_noops_gracefully_when_all_strategies_are_disabled(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    _offline_env(monkeypatch, root, tmp_path / "market_data.db")
    disabled = {
        "disabled_only": {
            "symbol": "GOLD",
            "enabled": False,
            "classification": _classification("disabled"),
            "signal": {"long_strength_min": 0, "long_confidence_min": 0, "short_strength_max": -1, "event_block_below": 0},
        },
    }

    summary = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(disabled)).run("2026-05-10", paper_auto_approve=True)

    assert summary["strategy_count"] == 0
    assert summary["strategies"] == []
    assert summary["classification_audit"]["status"] == "pass"
    assert (root / "strategies" / "summary_current.json").exists()


def test_runs_each_strategy_in_isolated_namespace(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    _offline_env(monkeypatch, root, tmp_path / "market_data.db")
    run_date = "2026-05-10"

    runner = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_DIVERGENT))
    summary = runner.run(run_date, paper_auto_approve=True)

    assert summary["strategy_count"] == 2
    by_id = {s["strategy_id"]: s for s in summary["strategies"]}
    assert by_id["strat_long"]["status"] == "ok"
    assert by_id["strat_watch"]["status"] == "ok"
    assert by_id["strat_long"]["risk_monitor_status"] in {"pass", "warn", "block"}
    assert isinstance(by_id["strat_long"]["risk_kill_switch_active"], bool)

    # Each strategy has its OWN namespace with its OWN signal.
    long_sig = json.loads((root / "strategies" / "strat_long" / "signals" / f"{run_date}.json").read_text())
    watch_sig = json.loads((root / "strategies" / "strat_watch" / "signals" / f"{run_date}.json").read_text())
    assert long_sig[0]["direction"] == "long"     # divergent: ran with its own params
    assert watch_sig[0]["direction"] == "watch"
    assert (root / "strategies" / "strat_long" / "risk_monitor" / "current.json").exists()
    assert (root / "strategies" / "strat_watch" / "risk_monitor" / "current.json").exists()


def test_active_demo_reconciliation_drift_blocks_auto_execution_before_broker(monkeypatch, tmp_path: Path):
    _enable_binance_demo(monkeypatch)
    root = tmp_path / "outputs"
    _offline_env(monkeypatch, root, tmp_path / "market_data.db")
    run_date = "2026-05-10"
    runner = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_ACTIVE_DEMO_LONG))

    monkeypatch.setattr(
        runner,
        "_demo_reconciliation_for",
        lambda strategy, scoped, date: {
            "run_date": date,
            "reconciled": False,
            "error": "",
            "drift_count": 1,
            "drifts": [{"exchange_symbol": "XAUUSDT", "reason": "exchange position has no local record"}],
        },
    )

    original_broker_factory = runner._broker_adapter_for
    broker_factory_calls = []

    def diagnostic_broker(strategy, scoped):
        broker_factory_calls.append(strategy.strategy_id)
        return original_broker_factory(strategy, scoped)

    monkeypatch.setattr(runner, "_broker_adapter_for", diagnostic_broker)

    summary = runner.run(run_date, paper_auto_approve=True)
    result = summary["strategies"][0]

    assert result["strategy_id"] == "gold_1m_macd"
    assert result["status"] == "ok"
    assert result["pending_tickets"] >= 1
    assert result["executed_ticket"] is None
    assert "reconciliation drift" in result["execution_error"]
    assert result["demo_reconciliation_status"] == "drift"
    assert result["demo_reconciliation_drift_count"] == 1
    assert broker_factory_calls == ["gold_1m_macd"]  # descriptor/preflight only; no execution call


def test_runner_recovers_submitting_demo_intent_before_auto_approve(monkeypatch, tmp_path: Path):
    _enable_binance_demo(monkeypatch)
    root = tmp_path / "outputs"
    run_date = "2026-06-30"
    runner = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_ACTIVE_DEMO_LONG))
    strategy = runner.registry.get("gold_1m_macd")
    scoped = runner.strategy_root("gold_1m_macd")
    ticket = _demo_ticket(run_date, "restart")
    next_ticket = _demo_ticket(run_date, "next")
    order_id = "demo_order_recovery_restart"
    _seed_submitting_intent(scoped, run_date, ticket, order_id=order_id)

    def fake_daily(date: str, strategy, output_root: Path) -> None:
        out = Path(output_root)
        write_json(out / "trade_tickets" / f"{date}.json", [ticket, next_ticket])
        write_json(
            out / "journal_pending" / f"{date}.json",
            [
                {**ticket, "journal_id": f"j_{ticket['ticket_id']}", "decision_status": "pending_manual_decision"},
                {**next_ticket, "journal_id": f"j_{next_ticket['ticket_id']}", "decision_status": "pending_manual_decision"},
            ],
        )

    class FakePreflight:
        def __init__(self, output_root: Path, **_kwargs) -> None:
            self.output_root = Path(output_root)

        def run(self, date: str) -> dict:
            payload = {"run_date": date, "ready_for_paper": True, "ready_for_live": False, "status": "pass"}
            write_json(self.output_root / "data_source_preflight" / f"{date}.json", [payload])
            return payload

    class RecoveringAdapter:
        name = "binance_demo"

        def __init__(self) -> None:
            self.calls = []

        def submit_order(self, request):
            self.calls.append(request)
            assert request.latest_price == 4525.5
            assert request.actual_size == 0.002
            store = OrderLifecycleStore(scoped)
            store.transition(run_date, order_id, "accepted", reason="recovered_from_venue")
            store.transition(run_date, order_id, "filled", reason="venue_fill_recovered", filled_quantity=0.002)
            store.transition(run_date, order_id, "protective_attached", reason="protective_recovered", protective_quantity=0.002)
            return PaperOrder(
                order_id=order_id,
                ticket_id=request.ticket["ticket_id"],
                status="filled",
                requested_price=float(request.latest_price),
                fill_price=float(request.latest_price),
                quantity=float(request.actual_size),
                filled_at="2026-06-30T00:00:00+00:00",
            )

    class FakePerformance:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def build(self, _date: str) -> dict:
            return {"realized_pnl": 0.0}

    class FakeEquity:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def build(self, _date: str, _performance: dict) -> dict:
            return {"current_equity": 10000.0, "current_drawdown_pct": 0.0}

    class FakeRunService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, _date: str) -> dict:
            return {"status": "pass", "kill_switch_active": False, "allow_paper_auto_approve": True}

    class FakeReconciliation(FakeRunService):
        def run(self, _date: str) -> dict:
            return {"status": "pass"}

    class FakeBook:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def build(self, _date: str) -> dict:
            return {"audit": {"status": "pass"}}

    class FakeEdge:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def build(self, _date: str) -> dict:
            return {"label": "watch"}

    adapter = RecoveringAdapter()
    monkeypatch.setattr("services.multi_strategy_runner.run_daily_pipeline", fake_daily)
    monkeypatch.setattr("services.multi_strategy_runner.DataSourcePreflight", FakePreflight)
    monkeypatch.setattr("services.multi_strategy_runner.PaperPerformanceAnalyzer", FakePerformance)
    monkeypatch.setattr("services.multi_strategy_runner.PaperEquityCurve", FakeEquity)
    monkeypatch.setattr("services.multi_strategy_runner.StrategyGuardrails", FakeRunService)
    monkeypatch.setattr("services.multi_strategy_runner.RiskMonitor", FakeRunService)
    monkeypatch.setattr("services.multi_strategy_runner.PaperReconciliation", FakeReconciliation)
    monkeypatch.setattr("services.multi_strategy_runner.StrategyBook", FakeBook)
    monkeypatch.setattr("services.multi_strategy_runner.EdgeJudgment", FakeEdge)
    monkeypatch.setattr(runner, "_demo_reconciliation_for", lambda *_args, **_kwargs: {"reconciled": True})
    monkeypatch.setattr(runner, "_broker_adapter_for", lambda *_args, **_kwargs: adapter)

    result = runner._run_one(run_date, strategy, paper_auto_approve=True)

    assert result["status"] == "ok"
    assert result["executed_ticket"] == ticket["ticket_id"]
    assert result["pending_tickets"] == 1
    assert "skipped new auto approval" in result["execution_error"]
    assert result["order_recovery_status"] == "recovered"
    assert result["order_recovery_count"] == 1
    assert len(adapter.calls) == 1
    # Autonomous: the new ticket is not left pending — it is auto-rejected this cycle
    # (a recovery already added exposure); the signal re-fires next cycle if still valid.
    assert load_json(scoped / "journal_pending" / f"{run_date}.json") == []
    assert result["auto_resolution"]["rejected"] == [next_ticket["ticket_id"]]
    decisions = load_json(scoped / "journal_decisions" / f"{run_date}.json")
    assert any(d["ticket_id"] == ticket["ticket_id"] for d in decisions)
    next_decision = next(d for d in decisions if d["ticket_id"] == next_ticket["ticket_id"])
    assert next_decision["decision_status"] == "rejected"
    recovered_decision = next(d for d in decisions if d["ticket_id"] == ticket["ticket_id"])
    assert recovered_decision["notes"] == "recovered from durable order intent after runner restart"
    assert OrderLifecycleStore(scoped).current(run_date, order_id)["state"] == "protective_attached"


def test_runner_recovers_from_durable_ticket_snapshot_when_ticket_file_is_missing(monkeypatch, tmp_path: Path):
    _enable_binance_demo(monkeypatch)
    root = tmp_path / "outputs"
    run_date = "2026-06-30"
    runner = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_ACTIVE_DEMO_LONG))
    strategy = runner.registry.get("gold_1m_macd")
    scoped = runner.strategy_root("gold_1m_macd")
    ticket = _demo_ticket(run_date, "snapshot")
    order_id = "demo_order_recovery_snapshot"
    _seed_submitting_intent(scoped, run_date, ticket, order_id=order_id)

    class RecoveringAdapter:
        name = "binance_demo"

        def __init__(self) -> None:
            self.calls = []

        def submit_order(self, request):
            self.calls.append(request)
            assert request.ticket["ticket_id"] == ticket["ticket_id"]
            store = OrderLifecycleStore(scoped)
            store.transition(run_date, order_id, "accepted", reason="recovered_from_venue")
            store.transition(run_date, order_id, "filled", reason="venue_fill_recovered", filled_quantity=0.002)
            store.transition(run_date, order_id, "protective_attached", reason="protective_recovered", protective_quantity=0.002)
            return PaperOrder(
                order_id=order_id,
                ticket_id=request.ticket["ticket_id"],
                status="filled",
                requested_price=float(request.latest_price),
                fill_price=float(request.latest_price),
                quantity=float(request.actual_size),
                filled_at="2026-06-30T00:00:00+00:00",
            )

    adapter = RecoveringAdapter()
    monkeypatch.setattr(runner, "_broker_adapter_for", lambda *_args, **_kwargs: adapter)

    report = runner._recover_demo_order_intents(strategy, scoped, run_date)

    assert report["status"] == "recovered"
    assert report["recovered_ticket_ids"] == [ticket["ticket_id"]]
    assert len(adapter.calls) == 1
    assert load_json(scoped / "trade_tickets" / f"{run_date}.json") == []
    decision = load_json(scoped / "journal_decisions" / f"{run_date}.json")[0]
    assert decision["ticket_id"] == ticket["ticket_id"]
    assert decision["paper_order"]["order_id"] == order_id


def test_runner_expires_accepted_demo_limit_after_ttl_and_cancels_broker_order(monkeypatch, tmp_path: Path):
    _enable_binance_demo(monkeypatch)
    root = tmp_path / "outputs"
    run_date = "2026-06-30"
    runner = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_ACTIVE_DEMO_LONG))
    strategy = runner.registry.get("gold_1m_macd")
    scoped = runner.strategy_root("gold_1m_macd")
    ticket = _demo_limit_ticket(run_date, "accepted_expired")
    order_id = "demo_order_accepted_expired"
    client_order_id = "client_accepted_expired"
    write_json(scoped / "trade_tickets" / f"{run_date}.json", [ticket])
    write_json(
        scoped / "clean_bars" / run_date / "GOLD_1m.json",
        [
            {
                "symbol": "GOLD",
                "timeframe": "1m",
                "timestamp": f"{run_date}T00:{minute:02d}:00+00:00",
                "open": 100.0,
                "high": 100.2,
                "low": 99.9,
                "close": 100.0,
                "volume": 1000,
                "provider": "mock",
            }
            for minute in range(11)
        ],
    )
    store = OrderLifecycleStore(scoped)
    store.write_intent(
        run_date,
        order_id=order_id,
        ticket_id=ticket["ticket_id"],
        idempotency_key=client_order_id,
        requested_quantity=1.0,
        requested_price=99.8,
        source="binance_usdm:demo",
        metadata={"symbol": "XAUUSDT", "ticket": ticket},
    )
    store.transition(run_date, order_id, "submitting", reason="submit_started")
    store.transition(run_date, order_id, "accepted", reason="entry_accepted", metadata={"client_order_id": client_order_id})

    class CancelingAdapter:
        name = "binance_demo"
        provider = "binance_usdm"
        capabilities = execution_capabilities_for(provider=provider, adapter_name=name)

        def __init__(self) -> None:
            self.cancel_calls = []

        def cancel_order(self, request) -> dict:
            assert request.asset == "GOLD"
            self.cancel_calls.append(
                {
                    "symbol": "XAUUSDT",
                    "orig_client_order_id": request.client_order_id,
                    "order_id": request.broker_order_id,
                }
            )
            return {"status": "CANCELED", "clientOrderId": request.client_order_id}

    adapter = CancelingAdapter()
    monkeypatch.setattr(runner, "_broker_adapter_for", lambda *_args, **_kwargs: adapter)

    report = runner._recover_demo_order_intents(strategy, scoped, run_date, reconciliation={"reconciled": True})
    lifecycle = OrderLifecycleStore(scoped).current(run_date, order_id)
    decisions = load_json(scoped / "journal_decisions" / f"{run_date}.json")

    assert report["status"] == "recovered"
    assert report["expired_entry_orders"]["expired_ticket_ids"] == [ticket["ticket_id"]]
    assert adapter.cancel_calls == [{"symbol": "XAUUSDT", "orig_client_order_id": client_order_id, "order_id": ""}]
    assert lifecycle["state"] == "expired"
    assert lifecycle["metadata"]["cancel_response"]["status"] == "CANCELED"
    assert decisions[0]["ticket_id"] == ticket["ticket_id"]
    assert decisions[0]["decision_status"] == "rejected"
    assert decisions[0]["paper_order"]["status"] == "expired"


def test_tiger_inherited_binance_cancel_method_is_blocked_before_network(monkeypatch, tmp_path: Path):
    from services.broker_adapter import LiveBrokerAdapter

    root = tmp_path / "outputs"
    run_date = "2026-06-30"
    runner = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_ACTIVE_DEMO_LONG))
    strategy = runner.registry.get("gold_1m_macd")
    scoped = runner.strategy_root("gold_1m_macd")
    ticket = _demo_limit_ticket(run_date, "tiger_expired")
    order_id = "tiger_order_accepted_expired"
    client_order_id = "tiger_client_accepted_expired"
    write_json(scoped / "trade_tickets" / f"{run_date}.json", [ticket])
    write_json(
        scoped / "clean_bars" / run_date / "GOLD_1m.json",
        [
            {
                "symbol": "GOLD",
                "timeframe": "1m",
                "timestamp": f"{run_date}T00:{minute:02d}:00+00:00",
                "open": 100.0,
                "high": 100.2,
                "low": 99.9,
                "close": 100.0,
                "volume": 1000,
                "provider": "mock",
            }
            for minute in range(11)
        ],
    )
    store = OrderLifecycleStore(scoped)
    store.write_intent(
        run_date,
        order_id=order_id,
        ticket_id=ticket["ticket_id"],
        idempotency_key=client_order_id,
        requested_quantity=1.0,
        requested_price=99.8,
        source="tiger_openapi:paper",
        metadata={"symbol": "MGC", "ticket": ticket},
    )
    store.transition(run_date, order_id, "submitting", reason="submit_started")
    lifecycle = store.transition(
        run_date,
        order_id,
        "accepted",
        reason="entry_accepted",
        metadata={"client_order_id": client_order_id},
    )
    adapter = LiveBrokerAdapter(
        scoped,
        True,
        {"provider": "tiger_openapi", "environment": "paper", "dry_run": True},
    )
    network_calls = []
    monkeypatch.setattr(
        adapter,
        "cancel_binance_order",
        lambda *args, **kwargs: network_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "_broker_adapter_for", lambda *_args, **_kwargs: adapter)

    report = runner._expire_demo_entry_orders(strategy, scoped, run_date, [lifecycle])

    assert report["status"] == "blocked"
    assert "does not support cancel_order" in report["block_reason"]
    assert network_calls == []
    assert OrderLifecycleStore(scoped).current(run_date, order_id)["state"] == "accepted"


def test_runner_reprobes_blocked_submitting_intent_after_connectivity_recovers(monkeypatch, tmp_path: Path):
    _enable_binance_demo(monkeypatch)
    root = tmp_path / "outputs"
    run_date = "2026-06-30"
    runner = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_ACTIVE_DEMO_LONG))
    strategy = runner.registry.get("gold_1m_macd")
    scoped = runner.strategy_root("gold_1m_macd")
    ticket = _demo_ticket(run_date, "blocked_recovered")
    order_id = "demo_order_recovery_blocked"
    _seed_demo_pending(scoped, run_date, ticket)
    store = _seed_submitting_intent(scoped, run_date, ticket, order_id=order_id)
    blockers = store.watchdog_tick(run_date, max_cycles=0)
    assert blockers
    assert OrderLifecycleStore(scoped).current(run_date, order_id)["blocked"] is True

    class RecoveringAdapter:
        name = "binance_demo"

        def __init__(self) -> None:
            self.calls = []

        def submit_order(self, request):
            self.calls.append(request)
            store = OrderLifecycleStore(scoped)
            store.transition(run_date, order_id, "accepted", reason="connectivity_restored_recovered_from_venue")
            store.transition(run_date, order_id, "filled", reason="venue_fill_recovered", filled_quantity=0.002)
            store.transition(run_date, order_id, "protective_attached", reason="protective_recovered", protective_quantity=0.002)
            return PaperOrder(
                order_id=order_id,
                ticket_id=request.ticket["ticket_id"],
                status="filled",
                requested_price=float(request.latest_price),
                fill_price=float(request.latest_price),
                quantity=float(request.actual_size),
                filled_at="2026-06-30T00:00:00+00:00",
            )

    adapter = RecoveringAdapter()
    monkeypatch.setattr(runner, "_broker_adapter_for", lambda *_args, **_kwargs: adapter)

    report = runner._recover_demo_order_intents(strategy, scoped, run_date)
    lifecycle = OrderLifecycleStore(scoped).current(run_date, order_id)

    assert report["status"] == "recovered"
    assert report["recovered_ticket_ids"] == [ticket["ticket_id"]]
    assert len(adapter.calls) == 1
    assert lifecycle["state"] == "protective_attached"
    assert lifecycle["blocked"] is False
    assert lifecycle["blocker"] == {}
    assert lifecycle["resolved_blockers"][0]["blocker"]["source"] == "order_lifecycle_watchdog"


def test_runner_recovers_filled_intent_with_missing_protective_orders(monkeypatch, tmp_path: Path):
    _enable_binance_demo(monkeypatch)
    root = tmp_path / "outputs"
    run_date = "2026-06-30"
    runner = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_ACTIVE_DEMO_LONG))
    strategy = runner.registry.get("gold_1m_macd")
    scoped = runner.strategy_root("gold_1m_macd")
    ticket = _demo_ticket(run_date, "filled_naked")
    order_id = "demo_order_filled_without_protective"
    store = OrderLifecycleStore(scoped)
    store.write_intent(
        run_date,
        order_id=order_id,
        ticket_id=ticket["ticket_id"],
        idempotency_key=order_id[:36],
        requested_quantity=0.002,
        requested_price=4525.5,
        source="binance_usdm:demo",
        metadata={"symbol": "XAUUSDT", "ticket": ticket},
    )
    store.transition(run_date, order_id, "submitting", reason="submit_started")
    store.transition(run_date, order_id, "accepted", reason="entry_accepted")
    store.transition(run_date, order_id, "filled", reason="crash_after_fill_before_protective", filled_quantity=0.002)
    reconciliation = {
        "suspected_naked_position": True,
        "naked_position_risks": [
            {
                "exchange_symbol": "XAUUSDT",
                "exchange_qty": 0.002,
                "reason_code": "naked_position_suspected",
                "missing_protective_order": True,
            }
        ],
        "exchange_positions": [{"symbol": "XAUUSDT", "position_amt": 0.002, "entry_price": 4525.5}],
    }

    class ProtectiveRecoveryAdapter:
        name = "binance_demo"
        provider = "binance_usdm"
        capabilities = execution_capabilities_for(provider=provider, adapter_name=name)

        def __init__(self) -> None:
            self.calls = []

        def recover_protective_orders(self, request):
            self.calls.append(
                (
                    request.run_date,
                    request.lifecycle_record,
                    request.exchange_position,
                    request.source,
                )
            )
            OrderLifecycleStore(scoped).transition(request.run_date, order_id, "protective_attached", reason="recovered_missing_protective", protective_quantity=0.002)
            return {
                "status": "recovered",
                "action": "attach_missing_protective_orders",
                "order_id": order_id,
                "ticket_id": ticket["ticket_id"],
                "protective_status": "pass",
            }

    adapter = ProtectiveRecoveryAdapter()
    monkeypatch.setattr(runner, "_broker_adapter_for", lambda *_args, **_kwargs: adapter)

    report = runner._recover_demo_order_intents(strategy, scoped, run_date, reconciliation=reconciliation)
    lifecycle = OrderLifecycleStore(scoped).current(run_date, order_id)

    assert report["status"] == "recovered"
    assert report["reconciliation_refresh_required"] is True
    assert report["blocks_new_orders"] is False
    assert report["recovered_ticket_ids"] == [ticket["ticket_id"]]
    assert adapter.calls[0][3] == "order_recovery_missing_protective"
    assert lifecycle["state"] == "protective_attached"
    assert lifecycle["protective_quantity"] == 0.002


def test_runner_ambiguous_demo_recovery_blocks_new_orders_until_watchdog_blocks(monkeypatch, tmp_path: Path):
    _enable_binance_demo(monkeypatch)
    root = tmp_path / "outputs"
    run_date = "2026-06-30"
    runner = MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_ACTIVE_DEMO_LONG))
    strategy = runner.registry.get("gold_1m_macd")
    scoped = runner.strategy_root("gold_1m_macd")
    ticket = _demo_ticket(run_date, "ambiguous")
    order_id = "demo_order_recovery_ambiguous"
    _seed_demo_pending(scoped, run_date, ticket)
    _seed_submitting_intent(scoped, run_date, ticket, order_id=order_id)

    class AmbiguousAdapter:
        name = "binance_demo"

        def __init__(self) -> None:
            self.calls = []

        def submit_order(self, request):
            self.calls.append(request)
            return PaperOrder(
                order_id=order_id,
                ticket_id=request.ticket["ticket_id"],
                status="submitted_to_binance",
                requested_price=float(request.latest_price),
                fill_price=None,
                quantity=float(request.actual_size),
                filled_at="",
                rejection_reason="Binance entry recovery ambiguous; no duplicate order submitted",
            )

    adapter = AmbiguousAdapter()
    monkeypatch.setattr(runner, "_broker_adapter_for", lambda *_args, **_kwargs: adapter)

    first = runner._recover_demo_order_intents(strategy, scoped, run_date)
    second = runner._recover_demo_order_intents(strategy, scoped, run_date)
    third = runner._recover_demo_order_intents(strategy, scoped, run_date)
    fourth = runner._recover_demo_order_intents(strategy, scoped, run_date)

    assert first["status"] == "blocked"
    assert first["blocks_new_orders"] is True
    assert "ambiguous" in first["block_reason"]
    assert second["blocks_new_orders"] is True
    assert third["blocks_new_orders"] is True
    assert fourth["status"] == "blocked"
    assert "ambiguous" in fourth["block_reason"]
    assert len(adapter.calls) == 4
    assert load_json(scoped / "journal_pending" / f"{run_date}.json")[0]["ticket_id"] == ticket["ticket_id"]
    assert load_json(scoped / "journal_decisions" / f"{run_date}.json") == []
    lifecycle = OrderLifecycleStore(scoped).current(run_date, order_id)
    assert lifecycle["state"] == "submitting"
    assert lifecycle["blocked"] is True
    assert lifecycle["blocker"]["source"] == "order_lifecycle_watchdog"
    recovery_report = load_json(scoped / "order_recovery" / "current.json")[0]
    assert recovery_report["blocks_new_orders"] is True
    assert recovery_report["errors"][0]["order_id"] == order_id
    assert "ambiguous" in recovery_report["block_reason"]


def test_no_cross_contamination_or_global_leak(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    _offline_env(monkeypatch, root, tmp_path / "market_data.db")
    run_date = "2026-05-10"

    MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_DIVERGENT)).run(run_date, paper_auto_approve=True)

    # Per-strategy trading artifacts must NOT appear at the global root — only
    # in their own namespace. (Catches any service that bypassed output_root.)
    assert not (root / "signals" / f"{run_date}.json").exists()
    assert not (root / "trade_tickets" / f"{run_date}.json").exists()
    assert not (root / "paper_trades" / "current.json").exists()

    # The long strategy traded; the watch strategy did not — divergent accounts.
    long_tickets = json.loads((root / "strategies" / "strat_long" / "trade_tickets" / f"{run_date}.json").read_text())
    watch_tickets = json.loads((root / "strategies" / "strat_watch" / "trade_tickets" / f"{run_date}.json").read_text())
    assert len(long_tickets) >= 1
    assert len(watch_tickets) == 0


def test_each_namespace_independently_reconciles(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    _offline_env(monkeypatch, root, tmp_path / "market_data.db")
    run_date = "2026-05-10"

    MultiStrategyRunner(output_root=root, registry=StrategyRegistry(_DIVERGENT)).run(run_date, paper_auto_approve=True)

    # The accounting_invariant (equity == starting + realized + unrealized) must
    # hold INDEPENDENTLY in each strategy's namespace.
    for strategy_id in ("strat_long", "strat_watch"):
        recon = json.loads((root / "strategies" / strategy_id / "paper_reconciliation" / f"{run_date}.json").read_text())[0]
        invariant = next((c for c in recon["checks"] if c["name"] == "accounting_invariant"), None)
        assert invariant is not None, f"{strategy_id} missing accounting_invariant"
        assert invariant["status"] == "pass", f"{strategy_id} equity did not reconcile: {invariant['evidence']}"
