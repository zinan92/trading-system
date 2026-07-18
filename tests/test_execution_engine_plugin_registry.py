from __future__ import annotations

from pathlib import Path

import pytest

from services.execution_engine_plugin_registry import ExecutionEnginePluginRegistry
from services.execution_engine_port import (
    EXECUTION_ENGINE_CAPABILITIES,
    ExecutionEngineBuildRequest,
    ExecutionEnginePluginDescriptor,
)
from services.execution_plugin_composition import (
    EXECUTION_ENGINE_PLUGINS,
    build_default_execution_engine_registry,
    compose_configured_execution_engine,
    execution_engine_selection,
)
from services.dualtrack_nautilus_parity_contract import PLATFORM_CODE_PATHS


class ProviderFreePaperEngine:
    name = "provider_free_paper"

    def submit_order(self, command):
        return {"status": "accepted", "command": dict(command)}

    def cancel_orders(self, cycle_id, *, order_ids=None, strategy_plan_id=None, ts=None, reason=""):
        return {"status": "cancelled", "cycle_id": cycle_id}

    def process_market_event(self, event):
        return {"status": "ok", "event": dict(event)}

    def snapshot(self, cycle_id, *, mark_price=None, mark_fresh=False, mark_source=""):
        return {"engine": self.name, "cycle_id": cycle_id, "orders": [], "positions": []}

    def reconcile(self, cycle_id):
        return {"status": "ok", "cycle_id": cycle_id, "issues": []}


def _provider_free_registry() -> ExecutionEnginePluginRegistry:
    return (
        ExecutionEnginePluginRegistry()
        .register(
            ExecutionEnginePluginDescriptor(
                name="provider_free_paper",
                implementation=f"{__name__}.ProviderFreePaperEngine",
                roles=("authoritative",),
            ),
            lambda _request: ProviderFreePaperEngine(),
        )
        .freeze()
    )


def _request(tmp_path: Path) -> ExecutionEngineBuildRequest:
    return ExecutionEngineBuildRequest(
        output_root=tmp_path / "outputs",
        config={},
        role="authoritative",
    )


def test_default_registry_is_frozen_explicit_and_content_hashed() -> None:
    descriptors = {row.name: row for row in EXECUTION_ENGINE_PLUGINS.descriptors()}

    assert set(descriptors) == {"legacy_paper", "nautilus_paper", "shadowing"}
    assert descriptors["legacy_paper"].roles == ("authoritative",)
    assert descriptors["nautilus_paper"].roles == ("authoritative", "shadow")
    assert descriptors["nautilus_paper"].requires_attended_authority is True
    assert descriptors["nautilus_paper"].requires_cutover_gate is True
    assert descriptors["nautilus_paper"].requires_runtime_path is True
    assert descriptors["shadowing"].roles == ("wrapper",)
    assert "flush_shadow" in descriptors["shadowing"].capabilities
    assert EXECUTION_ENGINE_PLUGINS.fingerprint.startswith("sha256:")
    assert build_default_execution_engine_registry().fingerprint == EXECUTION_ENGINE_PLUGINS.fingerprint

    with pytest.raises(RuntimeError, match="frozen"):
        EXECUTION_ENGINE_PLUGINS.register(
            ExecutionEnginePluginDescriptor(
                name="late_plugin",
                implementation="tests.LatePlugin",
                roles=("authoritative",),
            ),
            lambda _request: ProviderFreePaperEngine(),
        )


def test_custom_provider_free_paper_engine_composes_without_core_engine_branch(tmp_path: Path) -> None:
    registry = _provider_free_registry()
    config = {
        "execution_engine": {
            "authoritative": "provider_free_paper",
            "shadow": "none",
            "real_money_eligible": False,
        }
    }

    selection = execution_engine_selection(config, environ={}, registry=registry)
    runtime = compose_configured_execution_engine(
        tmp_path / "outputs",
        config=config,
        environ={},
        registry=registry,
    )

    assert selection["authoritative"] == "provider_free_paper"
    assert selection["paper_only"] is True
    assert selection["real_money_eligible"] is False
    assert isinstance(runtime.port, ProviderFreePaperEngine)
    assert runtime.audit_dict() == {
        "schema_version": "dualtrack-execution-engine-runtime-v1",
        "authoritative_plugin": "provider_free_paper",
        "shadow_plugin": "none",
        "wrapper_plugin": "none",
        "registry_fingerprint": registry.fingerprint,
        "paper_only": True,
        "real_money_eligible": False,
    }


def test_unknown_configured_plugin_fails_before_creating_output_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="unsupported authoritative execution engine"):
        compose_configured_execution_engine(
            output,
            config={
                "execution_engine": {
                    "authoritative": "not_registered",
                    "shadow": "none",
                }
            },
            environ={},
        )

    assert not output.exists()


