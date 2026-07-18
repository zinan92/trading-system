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


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "docs" / "trading-system-ports-and-adapters-audit-2026-07-18.md"

CONTRACT_KERNELS = (
    "schemas/market_data.py",
    "schemas/accounting.py",
    "schemas/risk.py",
    "services/datafeed_market_mapper.py",
    "services/market_data_envelope_projection.py",
    "services/strategy_plan_execution.py",
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
    "services.dualtrack_human",
    "services.dualtrack_nautilus_",
    "services.dualtrack_shadow_execution_adapter",
    "services.market_store",
)

EXPECTED_SCORE_ROWS = {
    "Data download": (25, 25, 25, 15, 90),
    "Data cleaning / quality": (25, 25, 20, 15, 85),
    "Analysis / strategy": (20, 15, 10, 20, 65),
    "Backtest / replay": (20, 20, 15, 25, 80),
    "Live execution / broker": (25, 20, 20, 20, 85),
    "Risk / accounting / reconciliation": (25, 20, 20, 25, 90),
    "Dashboard / read model": (25, 25, 20, 25, 95),
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_contract_kernels_do_not_import_concrete_adapters_or_network_clients() -> None:
    violations: list[str] = []
    for relative in CONTRACT_KERNELS:
        path = ROOT / relative
        assert path.exists(), relative
        for module in sorted(_imported_modules(path)):
            if any(module == prefix or module.startswith(prefix) for prefix in FORBIDDEN_KERNEL_IMPORTS):
                violations.append(f"{relative} imports {module}")
    assert violations == []


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


def test_provider_free_plugins_structurally_fit_the_public_ports() -> None:
    assert isinstance(GenericMarketPort(), TrustedMarketDataReadPort)
    assert isinstance(GenericExecutionPort(), ExecutionEngineAdapter)
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
    assert overall == 84
    assert "**Overall architecture progress: 84%**" in text


def test_audit_names_every_known_non_hexagonal_seam() -> None:
    text = AUDIT.read_text(encoding="utf-8")
    for seam in (
        "Strategy._base_engine",
        "Backtest Port",
        "dualtrack_execution_adapter.py",
        "accounting_projection.py",
        "LiveBrokerAdapter",
        "market_data_contract_mode=shadow",
    ):
        assert seam in text
