from __future__ import annotations

from pathlib import Path

import pytest

from services.dualtrack_execution_adapter import (
    NAUTILUS_PAPER_GATE_OVERRIDE_ACKNOWLEDGEMENT,
    build_configured_execution_engine_adapter,
    execution_engine_selection,
)


def test_execution_selection_defaults_to_legacy_with_nautilus_shadow() -> None:
    selection = execution_engine_selection({}, environ={})

    assert selection == {
        "authoritative": "legacy_paper",
        "shadow": "nautilus_paper",
        "paper_only": True,
        "real_money_eligible": False,
        "attended_approval": False,
        "nautilus_python": "",
        "shadow_gate_override": False,
    }


def test_config_cannot_self_approve_nautilus_cutover() -> None:
    config = {
        "execution_engine": {
            "authoritative": "nautilus_paper",
            "shadow": "nautilus_paper",
            "allow_paper_switch": True,
            "nautilus_python": "/untrusted/config/path",
        }
    }

    with pytest.raises(RuntimeError, match="attended approval"):
        execution_engine_selection(config, environ={})


def test_nautilus_cutover_requires_exact_approval_and_isolated_runtime_env() -> None:
    config = {"execution_engine": {"authoritative": "nautilus_paper"}}

    with pytest.raises(RuntimeError, match="isolated runtime path"):
        execution_engine_selection(
            config,
            environ={"TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED": "1"},
        )

    selection = execution_engine_selection(
        config,
        environ={
            "TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED": "1",
            "TRADING_ORCHESTRATOR_NAUTILUS_PYTHON": "/isolated/nautilus/bin/python",
        },
    )

    assert selection["authoritative"] == "nautilus_paper"
    assert selection["attended_approval"] is True
    assert selection["nautilus_python"] == "/isolated/nautilus/bin/python"
    assert selection["real_money_eligible"] is False


def test_accelerated_gate_override_requires_exact_paper_only_acknowledgement() -> None:
    config = {"execution_engine": {"authoritative": "nautilus_paper", "real_money_eligible": False}}
    base = {
        "TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED": "1",
        "TRADING_ORCHESTRATOR_NAUTILUS_PYTHON": "/isolated/nautilus/bin/python",
    }

    assert execution_engine_selection(config, environ={
        **base,
        "TRADING_ORCHESTRATOR_NAUTILUS_PAPER_GATE_OVERRIDE": "yes",
    })["shadow_gate_override"] is False
    assert execution_engine_selection(config, environ={
        **base,
        "TRADING_ORCHESTRATOR_NAUTILUS_PAPER_GATE_OVERRIDE": NAUTILUS_PAPER_GATE_OVERRIDE_ACKNOWLEDGEMENT,
    })["shadow_gate_override"] is True


def test_execution_selection_rejects_unknown_or_real_money_engine() -> None:
    with pytest.raises(ValueError, match="unsupported authoritative execution engine"):
        execution_engine_selection({"execution_engine": {"authoritative": "binance_live"}}, environ={})

    with pytest.raises(ValueError, match="paper-only"):
        execution_engine_selection(
            {"execution_engine": {"authoritative": "legacy_paper", "real_money_eligible": True}},
            environ={},
        )


def test_configured_factory_preserves_legacy_authority_without_cutover_gate(tmp_path: Path) -> None:
    adapter = build_configured_execution_engine_adapter(
        tmp_path / "outputs",
        config={"execution_engine": {"authoritative": "legacy_paper"}},
        environ={},
    )

    assert adapter.name == "legacy_paper"


def test_production_entrypoints_use_only_configured_execution_factory() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "pipelines" / "dualtrack_cycle_runner.py",
        root / "pipelines" / "dashboard_server.py",
        root / "services" / "strategy_control_plane.py",
        root / "pipelines" / "dualtrack_nautilus_cutover_apply.py",
    )

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "build_configured_execution_engine_adapter" in source, path
        assert "services.execution_plugin_composition" in source, path
        assert "services.dualtrack_execution_adapter" not in source, path
        assert "build_execution_engine_adapter(" not in source, path
