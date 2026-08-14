"""Read-only final cutover and safety gate for Park Strategy Track."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


PARK_CUTOVER_SCHEMA = "park-cutover-gate-v1"
REQUIRED_SAFETY_GATES = (
    "trusted_market",
    "tick_freshness",
    "stale_cycle_state",
    "reconciliation",
    "immutable_fill",
    "park_risk_confirmation",
    "paper_only",
    "release_sha_ownership",
    "boot",
    "supervisor_fail_closed",
)


class ParkCutoverError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_default_config() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "configs" / "park_strategy_track.json"
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_park_cutover(
    config: Mapping[str, Any] | None = None,
    *,
    safety_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate admission only; this function has no mutation capability."""

    settings = dict(config or load_default_config())
    evidence = dict(safety_evidence or {})
    blockers: list[str] = []
    if settings.get("feature_enabled") is not True:
        blockers.append("park_track_disabled")
    if settings.get("execution_track_count") != 1:
        blockers.append("execution_track_count_not_one")
    if settings.get("runtime_mode") != "paper_only":
        blockers.append("paper_only_required")
    if settings.get("control_plane") != "telegram":
        blockers.append("telegram_only_required")
    for key, code in (
        ("autonomous", "autonomous_forbidden"),
        ("shadow_mutation", "shadow_mutation_forbidden"),
        ("feishu_control", "feishu_control_forbidden"),
    ):
        if settings.get(key) is not False:
            blockers.append(code)
    if not str(evidence.get("release_sha") or "").strip():
        blockers.append("release_sha_missing")
    if evidence.get("boot_verified") is not True:
        blockers.append("boot_unverified")
    for gate in REQUIRED_SAFETY_GATES:
        if evidence.get(gate) is not True:
            blockers.append(f"safety_gate_failed:{gate}")
    return {
        "schema_version": PARK_CUTOVER_SCHEMA,
        "status": "pass" if not blockers else "blocked",
        "blockers": blockers,
        "execution_track_count": settings.get("execution_track_count"),
        "feature_enabled": settings.get("feature_enabled") is True,
        "mutations": [],
        "deployment_required": True,
        "runtime_ready_claim": False,
    }