def test_missing_shadow_runtime_stays_non_authoritative_and_explicit(tmp_path: Path) -> None:
    runtime = compose_configured_execution_engine(
        tmp_path / "outputs",
        config={"execution_engine": {"authoritative": "legacy_paper"}},
        environ={},
    )

    assert runtime.port.name == "legacy_paper"
    assert runtime.audit_dict()["shadow_plugin"] == "nautilus_paper"
    assert runtime.audit_dict()["wrapper_plugin"] == "shadowing"
    result = runtime.port.flush_shadow("2026-07-18_DAY")
    assert result["status"] == "blocked"
    assert result["blocker"] == "nautilus_shadow_runtime_missing"
    assert result["authoritative_engine"] == "legacy_paper"
    assert result["authoritative_unchanged"] is True


def test_registry_rejects_empty_duplicate_malformed_and_non_paper_plugins() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        ExecutionEnginePluginRegistry().freeze()

    registry = ExecutionEnginePluginRegistry().register(
        ExecutionEnginePluginDescriptor(
            name="provider_free_paper",
            implementation="tests.ProviderFreePaperEngine",
            roles=("authoritative",),
        ),
        lambda _request: ProviderFreePaperEngine(),
    )
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(
            ExecutionEnginePluginDescriptor(
                name="provider_free_paper",
                implementation="tests.SecondEngine",
                roles=("authoritative",),
            ),
            lambda _request: ProviderFreePaperEngine(),
        )
    with pytest.raises(ValueError, match="name is invalid"):
        ExecutionEnginePluginRegistry().register(
            ExecutionEnginePluginDescriptor(
                name="Bad Plugin",
                implementation="tests.BadPlugin",
                roles=("authoritative",),
            ),
            lambda _request: ProviderFreePaperEngine(),
        )
    with pytest.raises(ValueError, match="paper-only"):
        ExecutionEnginePluginRegistry().register(
            ExecutionEnginePluginDescriptor(
                name="live_plugin",
                implementation="tests.LivePlugin",
                roles=("authoritative",),
                paper_only=False,
                real_money_eligible=True,
            ),
            lambda _request: ProviderFreePaperEngine(),
        )
    with pytest.raises(ValueError, match="omits required capabilities"):
        ExecutionEnginePluginRegistry().register(
            ExecutionEnginePluginDescriptor(
                name="thin_plugin",
                implementation="tests.ThinPlugin",
                roles=("authoritative",),
                capabilities=EXECUTION_ENGINE_CAPABILITIES[:-1],
            ),
            lambda _request: ProviderFreePaperEngine(),
        )


def test_registry_rejects_wrong_role_and_invalid_factory_product(tmp_path: Path) -> None:
    registry = _provider_free_registry()

    with pytest.raises(ValueError, match="does not support shadow role"):
        registry.build(
            "provider_free_paper",
            ExecutionEngineBuildRequest(
                output_root=tmp_path / "outputs",
                config={},
                role="shadow",
            ),
        )

    invalid = (
        ExecutionEnginePluginRegistry()
        .register(
            ExecutionEnginePluginDescriptor(
                name="invalid_product",
                implementation="builtins.object",
                roles=("authoritative",),
            ),
            lambda _request: object(),
        )
        .freeze()
    )
    with pytest.raises(TypeError, match="invalid adapter"):
        invalid.build("invalid_product", _request(tmp_path))

    false_identity = (
        ExecutionEnginePluginRegistry()
        .register(
            ExecutionEnginePluginDescriptor(
                name="false_identity",
                implementation="tests.NotTheReturnedEngine",
                roles=("authoritative",),
            ),
            lambda _request: ProviderFreePaperEngine(),
        )
        .freeze()
    )
    with pytest.raises(TypeError, match="implementation mismatch"):
        false_identity.build("false_identity", _request(tmp_path))


def test_unfrozen_registry_cannot_resolve_or_build(tmp_path: Path) -> None:
    registry = ExecutionEnginePluginRegistry().register(
        ExecutionEnginePluginDescriptor(
            name="provider_free_paper",
            implementation="tests.ProviderFreePaperEngine",
            roles=("authoritative",),
        ),
        lambda _request: ProviderFreePaperEngine(),
    )

    with pytest.raises(RuntimeError, match="must be frozen"):
        registry.descriptor("provider_free_paper")
    with pytest.raises(RuntimeError, match="must be frozen"):
        registry.build("provider_free_paper", _request(tmp_path))


def test_nautilus_parity_hash_covers_every_execution_plugin_semantic_file() -> None:
    assert {
        "services/dualtrack_execution_adapter.py",
        "services/execution_engine_plugin_registry.py",
        "services/execution_engine_port.py",
        "services/execution_plugin_composition.py",
        "services/legacy_paper_execution_adapter.py",
    }.issubset(PLATFORM_CODE_PATHS)
