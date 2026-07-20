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
from services.dualtrack_execution_adapter import ExecutionEngineAdapter
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
    "services/dualtrack_execution_contract.py",
    "services/accounting_projection.py",
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
    "Backtest / replay": (20, 20, 15, 15, 70),
    "Live execution / broker": (25, 20, 20, 20, 85),
    "Risk / accounting / reconciliation": (25, 20, 20, 25, 90),
    "Dashboard / read model": (25, 25, 20, 25, 95),
}

APPROVED_INLINE_PROVIDER_BRANCHES = {
    "services/accounting_projection.py": {
        "binance_usdm",
        "binance_usdm_futures",
        "tiger_openapi",
    },
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


class GenericStrategyAnalysisPort:
    def generate(self, asset, candles, events=None, run_date="", factor_context=None):
        return None


class GenericStrategyProposalPort:
    def propose(self, request):
        return {"cycle_id": request.cycle_id}


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
    assert isinstance(GenericStrategyAnalysisPort(), StrategyAnalysisPort)
    assert isinstance(GenericStrategyProposalPort(), StrategyProposalPort)
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
    assert overall == 87
    assert "**Overall architecture progress: 87%**" in text


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


def test_audit_names_every_known_non_hexagonal_seam() -> None:
    text = AUDIT.read_text(encoding="utf-8")
    backlog = text.split("## Shortest remaining architecture backlog", 1)[1]
    for seam in (
        "Backtest Port",
        "dualtrack_execution_adapter.py",
        "accounting_projection.py",
        "LiveBrokerAdapter",
    ):
        assert seam in backlog
    assert "DualTrackMachinePlanner" not in backlog
    assert "Strategy._base_engine" not in backlog
    assert "strategy-proposal-request-v1" in text
    assert "market_data_contract_mode=shadow" in text
