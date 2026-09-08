"""Read-only final cutover and safety gate for Park Strategy Track."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
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


def load_park_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Load the explicit Park config selected by the runtime environment.

    Dashboard surfaces must share the same config source as ``park_control``;
    silently falling back to the repository's disabled sample config would
    make a Paper runtime appear unavailable for the wrong reason.
    """

    environment = os.environ if environ is None else environ
    raw_path = str(environment.get("TRADING_ORCHESTRATOR_PARK_CONFIG") or "").strip()
    if not raw_path:
        raise ParkCutoverError(
            "park_config_missing",
            "TRADING_ORCHESTRATOR_PARK_CONFIG is required for Park Dashboard AI chat",
        )
    path = Path(raw_path).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParkCutoverError(
            "park_config_unavailable",
            f"Park config could not be read from {path}",
        ) from exc
    if not isinstance(value, dict):
        raise ParkCutoverError("park_config_invalid", "Park config must be a JSON object")
    return dict(value)


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
    evidence_status = evidence.get("status")
    if evidence_status not in (None, "pass"):
        blockers.append("safety_evidence_not_passing")
    expires_at = _parse_timestamp(evidence.get("expires_at"))
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        blockers.append("safety_evidence_stale")
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


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
