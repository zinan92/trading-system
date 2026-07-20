"""Provider-free execution port and immutable plugin metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


EXECUTION_ENGINE_PORT_SCHEMA = "dualtrack-execution-engine-port-v1"
EXECUTION_ENGINE_RUNTIME_SCHEMA = "dualtrack-execution-engine-runtime-v1"
EXECUTION_ENGINE_ROLES = frozenset({"authoritative", "shadow", "wrapper"})
EXECUTION_ENGINE_CAPABILITIES = (
    "submit_order",
    "cancel_orders",
    "process_market_event",
    "snapshot",
    "reconcile",
)


@runtime_checkable
class ExecutionEngineAdapter(Protocol):
    name: str

    def submit_order(self, command: dict[str, Any]) -> dict[str, Any]:
        ...

    def cancel_orders(
        self,
        cycle_id: str,
        *,
        order_ids: list[str] | None = None,
        strategy_plan_id: str | None = None,
        ts: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        ...

    def process_market_event(self, event: dict[str, Any]) -> dict[str, Any]:
        ...

    def snapshot(
        self,
        cycle_id: str,
        *,
        mark_price: float | None = None,
        mark_fresh: bool = False,
        mark_source: str = "",
    ) -> dict[str, Any]:
        ...

    def reconcile(self, cycle_id: str) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class ExecutionEnginePluginDescriptor:
    """Trusted metadata registered by the application composition root."""

    name: str
    implementation: str
    roles: tuple[str, ...]
    capabilities: tuple[str, ...] = EXECUTION_ENGINE_CAPABILITIES
    paper_only: bool = True
    real_money_eligible: bool = False
    requires_attended_authority: bool = False
    requires_runtime_path: bool = False
    requires_cutover_gate: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "implementation": self.implementation,
            "roles": list(self.roles),
            "capabilities": list(self.capabilities),
            "paper_only": self.paper_only,
            "real_money_eligible": self.real_money_eligible,
            "requires_attended_authority": self.requires_attended_authority,
            "requires_runtime_path": self.requires_runtime_path,
            "requires_cutover_gate": self.requires_cutover_gate,
        }


@dataclass(frozen=True)
class ExecutionEngineBuildRequest:
    """Uniform factory input; config expresses intent but never grants authority."""

    output_root: Path
    config: dict[str, Any]
    role: str
    runtime_path: str | Path | None = None
    authoritative: ExecutionEngineAdapter | None = None
    shadow: ExecutionEngineAdapter | None = None
    blocker: str = ""


class ExecutionEngineFactory(Protocol):
    def __call__(self, request: ExecutionEngineBuildRequest) -> ExecutionEngineAdapter:
        ...


@dataclass(frozen=True)
class ExecutionEngineRuntime:
    port: ExecutionEngineAdapter
    authoritative_plugin: str
    shadow_plugin: str
    wrapper_plugin: str
    registry_fingerprint: str
    selection: dict[str, Any]

    def audit_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXECUTION_ENGINE_RUNTIME_SCHEMA,
            "authoritative_plugin": self.authoritative_plugin,
            "shadow_plugin": self.shadow_plugin,
            "wrapper_plugin": self.wrapper_plugin,
            "registry_fingerprint": self.registry_fingerprint,
            "paper_only": True,
            "real_money_eligible": False,
        }
