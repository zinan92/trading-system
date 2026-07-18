"""Trusted composition root for paper execution plugins and cutover policy."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from services.dualtrack_nautilus_execution_adapter import NautilusExecutionAdapter
from services.dualtrack_shadow_execution_adapter import ShadowingExecutionEngineAdapter
from services.execution_engine_plugin_registry import ExecutionEnginePluginRegistry
from services.execution_engine_port import (
    EXECUTION_ENGINE_CAPABILITIES,
    ExecutionEngineAdapter,
    ExecutionEngineBuildRequest,
    ExecutionEnginePluginDescriptor,
    ExecutionEngineRuntime,
)
from services.journal_store import load_json
from services.legacy_paper_execution_adapter import LegacyPaperExecutionAdapter


NAUTILUS_PAPER_GATE_OVERRIDE_ACKNOWLEDGEMENT = (
    "I_UNDERSTAND_NAUTILUS_PAPER_CUTOVER_BYPASSES_7_CYCLE_SHADOW_GATE"
)
EXECUTION_ENGINE_ALIASES = {"nautilus": "nautilus_paper"}
NAUTILUS_EXECUTION_IMPLEMENTATION = (
    "services.dualtrack_nautilus_execution_adapter.NautilusExecutionAdapter"
)


def _legacy_factory(request: ExecutionEngineBuildRequest) -> ExecutionEngineAdapter:
    return LegacyPaperExecutionAdapter(request.output_root, config=request.config)


def _nautilus_factory(request: ExecutionEngineBuildRequest) -> ExecutionEngineAdapter:
    if request.runtime_path in (None, ""):
        raise RuntimeError("Nautilus paper switch requires an isolated runtime path")
    return NautilusExecutionAdapter(
        request.output_root,
        nautilus_python=request.runtime_path,
        storage_namespace=(
            "nautilus_authoritative" if request.role == "authoritative" else "nautilus_paper"
        ),
        defer_replay=request.role == "shadow",
        config=request.config,
    )


def _shadowing_factory(request: ExecutionEngineBuildRequest) -> ExecutionEngineAdapter:
    if request.authoritative is None:
        raise ValueError("shadowing execution plugin requires an authoritative adapter")
    return ShadowingExecutionEngineAdapter(
        request.output_root,
        authoritative=request.authoritative,
        shadow=request.shadow,
        blocker=request.blocker,
    )


def build_default_execution_engine_registry() -> ExecutionEnginePluginRegistry:
    return (
        ExecutionEnginePluginRegistry()
        .register(
            ExecutionEnginePluginDescriptor(
                name="legacy_paper",
                implementation="services.legacy_paper_execution_adapter.LegacyPaperExecutionAdapter",
                roles=("authoritative",),
            ),
            _legacy_factory,
        )
        .register(
            ExecutionEnginePluginDescriptor(
                name="nautilus_paper",
                implementation=NAUTILUS_EXECUTION_IMPLEMENTATION,
                roles=("authoritative", "shadow"),
                requires_attended_authority=True,
                requires_runtime_path=True,
                requires_cutover_gate=True,
            ),
            _nautilus_factory,
        )
        .register(
            ExecutionEnginePluginDescriptor(
                name="shadowing",
                implementation=(
                    "services.dualtrack_shadow_execution_adapter.ShadowingExecutionEngineAdapter"
                ),
                roles=("wrapper",),
                capabilities=(*EXECUTION_ENGINE_CAPABILITIES, "flush_shadow"),
            ),
            _shadowing_factory,
        )
        .freeze()
    )


EXECUTION_ENGINE_PLUGINS = build_default_execution_engine_registry()


def execution_engine_selection(
    config: dict[str, Any] | None,
    *,
    environ: dict[str, str] | None = None,
    registry: ExecutionEnginePluginRegistry = EXECUTION_ENGINE_PLUGINS,
) -> dict[str, Any]:
    """Resolve registered intent while retaining all authority in this core."""

    settings = dict((config or {}).get("execution_engine") or {})
    authoritative = str(settings.get("authoritative") or "legacy_paper").strip().lower()
    shadow = str(settings.get("shadow") or "nautilus_paper").strip().lower()
    try:
        authoritative_descriptor = registry.descriptor(authoritative, role="authoritative")
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unsupported authoritative execution engine: {authoritative}") from exc
    _require_restricted_authoritative_policy(authoritative_descriptor)
    if shadow != "none":
        try:
            registry.descriptor(shadow, role="shadow")
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unsupported shadow execution engine: {shadow}") from exc
    if settings.get("real_money_eligible") not in (None, False):
        raise ValueError("DualTrack execution engine selection is paper-only")

    environment = dict(os.environ if environ is None else environ)
    approved = environment.get("TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED") == "1"
    shadow_gate_override = (
        environment.get("TRADING_ORCHESTRATOR_NAUTILUS_PAPER_GATE_OVERRIDE")
        == NAUTILUS_PAPER_GATE_OVERRIDE_ACKNOWLEDGEMENT
    )
    nautilus_python = str(environment.get("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON") or "").strip()
    if authoritative_descriptor.requires_attended_authority and not approved:
        raise RuntimeError("Nautilus paper switch requires attended approval")
    if authoritative_descriptor.requires_runtime_path and not nautilus_python:
        raise RuntimeError("Nautilus paper switch requires an isolated runtime path")
    return {
        "authoritative": authoritative,
        "shadow": shadow,
        "paper_only": True,
        "real_money_eligible": False,
        "attended_approval": approved,
        "nautilus_python": nautilus_python,
        "shadow_gate_override": shadow_gate_override,
    }


def build_execution_engine_adapter(
    output_root: Path,
    *,
    engine: str = "legacy_paper",
    config: dict[str, Any] | None = None,
    nautilus_python: str | Path | None = None,
    allow_paper_switch: bool = False,
    allow_shadow_gate_override: bool = False,
    registry: ExecutionEnginePluginRegistry = EXECUTION_ENGINE_PLUGINS,
) -> ExecutionEngineAdapter:
    """Build one authoritative adapter after core-owned policy checks."""

    requested = str(engine or "legacy_paper").strip().lower()
    normalized = EXECUTION_ENGINE_ALIASES.get(requested, requested)
    try:
        descriptor = registry.descriptor(normalized, role="authoritative")
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown execution engine: {engine}") from exc
    _require_restricted_authoritative_policy(descriptor)
    if descriptor.requires_attended_authority and not allow_paper_switch:
        raise RuntimeError("Nautilus paper switch requires attended approval")
    if descriptor.requires_cutover_gate:
        _require_cutover_gate(
            Path(output_root),
            allow_shadow_gate_override=allow_shadow_gate_override,
        )
    if descriptor.requires_runtime_path and nautilus_python in (None, ""):
        raise RuntimeError("Nautilus paper switch requires an isolated runtime path")
    return registry.build(
        normalized,
        ExecutionEngineBuildRequest(
            output_root=Path(output_root),
            config=dict(config or {}),
            role="authoritative",
            runtime_path=nautilus_python,
        ),
    )


def compose_configured_execution_engine(
    output_root: Path,
    *,
    config: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
    registry: ExecutionEnginePluginRegistry = EXECUTION_ENGINE_PLUGINS,
) -> ExecutionEngineRuntime:
    """Compose authoritative and optional shadow engines through one registry."""

    if config is None:
        from services.dualtrack_config import dualtrack_config

        config = dualtrack_config()
    selection = execution_engine_selection(config, environ=environ, registry=registry)
    authoritative = build_execution_engine_adapter(
        output_root,
        engine=selection["authoritative"],
        config=config,
        nautilus_python=selection["nautilus_python"] or None,
        allow_paper_switch=selection["attended_approval"],
        allow_shadow_gate_override=selection["shadow_gate_override"],
        registry=registry,
    )
    shadow_name = str(selection["shadow"])
    if shadow_name == "none" or shadow_name == selection["authoritative"]:
        return ExecutionEngineRuntime(
            port=authoritative,
            authoritative_plugin=str(selection["authoritative"]),
            shadow_plugin=shadow_name,
            wrapper_plugin="none",
            registry_fingerprint=registry.fingerprint,
            selection=dict(selection),
        )

    environment = dict(os.environ if environ is None else environ)
    settings = dict((config or {}).get("execution_engine") or {})
    shadow_descriptor = registry.descriptor(shadow_name, role="shadow")
    shadow_runtime = str(
        environment.get("TRADING_ORCHESTRATOR_NAUTILUS_SHADOW_PYTHON")
        or settings.get("shadow_runtime_path")
        or ""
    ).strip()
    shadow: ExecutionEngineAdapter | None = None
    blocker = ""
    if shadow_descriptor.requires_runtime_path and not shadow_runtime:
        blocker = f"{_provider_prefix(shadow_name)}_shadow_runtime_missing"
    else:
        try:
            shadow = registry.build(
                shadow_name,
                ExecutionEngineBuildRequest(
                    output_root=Path(output_root),
                    config=dict(config),
                    role="shadow",
                    runtime_path=shadow_runtime or None,
                ),
            )
        except Exception as exc:
            blocker = f"{_provider_prefix(shadow_name)}_shadow_unavailable:{str(exc)[-500:]}"
    wrapper = registry.build(
        "shadowing",
        ExecutionEngineBuildRequest(
            output_root=Path(output_root),
            config=dict(config),
            role="wrapper",
            authoritative=authoritative,
            shadow=shadow,
            blocker=blocker,
        ),
    )
    return ExecutionEngineRuntime(
        port=wrapper,
        authoritative_plugin=str(selection["authoritative"]),
        shadow_plugin=shadow_name,
        wrapper_plugin="shadowing",
        registry_fingerprint=registry.fingerprint,
        selection=dict(selection),
    )


def build_configured_execution_engine_adapter(
    output_root: Path,
    *,
    config: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
    registry: ExecutionEnginePluginRegistry = EXECUTION_ENGINE_PLUGINS,
) -> ExecutionEngineAdapter:
    return compose_configured_execution_engine(
        output_root,
        config=config,
        environ=environ,
        registry=registry,
    ).port


def inspect_execution_engine_state(
    output_root: Path,
    *,
    engine: str,
    cycle_id: str,
    config: dict[str, Any] | None = None,
    runtime_path: str | Path | None = None,
    registry: ExecutionEnginePluginRegistry = EXECUTION_ENGINE_PLUGINS,
) -> dict[str, Any]:
    """Read candidate state without returning an order-capable adapter.

    This is intentionally separate from authoritative selection: attended
    prechecks need to inspect both ledgers, but only the cutover path may grant
    authority after its evidence gates pass.
    """

    requested = str(engine or "").strip().lower()
    normalized = EXECUTION_ENGINE_ALIASES.get(requested, requested)
    try:
        descriptor = registry.descriptor(normalized, role="authoritative")
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown execution engine: {engine}") from exc
    if descriptor.requires_runtime_path and runtime_path in (None, ""):
        raise RuntimeError("execution engine inspection requires an isolated runtime path")
    adapter = registry.build(
        normalized,
        ExecutionEngineBuildRequest(
            output_root=Path(output_root),
            config=dict(config or {}),
            role="authoritative",
            runtime_path=runtime_path,
        ),
    )
    return {
        "engine": normalized,
        "snapshot": adapter.snapshot(cycle_id),
        "reconciliation": adapter.reconcile(cycle_id),
        "registry_fingerprint": registry.fingerprint,
        "paper_only": True,
        "real_money_eligible": False,
    }


def _require_cutover_gate(output_root: Path, *, allow_shadow_gate_override: bool) -> None:
    gate_rows = load_json(output_root / "dualtrack" / "cutover" / "shadow_gate_current.json")
    gate = gate_rows[-1] if gate_rows else {}
    if gate.get("status") == "ready_for_attended_paper_switch":
        return
    parity_rows = load_json(output_root / "dualtrack" / "nautilus" / "parity" / "current.json")
    parity = parity_rows[-1] if parity_rows else {}
    if not allow_shadow_gate_override or parity.get("status") != "pass":
        raise RuntimeError("Nautilus paper switch evidence gate is not ready")


def _provider_prefix(name: str) -> str:
    return str(name).removesuffix("_paper")


def _require_restricted_authoritative_policy(
    descriptor: ExecutionEnginePluginDescriptor,
) -> None:
    """Prevent a future registration edit from weakening known cutover policy."""

    if descriptor.implementation != NAUTILUS_EXECUTION_IMPLEMENTATION:
        return
    if not (
        descriptor.requires_attended_authority
        and descriptor.requires_runtime_path
        and descriptor.requires_cutover_gate
    ):
        raise RuntimeError(
            "restricted execution plugin policy mismatch: Nautilus paper authority "
            "requires attended approval, isolated runtime, and cutover gate"
        )
