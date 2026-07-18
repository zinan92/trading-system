from __future__ import annotations

import ast
from pathlib import Path

from services.broker_port import (
    BrokerCapabilities,
    BrokerCapability,
    BrokerExecutionPort,
    BrokerPortDescriptor,
    BrokerReconciliationPort,
)
from services.backtest_port import (
    HistoricalStrategyBacktestPort,
    SignalBacktestPort,
    StrategyShadowReplayPort,
)
from services.accounting_projection_port import BrokerAccountingProjectionPort
from services.execution_engine_port import ExecutionEngineAdapter
from services.market_data_access import TrustedMarketDataReadPort
from services.risk_port import RiskDecisionPort
from services.strategy_analysis_port import StrategyAnalysisPort
from services.strategy_proposal_port import StrategyProposalPort


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "docs" / "trading-system-ports-and-adapters-audit-2026-07-18.md"

CONTRACT_KERNELS = (
    "schemas/market_data.py",
    "schemas/accounting.py",
    "schemas/risk.py",
    "services/datafeed_market_mapper.py",
    "services/market_data_envelope_projection.py",
    "services/strategy_plan_execution.py",
    "services/strategy_analysis_port.py",
    "services/strategy_plugin_registry.py",
    "services/strategy_proposal_port.py",
    "services/strategy_proposal_registry.py",
    "services/backtest_port.py",
    "services/backtest_plugin_registry.py",
    "services/backtest_service.py",
    "services/dualtrack_execution_contract.py",
    "services/execution_engine_port.py",
    "services/execution_engine_plugin_registry.py",
    "services/accounting_projection_core.py",
    "services/accounting_projection_port.py",
    "services/accounting_projection_registry.py",
    "services/accounting_broker_common.py",
    "services/risk_port.py",
    "services/broker_port.py",
    "services/trading_system_read_model.py",
)

FORBIDDEN_KERNEL_IMPORTS = (
    "aiohttp",
    "httpx",
    "requests",
    "sqlite3",
    "urllib",
    "websocket",
    "services.binance_",
    "services.tiger_",
    "services.oanda_",
    "services.mt5_",
    "services.ib_",
    "services.ibkr_",
    "services.broker_adapter",
    "services.dualtrack_human",
    "services.dualtrack_nautilus_",
    "services.dualtrack_shadow_execution_adapter",
    "services.market_store",
    "services.signal_engine",
    "services.chan_signal_engine",
    "services.macd_signal_engine",
    "services.technical_rule_signal_engine",
    "services.codex_newsletter_strategy_proposal",
)

EXPECTED_SCORE_ROWS = {
    "Data download": (25, 25, 25, 15, 90),
    "Data cleaning / quality": (25, 25, 20, 15, 85),
    "Analysis / strategy": (25, 20, 25, 25, 95),
    "Backtest / replay": (25, 20, 25, 20, 90),
    "Live execution / broker": (25, 20, 25, 20, 90),
    "Risk / accounting / reconciliation": (25, 20, 20, 25, 90),
    "Dashboard / read model": (25, 25, 20, 25, 95),
}

APPROVED_INLINE_PROVIDER_BRANCHES = {
    "services/broker_port.py": {"binance_usdm"},
}
PROVIDER_SUBSCRIPT_DISPATCH = "<provider-subscript-dispatch>"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _references_provider(node: ast.AST) -> bool:
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and "provider" in item.id.lower():
            return True
        if isinstance(item, ast.Attribute) and "provider" in item.attr.lower():
            return True
        if (
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr == "get"
            and item.args
            and isinstance(item.args[0], ast.Constant)
            and isinstance(item.args[0].value, str)
            and "provider" in item.args[0].value.lower()
        ):
            return True
    return False


def _string_literals(*nodes: ast.AST) -> set[str]:
    return {
        str(item.value)
        for node in nodes
        for item in ast.walk(node)
        if isinstance(item, ast.Constant)
        and isinstance(item.value, str)
        and item.value.lower() != "provider"
    }


