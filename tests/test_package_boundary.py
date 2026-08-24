from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

import trading_strategy
from trading_strategy.precision import normalize_execution_command


PACKAGE_ROOT = Path(trading_strategy.__file__).parent
STDLIB_ROOTS = set(sys.stdlib_module_names)


def test_package_imports_only_stdlib_or_its_own_modules() -> None:
    violations: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: str | None = None
            if isinstance(node, ast.Import):
                imported = node.names[0].name
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported = node.module
            if not imported:
                continue
            root = imported.split(".", 1)[0]
            if root not in STDLIB_ROOTS and not imported.startswith("trading_strategy"):
                violations.append(f"{path.name}: {imported}")
    assert violations == []


def test_no_broker_native_module_or_implicit_second_algorithm_is_exposed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("trading_strategy.broker")

    assert trading_strategy.build_dca_preview.__module__ == "trading_strategy.dca_plan"
    assert trading_strategy.build_grid_preview.__module__ == "trading_strategy.grid_sizing"
    assert not hasattr(trading_strategy, "build_external_dca_preview")
    assert not hasattr(trading_strategy, "build_broker_grid_preview")


def test_broker_native_object_injection_is_rejected_at_the_pure_boundary() -> None:
    class NativeBrokerOrder:
        price = 100.0
        quantity = 1.0

    with pytest.raises((TypeError, ValueError)):
        normalize_execution_command(
            NativeBrokerOrder(),
            {"execution_contract": {"price_increment": "0.01", "quantity_increment": "0.001"}},
        )
