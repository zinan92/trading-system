"""Compatibility facade for the A11 execution plugin composition root."""

from services.execution_engine_port import ExecutionEngineAdapter
from services.execution_plugin_composition import (
    NAUTILUS_PAPER_GATE_OVERRIDE_ACKNOWLEDGEMENT,
    build_configured_execution_engine_adapter,
    build_execution_engine_adapter,
    execution_engine_selection,
)
from services.legacy_paper_execution_adapter import LegacyPaperExecutionAdapter


__all__ = (
    "ExecutionEngineAdapter",
    "LegacyPaperExecutionAdapter",
    "NAUTILUS_PAPER_GATE_OVERRIDE_ACKNOWLEDGEMENT",
    "build_configured_execution_engine_adapter",
    "build_execution_engine_adapter",
    "execution_engine_selection",
)