def _inline_provider_branch_literals(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            compared = (node.left, *node.comparators)
            if any(_references_provider(value) for value in compared):
                found.update(_string_literals(*compared))
        elif isinstance(node, ast.Match) and _references_provider(node.subject):
            found.update(_string_literals(*node.cases))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"startswith", "endswith"}
            and _references_provider(node.func.value)
        ):
            found.update(_string_literals(*node.args))
        elif isinstance(node, ast.Subscript) and _references_provider(node.slice):
            found.add(PROVIDER_SUBSCRIPT_DISPATCH)
    return found


def test_contract_kernels_do_not_import_concrete_adapters_or_network_clients() -> None:
    violations: list[str] = []
    for relative in CONTRACT_KERNELS:
        path = ROOT / relative
        assert path.exists(), relative
        for module in sorted(_imported_modules(path)):
            if any(module == prefix or module.startswith(prefix) for prefix in FORBIDDEN_KERNEL_IMPORTS):
                violations.append(f"{relative} imports {module}")
    assert violations == []


def test_existing_inline_provider_branches_are_frozen_to_named_extraction_debt() -> None:
    observed = {
        relative: _inline_provider_branch_literals(ROOT / relative)
        for relative in CONTRACT_KERNELS
        if _inline_provider_branch_literals(ROOT / relative)
    }
    assert observed == APPROVED_INLINE_PROVIDER_BRANCHES


