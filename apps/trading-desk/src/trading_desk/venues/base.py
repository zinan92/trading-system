"""The Desk Venue Module contract and its frozen registry."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from services.structured_log import event, get_logger

LOG = get_logger("desk.venues")

# Every flow the desk switches on a capability. Declared by a module, never inferred from its methods.
CAPABILITIES = frozenset({
    "live_price",         # the venue gives a current price for the plan (else the last 1-hour close)
    "bars",               # K-lines for the chart
    "grid_plan",          # the desk drafts a grid plan from Park's direction (else the call is record-only)
    "preview",            # a non-authorizing preview of that plan
    "execute",            # Park's 执行 button may hand the plan on (also needs preview)
    "stop",               # Park's 停止网格 button applies
    "rearm_hold",         # 暂停补单 / 恢复补单 applies
    "price_alerts",       # the 20-minute job watches this asset's grid against the price
    "bars_health",        # the system page checks this venue's K-line feed
    "futures_overview",   # the 20-minute job refreshes this asset's futures overview
})


class VenueModule:
    """Desk-side behaviour of one kind. Subclasses set the class attributes and override what they declare."""

    kind: str = ""
    capabilities: frozenset[str] = frozenset()
    plan_template: Mapping[str, Any] | None = None
    started_text: str = ""
    bars_health_label: str = ""

    def venue_text(self, sources: Any, asset: Mapping[str, Any]) -> tuple[str, str]:
        """(venue, money) shown under the asset name."""
        raise NotImplementedError

    def asset_row(self, sources: Any, row: dict[str, Any]) -> dict[str, Any]:
        """The /api/assets row; a module whose venue comes from a catalog fills it in here."""
        return row

    def grid_and_price(self, sources: Any, asset: Mapping[str, Any]) -> tuple[dict[str, Any], float | None]:
        """The asset's grid record and the venue's current price (None when it has none)."""
        raise NotImplementedError

    def bars(self, sources: Any, asset: Mapping[str, Any], timeframe: str, limit: int) -> dict[str, Any]:
        return {"ok": False, "reason": "这个品种没有 K 线", "bars": []}

    def preview(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def execute(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any], *, shown_max_loss: float | None,
                judgment_id: int) -> dict[str, Any]:
        raise NotImplementedError

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


def _overrides(module: VenueModule, name: str) -> bool:
    return getattr(type(module), name) is not getattr(VenueModule, name)


# What a declared capability needs the module to provide. Missing it means the capability is dropped.
NEEDS = {
    "grid_plan": lambda m: bool(m.plan_template),
    "preview": lambda m: _overrides(m, "preview"),
    "execute": lambda m: _overrides(m, "execute") and _overrides(m, "preview") and "preview" in m.capabilities and bool(m.started_text),
    "bars": lambda m: _overrides(m, "bars"),
    "bars_health": lambda m: _overrides(m, "bars") and bool(m.bars_health_label),
}


@dataclass(frozen=True)
class Registry:
    modules: Mapping[str, VenueModule]

    def get(self, kind: str) -> VenueModule:
        try:
            return self.modules[kind]
        except KeyError:
            raise ValueError(f"unknown venue kind: {kind}") from None

    def of(self, asset: Mapping[str, Any]) -> VenueModule:
        return self.get(str(asset["kind"]))

    def kinds(self) -> tuple[str, ...]:
        return tuple(self.modules)

    def having(self, capability: str) -> tuple[str, ...]:
        return tuple(kind for kind, module in self.modules.items() if module.can(capability))


def build(*modules: VenueModule) -> Registry:
    """Freeze the registry. An unknown capability is an error; one the module cannot back is dropped and logged."""
    table: dict[str, VenueModule] = {}
    for module in modules:
        if not module.kind or module.kind in table:
            raise ValueError(f"venue kind missing or repeated: {module.kind!r}")
        unknown = set(module.capabilities) - CAPABILITIES
        if unknown:
            raise ValueError(f"{module.kind}: unknown capabilities {sorted(unknown)}")
        dropped = sorted(name for name, needs in NEEDS.items() if name in module.capabilities and not needs(module))
        if dropped:
            event(LOG, "venue_capability_dropped", kind=module.kind, dropped=dropped, level=logging.WARNING)
            module.capabilities = frozenset(module.capabilities) - set(dropped)
        else:
            module.capabilities = frozenset(module.capabilities)
        table[module.kind] = module
    return Registry(MappingProxyType(table))
