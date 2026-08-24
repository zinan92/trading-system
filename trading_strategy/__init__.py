"""Canonical, engine-neutral DCA and Grid Strategy foundation."""

from .dca_plan import (
    DCA_DIRECTIONS,
    DCA_PLAN_SCHEMA,
    DCA_PREVIEW_SCHEMA,
    DCA_REPLAY_SCHEMA,
    build_dca_entry_commands,
    build_dca_preview,
    build_dca_strategy_plan,
    build_deterministic_dca_candidate_payload_v1,
    replay_dca_marks,
)
from .grid_core import (
    GRID_LINE_STATES,
    Bar,
    GridLineLifecycle,
    GridResult,
    GridStop,
    simulate_conditional_grid,
    simulate_explicit_grid,
)
from .grid_sizing import (
    GRID_DIRECTIONS,
    GRID_MODES,
    GRID_STYLES,
    build_adaptive_grid_preview,
    build_grid_preview,
)

__all__ = [
    "Bar",
    "DCA_DIRECTIONS",
    "DCA_PLAN_SCHEMA",
    "DCA_PREVIEW_SCHEMA",
    "DCA_REPLAY_SCHEMA",
    "GRID_DIRECTIONS",
    "GRID_LINE_STATES",
    "GRID_MODES",
    "GRID_STYLES",
    "GridLineLifecycle",
    "GridResult",
    "GridStop",
    "build_adaptive_grid_preview",
    "build_dca_entry_commands",
    "build_dca_preview",
    "build_dca_strategy_plan",
    "build_deterministic_dca_candidate_payload_v1",
    "build_grid_preview",
    "replay_dca_marks",
    "simulate_conditional_grid",
    "simulate_explicit_grid",
]
