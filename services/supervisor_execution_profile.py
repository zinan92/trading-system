"""Closed execution-profile policy for Supervisor convergence behavior."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


FAIL_CLOSED = "fail_closed"
PAPER_CONTINUOUS = "paper_continuous"
EXECUTION_PROFILES = frozenset({FAIL_CLOSED, PAPER_CONTINUOUS})
PAPER_EXECUTION_NAMES = frozenset({"legacy_paper", "nautilus_paper"})


class SupervisorExecutionProfileError(ValueError):
    """Stable failure for an invalid or unsafe profile selection."""


def resolve_supervisor_execution_profile(
    config: Mapping[str, Any],
    *,
    execution_name: str,
) -> str:
    """Resolve one source-controlled profile and prove Paper-only eligibility."""

    convergence = config.get("convergence")
    if not isinstance(convergence, Mapping):
        raise SupervisorExecutionProfileError(
            "supervisor_execution_profile_invalid"
        )
    profile = str(convergence.get("execution_profile") or "").strip()
    if profile not in EXECUTION_PROFILES:
        raise SupervisorExecutionProfileError(
            "supervisor_execution_profile_invalid"
        )
    if profile == FAIL_CLOSED:
        return profile

    engine = config.get("execution_engine")
    if not isinstance(engine, Mapping):
        raise SupervisorExecutionProfileError(
            "paper_only_execution_profile_required"
        )
    authoritative = str(engine.get("authoritative") or "").lower()
    adapter = str(execution_name or "").lower()
    if (
        authoritative not in PAPER_EXECUTION_NAMES
        or adapter != authoritative
        or engine.get("real_money_eligible") is not False
        or engine.get("paper_gate_override_approved") is not True
    ):
        raise SupervisorExecutionProfileError(
            "paper_only_execution_profile_required"
        )
    return profile