def test_fitness_scanner_catches_alias_imports_and_common_provider_dispatch_forms(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "provider_dispatch.py"
    sample.write_text(
        """
from services import oanda_adapter

def select(provider, source, handlers):
    if source.get("provider") == "mt5":
        return None
    if provider.startswith("binance_"):
        return None
    match provider:
        case "tiger_openapi":
            return None
    return handlers[provider]
""",
        encoding="utf-8",
    )

    assert "services.oanda_adapter" in _imported_modules(sample)
    assert _inline_provider_branch_literals(sample) == {
        "mt5",
        "binance_",
        "tiger_openapi",
        PROVIDER_SUBSCRIPT_DISPATCH,
    }


class GenericMarketPort:
    def load_envelope(self, symbol, timeframe, limit, *, start=None, end=None):
        return None


class GenericExecutionPort:
    name = "generic_execution"

    def submit_order(self, command):
        return {}

    def cancel_orders(self, cycle_id, *, order_ids=None, strategy_plan_id=None, ts=None, reason=""):
        return {}

    def process_market_event(self, event):
        return {}

    def snapshot(self, cycle_id, *, mark_price=None, mark_fresh=False, mark_source=""):
        return {}

    def reconcile(self, cycle_id):
        return {}


class GenericBrokerAccountingPort:
    name = "generic_accounting"
    source_names = ("generic_broker",)

    def project(self, source):
        return source


class GenericStrategyAnalysisPort:
    def generate(self, asset, candles, events=None, run_date="", factor_context=None):
        return None


class GenericStrategyProposalPort:
    def propose(self, request):
        return {"cycle_id": request.cycle_id}


class GenericSignalBacktestPort:
    def evaluate(self, request):
        return request


class GenericHistoricalBacktestPort:
    def run(self, request):
        return {"request": request}


class GenericStrategyShadowReplayPort:
    def replay(self, scenario):
        return {"scenario": scenario}


class GenericRiskPort:
    name = "generic_risk"

    def evaluate(self, request):
        return None


class GenericBrokerPort:
    name = "generic_broker"
    provider = "generic_venue"
    capabilities = BrokerCapabilities(
        frozenset({BrokerCapability.PREFLIGHT, BrokerCapability.SUBMIT_ORDER})
    )
    descriptor = BrokerPortDescriptor(
        adapter_name=name,
        provider=provider,
        environment="paper",
        capabilities=capabilities.names,
        credential_env_names=(),
    )

    def preflight(self):
        return {"ready": True}

    def submit_order(self, request):
        return None


class GenericReconciliationPort:
    def run(self, run_date):
        return {"status": "pass"}


def test_provider_free_fakes_expose_the_public_port_shapes() -> None:
    assert isinstance(GenericMarketPort(), TrustedMarketDataReadPort)
    assert isinstance(GenericExecutionPort(), ExecutionEngineAdapter)
    assert isinstance(GenericBrokerAccountingPort(), BrokerAccountingProjectionPort)
    assert isinstance(GenericStrategyAnalysisPort(), StrategyAnalysisPort)
    assert isinstance(GenericStrategyProposalPort(), StrategyProposalPort)
    assert isinstance(GenericSignalBacktestPort(), SignalBacktestPort)
    assert isinstance(GenericHistoricalBacktestPort(), HistoricalStrategyBacktestPort)
    assert isinstance(GenericStrategyShadowReplayPort(), StrategyShadowReplayPort)
    assert isinstance(GenericRiskPort(), RiskDecisionPort)
    assert isinstance(GenericBrokerPort(), BrokerExecutionPort)
    assert isinstance(GenericReconciliationPort(), BrokerReconciliationPort)


def test_architecture_progress_bar_is_reproducible_from_visible_scores() -> None:
    text = AUDIT.read_text(encoding="utf-8")
    observed: dict[str, tuple[int, int, int, int, int]] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 6 or cells[0] not in EXPECTED_SCORE_ROWS:
            continue
        observed[cells[0]] = tuple(int(value) for value in cells[1:])

    assert observed == EXPECTED_SCORE_ROWS
    for values in observed.values():
        assert sum(values[:4]) == values[4]
    overall = round(sum(values[4] for values in observed.values()) / len(observed))
    assert overall == 91
    assert "**Overall architecture progress: 91%**" in text


def test_strategy_selection_stays_in_the_explicit_plugin_composition_root() -> None:
    application_paths = (
        ROOT / "services" / "strategy_registry.py",
        ROOT / "pipelines" / "daily.py",
    )
    concrete_modules = {
        "services.signal_engine",
        "services.chan_signal_engine",
        "services.macd_signal_engine",
        "services.technical_rule_signal_engine",
    }
    for path in application_paths:
        source = path.read_text(encoding="utf-8")
        assert "_base_engine" not in source
        assert not (_imported_modules(path) & concrete_modules)

    composition = (ROOT / "services" / "strategy_plugin_composition.py").read_text(encoding="utf-8")
    for module in concrete_modules:
        assert module in composition


def test_strategy_proposal_selection_stays_in_the_explicit_composition_root() -> None:
    application_paths = (
        ROOT / "services" / "dualtrack_machine_plan.py",
        ROOT / "pipelines" / "dualtrack_cycle_runner.py",
    )
    concrete_module = "services.codex_newsletter_strategy_proposal"
    for path in application_paths:
        source = path.read_text(encoding="utf-8")
        assert concrete_module not in _imported_modules(path)
        assert "CodexNewsletterStrategyProposal" not in source
        assert "def _codex_decision" not in source
        assert "def _prompt" not in source

    composition = (ROOT / "services" / "strategy_proposal_composition.py").read_text(encoding="utf-8")
    assert concrete_module in composition


def test_backtest_selection_stays_in_one_explicit_composition_root() -> None:
    application_paths = (
        ROOT / "pipelines" / "daily.py",
        ROOT / "pipelines" / "backtest_strategies.py",
        ROOT / "pipelines" / "strategy_shadow_replay.py",
        ROOT / "services" / "strategy_experiment_queue.py",
    )
    concrete_modules = {
        "services.backtest_client",
        "services.local_backtester",
        "services.strategy_backtester",
        "services.strategy_shadow_nautilus",
        "services.backtest_signal_adapters",
        "services.backtest_historical_adapter",
    }
    for path in application_paths:
        source = path.read_text(encoding="utf-8")
        assert not (_imported_modules(path) & concrete_modules)
        assert "BacktestClient" not in source
        assert "local_backtest_enabled" not in source
        assert "NautilusStrategyShadowReplay" not in source

    composition = (ROOT / "services" / "backtest_plugin_composition.py").read_text(
        encoding="utf-8"
    )
    for module in {
        "services.backtest_signal_adapters",
        "services.backtest_historical_adapter",
        "services.strategy_shadow_nautilus",
    }:
        assert module in composition
    assert "services.local_backtester" in (
        ROOT / "services" / "backtest_signal_adapters.py"
    ).read_text(encoding="utf-8")
    assert "services.strategy_backtester" in (
        ROOT / "services" / "backtest_historical_adapter.py"
    ).read_text(encoding="utf-8")


def test_execution_selection_stays_in_one_explicit_composition_root() -> None:
    application_paths = (
        ROOT / "pipelines" / "dualtrack_cycle_runner.py",
        ROOT / "pipelines" / "dashboard_server.py",
        ROOT / "services" / "strategy_control_plane.py",
        ROOT / "pipelines" / "dualtrack_nautilus_cutover_apply.py",
        ROOT / "pipelines" / "dualtrack_nautilus_attended_cutover.py",
    )
    concrete_modules = {
        "services.legacy_paper_execution_adapter",
        "services.dualtrack_nautilus_execution_adapter",
        "services.dualtrack_shadow_execution_adapter",
        "services.dualtrack_execution_adapter",
    }
    for path in application_paths:
        assert not (_imported_modules(path) & concrete_modules), path
        assert "services.execution_plugin_composition" in _imported_modules(path), path

    composition = ROOT / "services" / "execution_plugin_composition.py"
    imported = _imported_modules(composition)
    assert {
        "services.legacy_paper_execution_adapter",
        "services.dualtrack_nautilus_execution_adapter",
        "services.dualtrack_shadow_execution_adapter",
    }.issubset(imported)


def test_execution_compatibility_module_is_a_reexport_only_facade() -> None:
    path = ROOT / "services" / "dualtrack_execution_adapter.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )
    assert "services.execution_plugin_composition" in _imported_modules(path)
    assert "services.legacy_paper_execution_adapter" in _imported_modules(path)


def test_accounting_selection_stays_in_one_explicit_composition_root() -> None:
    execution_consumers = (
        ROOT / "pipelines" / "dashboard_server.py",
        ROOT / "services" / "risk_port.py",
        ROOT / "services" / "execution_conformance.py",
        ROOT / "services" / "production_accounting.py",
        ROOT / "services" / "strategy_shadow.py",
    )
    broker_consumers = (
        ROOT / "services" / "live_reconciliation.py",
        ROOT / "services" / "tiger_openapi_account_sync.py",
    )
    concrete_modules = {
        "services.accounting_binance_adapter",
        "services.accounting_tiger_adapter",
    }
    for path in execution_consumers:
        imports = _imported_modules(path)
        assert "services.accounting_projection_core" in imports, path
        assert "services.accounting_projection" not in imports, path
        assert "services.accounting_projection_composition" not in imports, path
        assert not (imports & concrete_modules), path
    for path in broker_consumers:
        imports = _imported_modules(path)
        assert "services.accounting_projection_composition" in imports, path
        assert "services.accounting_projection" not in imports, path
        assert not (imports & concrete_modules), path

    composition = ROOT / "services" / "accounting_projection_composition.py"
    imported = _imported_modules(composition)
    assert concrete_modules.issubset(imported)


def test_accounting_compatibility_module_is_a_reexport_only_facade() -> None:
    path = ROOT / "services" / "accounting_projection.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )
    assert "services.accounting_projection_core" in _imported_modules(path)
    assert "services.accounting_projection_composition" in _imported_modules(path)


def test_production_backtest_plugins_are_explicit_and_non_synthetic() -> None:
    pipeline = (ROOT / "configs" / "pipeline.yaml").read_text(encoding="utf-8")
    dualtrack = (ROOT / "configs" / "dualtrack.yaml").read_text(encoding="utf-8")

    assert '"signal": "local_signal"' in pipeline
    assert '"historical_strategy": "event_driven_strategy"' in pipeline
    assert '"strategy_shadow": "nautilus_strategy_shadow"' in dualtrack
    assert '"signal": "synthetic_signal_context"' not in pipeline


def test_audit_names_every_known_non_hexagonal_seam() -> None:
    text = AUDIT.read_text(encoding="utf-8")
    backlog = text.split("## Shortest remaining architecture backlog", 1)[1]
    for seam in (
        "accounting_projection.py",
        "LiveBrokerAdapter",
        "Market Envelope V2",
        "BacktestClient",
    ):
        assert seam in backlog
    assert "dualtrack_execution_adapter.py" not in backlog
    assert "DualTrackMachinePlanner" not in backlog
    assert "Strategy._base_engine" not in backlog
    assert "Backtest composition" not in backlog
    assert "strategy-proposal-request-v1" in text
    assert "market_data_contract_mode=shadow" in text
