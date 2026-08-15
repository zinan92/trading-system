"""Build source-bound, credential-free evidence for Park Paper cutover.

The Park control timer owns this artifact.  It is deliberately generated from
read-only checks immediately before a runtime pass; a missing, stale, or
failed check is represented as a false gate and never upgraded by inference.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from services.config_loader import ROOT
from services.journal_store import load_json, write_json
from services.paper_predeploy_gate import PaperPredeployGate
from services.paper_release_receipt import (
    DEFAULT_RELEASE_GATE_MAX_AGE_SECONDS,
    PaperReleaseReceiptGate,
    PaperServiceBootGate,
    current_source_attestation,
)
from services.park_cutover_guard import REQUIRED_SAFETY_GATES
from services.park_paper_preflight import (
    PARK_PAPER_PREFLIGHT_PATH,
    ParkPaperPreflightError,
    paper_execution_config_digest,
    validate_park_paper_preflight,
)
from services.supervisor_execution_profile import (
    FAIL_CLOSED,
    resolve_supervisor_execution_profile,
)


PARK_SAFETY_EVIDENCE_SCHEMA = "park-safety-evidence-v1"
PARK_SAFETY_EVIDENCE_PATH = "park_strategy/safety_evidence.json"
PARK_SAFETY_EVIDENCE_MAX_AGE_SECONDS = DEFAULT_RELEASE_GATE_MAX_AGE_SECONDS


def _now(value: datetime | None = None) -> datetime:
    observed = value or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc).replace(microsecond=0)


def _latest(path: Path) -> dict[str, Any]:
    rows = load_json(path)
    return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}


def _append_blocker(blockers: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in blockers:
        blockers.append(text)


def _supervisor_fail_closed(config: Mapping[str, Any]) -> tuple[bool, str]:
    """Prove the Park path selects the inert Supervisor profile explicitly."""

    profile = str(config.get("supervisor_execution_profile") or "").strip()
    if profile != FAIL_CLOSED:
        return False, "park_supervisor_execution_profile_not_fail_closed"
    if any(config.get(field) is not False for field in ("autonomous", "shadow_mutation", "feishu_control")):
        return False, "park_forbidden_control_path_enabled"
    engine = config.get("execution_engine")
    engine = engine if isinstance(engine, Mapping) else {}
    authoritative = str(engine.get("authoritative") or "nautilus_paper").strip()
    try:
        resolved = resolve_supervisor_execution_profile(
            {"convergence": {"execution_profile": profile}},
            execution_name=authoritative,
        )
    except Exception as exc:  # noqa: BLE001 - profile uncertainty must block.
        return False, f"supervisor_execution_profile_invalid:{type(exc).__name__}"
    if resolved != FAIL_CLOSED:
        return False, "park_supervisor_execution_profile_not_fail_closed"
    return True, ""


def build_park_safety_evidence(
    output_root: Path,
    *,
    config: Mapping[str, Any],
    repo_root: Path = ROOT,
    interpreter: str | Path | None = None,
    adapter: Any | None = None,
    now: datetime | None = None,
    service_name: str = "park-paper-runtime",
    source_attestation_reader: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write one source-bound safety receipt without touching Paper state.

    ``adapter`` is used only for its read-only snapshot capability surface.  A
    caller may omit it in tests or during startup; the immutable-fill gate then
    remains false and cutover stays blocked.
    """

    root = Path(output_root)
    observed_at = _now(now)
    expires_at = observed_at + timedelta(seconds=PARK_SAFETY_EVIDENCE_MAX_AGE_SECONDS)
    blockers: list[str] = []
    attestation: dict[str, Any] = {
        "source_sha": "",
        "source_tree_sha": "",
        "tracked_tree_clean": False,
    }
    source_reader = source_attestation_reader or (
        lambda: current_source_attestation(Path(repo_root))
    )
    try:
        attestation = dict(source_reader())
    except Exception as exc:  # noqa: BLE001 - source uncertainty must block.
        _append_blocker(blockers, f"source_attestation_failed:{type(exc).__name__}")
    if attestation.get("tracked_tree_clean") is not True:
        _append_blocker(blockers, "tracked_source_tree_dirty")

    release_sha = str(attestation.get("source_sha") or "").strip().lower()
    source_tree_sha = str(attestation.get("source_tree_sha") or "").strip().lower()
    gates: dict[str, bool] = {gate: False for gate in REQUIRED_SAFETY_GATES}
    evidence_detail: dict[str, Any] = {
        gate: {"status": "runtime_pending"}
        for gate in ("trusted_market", "tick_freshness", "stale_cycle_state", "reconciliation", "park_risk_confirmation")
    }

    predeploy: dict[str, Any] = {}
    release: dict[str, Any] = {}
    boot: dict[str, Any] = {}
    try:
        predeploy = PaperPredeployGate(
            root,
            interpreter=str(interpreter or "") or None,
            source_attestation_resolver=source_reader,
        ).run()
        release = PaperReleaseReceiptGate(
            root,
            repo_root=Path(repo_root),
            source_attestation_resolver=source_reader,
        ).verify()
        boot = PaperServiceBootGate(
            root,
            repo_root=Path(repo_root),
            source_attestation_resolver=source_reader,
        ).verify(service_name)
    except Exception as exc:  # noqa: BLE001 - every release uncertainty blocks.
        _append_blocker(blockers, f"release_boot_evidence_failed:{type(exc).__name__}")

    predeploy_ok = predeploy.get("status") == "pass"
    release_ok = release.get("ok") is True
    boot_ok = boot.get("ok") is True
    if not predeploy_ok:
        _append_blocker(blockers, f"paper_predeploy_not_passing:{predeploy.get('reason') or 'unknown'}")
    if not release_ok:
        _append_blocker(blockers, f"paper_release_not_owned:{release.get('blocker') or 'unknown'}")
    if not boot_ok:
        _append_blocker(blockers, f"paper_boot_not_verified:{boot.get('blocker') or 'unknown'}")

    release_sha_ownership = bool(
        release_ok
        and boot_ok
        and release_sha
        and release.get("source_sha") == release_sha
        and release.get("source_tree_sha") == source_tree_sha
        and boot.get("source_sha") == release_sha
        and boot.get("source_tree_sha") == source_tree_sha
        and attestation.get("tracked_tree_clean") is True
    )
    gates["release_sha_ownership"] = release_sha_ownership
    gates["boot"] = boot_ok and boot.get("source_sha") == release_sha and boot.get("source_tree_sha") == source_tree_sha
    if not release_sha_ownership:
        _append_blocker(blockers, "release_sha_ownership_failed")
    if not gates["boot"]:
        _append_blocker(blockers, "boot_evidence_failed")

    preflight: dict[str, Any] = _latest(root / PARK_PAPER_PREFLIGHT_PATH)
    preflight_ok = False
    try:
        validate_park_paper_preflight(
            preflight,
            expected_config_digest=paper_execution_config_digest(config),
            repo_root=Path(repo_root),
            now=observed_at,
        )
        preflight_ok = True
    except (ParkPaperPreflightError, OSError, ValueError) as exc:
        _append_blocker(blockers, f"paper_preflight_not_admissible:{getattr(exc, 'code', type(exc).__name__)}")

    adapter_name = str(getattr(adapter, "name", "")).strip()
    snapshot: Mapping[str, Any] = {}
    if adapter is not None:
        try:
            snapshot = dict(adapter.snapshot("park-safety-evidence"))
        except Exception as exc:  # noqa: BLE001 - read-only capability uncertainty blocks.
            _append_blocker(blockers, f"paper_adapter_capability_unavailable:{type(exc).__name__}")
    capabilities = snapshot.get("capabilities") if isinstance(snapshot, Mapping) else {}
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    immutable_fill = capabilities.get("immutable_fill_guard") is True
    if not immutable_fill:
        _append_blocker(blockers, "immutable_fill_capability_missing")
    gates["immutable_fill"] = immutable_fill

    engine = config.get("execution_engine")
    engine = engine if isinstance(engine, Mapping) else {}
    paper_only = bool(
        preflight_ok
        and adapter_name == "nautilus_paper"
        and config.get("runtime_mode") == "paper_only"
        and config.get("control_plane") == "telegram"
        and config.get("execution_track_count") == 1
        and isinstance(engine, Mapping)
        and engine.get("real_money_eligible") is False
    )
    gates["paper_only"] = paper_only
    if not paper_only:
        _append_blocker(blockers, "paper_only_contract_not_proven")

    supervisor_ok, supervisor_blocker = _supervisor_fail_closed(config)
    gates["supervisor_fail_closed"] = supervisor_ok
    if supervisor_blocker:
        _append_blocker(blockers, supervisor_blocker)

    # These are supplied by the runtime after it reads the trusted market,
    # current snapshot, reconciliation, and exact Park confirmation.  They are
    # intentionally not written as false here, so the runtime can evaluate them
    # with the facts from the same pass.
    evidence_detail.update({
        "release_sha_ownership": {
            "status": "pass" if gates["release_sha_ownership"] else "blocked",
            "source_sha": release_sha,
            "source_tree_sha": source_tree_sha,
            "release_receipt": release,
            "boot_receipt": boot.get("boot_receipt") if isinstance(boot, Mapping) else {},
        },
        "boot": {
            "status": "pass" if gates["boot"] else "blocked",
            "service": service_name,
            "source_sha": str(boot.get("source_sha") or ""),
            "source_tree_sha": str(boot.get("source_tree_sha") or ""),
        },
        "immutable_fill": {
            "status": "pass" if immutable_fill else "blocked",
            "adapter": adapter_name,
            "capability": "immutable_fill_guard",
        },
        "paper_only": {
            "status": "pass" if paper_only else "blocked",
            "adapter": adapter_name,
            "preflight": str(root / PARK_PAPER_PREFLIGHT_PATH),
        },
        "supervisor_fail_closed": {
            "status": "pass" if supervisor_ok else "blocked",
            "profile": str(config.get("supervisor_execution_profile") or ""),
        },
    })

    for gate in ("release_sha_ownership", "boot", "immutable_fill", "paper_only", "supervisor_fail_closed"):
        if not gates[gate]:
            _append_blocker(blockers, f"safety_gate_failed:{gate}")

    artifact: dict[str, Any] = {
        "schema_version": PARK_SAFETY_EVIDENCE_SCHEMA,
        "status": "pass" if not blockers else "blocked",
        "checked_at": observed_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "max_age_seconds": PARK_SAFETY_EVIDENCE_MAX_AGE_SECONDS,
        "release_sha": release_sha,
        "source_tree_sha": source_tree_sha,
        "tracked_tree_clean": attestation.get("tracked_tree_clean") is True,
        "boot_verified": gates["boot"],
        **{gate: gates[gate] for gate in ("immutable_fill", "paper_only", "release_sha_ownership", "boot", "supervisor_fail_closed")},
        "evidence": evidence_detail,
        "predeploy": {
            "status": str(predeploy.get("status") or "blocked"),
            "checked_at": str(predeploy.get("checked_at") or ""),
            "expires_at": str(predeploy.get("expires_at") or ""),
        },
        "preflight": {
            "status": str(preflight.get("status") or "missing"),
            "expires_at": str(preflight.get("expires_at") or ""),
            "config_digest": str(preflight.get("config_digest") or ""),
        },
        "blockers": sorted(blockers),
        "operations": {
            "reads_exchange_credentials": False,
            "starts_services": False,
            "submits_orders": False,
            "cancels_orders": False,
            "closes_positions": False,
            "mutations": [],
        },
    }
    write_json(root / PARK_SAFETY_EVIDENCE_PATH, [artifact])
    return artifact
