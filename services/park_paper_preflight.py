"""Source-bound, credential-free preflight for the Park Paper authority."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from services.config_loader import ROOT
from services.journal_store import write_json
from services.paper_release_receipt import current_source_attestation


PARK_PAPER_PREFLIGHT_SCHEMA = "park-paper-preflight-v1"
PARK_PAPER_PREFLIGHT_STATUS = "ready_for_park_paper"
PARK_PAPER_PREFLIGHT_MAX_AGE_SECONDS = 900
PARK_PAPER_PREFLIGHT_PATH = "park_strategy/paper_preflight_current.json"


class ParkPaperPreflightError(RuntimeError):
    """A missing or unsafe Park Paper preflight must fail closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def paper_execution_config_digest(config: Mapping[str, Any]) -> str:
    """Hash only the explicit Park Paper instrument/fee contract."""

    paper_execution = config.get("paper_execution") if isinstance(config, Mapping) else None
    payload = dict(paper_execution) if isinstance(paper_execution, Mapping) else {}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_park_paper_preflight(
    output_root: Path,
    config: Mapping[str, Any],
    *,
    repo_root: Path = ROOT,
    now: datetime | None = None,
    max_age_seconds: int = PARK_PAPER_PREFLIGHT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Persist a deterministic, Paper-only preflight without exchange access."""

    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    expires_at = observed_at + timedelta(seconds=int(max_age_seconds))
    paper_execution = config.get("paper_execution") if isinstance(config, Mapping) else None
    paper_execution = dict(paper_execution) if isinstance(paper_execution, Mapping) else {}
    instrument = paper_execution.get("instrument")
    fee_model = paper_execution.get("paper_fee_model")
    blockers: list[str] = []
    if not isinstance(instrument, Mapping):
        blockers.append("paper_instrument_contract_missing")
        instrument_payload: dict[str, Any] = {}
    else:
        instrument_payload = dict(instrument)
    if not isinstance(fee_model, Mapping):
        blockers.append("paper_fee_contract_missing")
        fee_payload: dict[str, Any] = {}
    else:
        fee_payload = dict(fee_model)

    for field in ("instrument_id", "symbol", "venue", "provider"):
        if not str(instrument_payload.get(field) or "").strip():
            blockers.append(f"paper_instrument_{field}_missing")
    if str(fee_payload.get("mode") or "") != "paper_contract":
        blockers.append("paper_fee_contract_mode_required")
    for field in ("maker_fee_rate", "taker_fee_rate", "funding_rate"):
        if fee_payload.get(field) in (None, ""):
            blockers.append(f"paper_fee_{field}_missing")
    if fee_payload.get("real_money_eligible") is not False:
        blockers.append("paper_fee_real_money_forbidden")
    if str(fee_payload.get("source") or "") != "park_paper_config":
        blockers.append("paper_fee_source_must_be_park_config")

    try:
        attestation = current_source_attestation(Path(repo_root))
    except Exception as exc:  # noqa: BLE001 - source uncertainty must block.
        attestation = {
            "source_sha": "",
            "source_tree_sha": "",
            "tracked_tree_clean": False,
        }
        blockers.append(f"source_attestation_failed:{type(exc).__name__}")
    if not attestation.get("tracked_tree_clean"):
        blockers.append("tracked_source_tree_dirty")

    fee_payload.setdefault("environment", "paper")
    fee_payload.setdefault("observed_at", observed_at.isoformat())
    fee_payload.setdefault("funding_time", observed_at.isoformat())
    fee_payload["real_money_eligible"] = False
    config_digest = paper_execution_config_digest(config)
    artifact: dict[str, Any] = {
        "schema_version": PARK_PAPER_PREFLIGHT_SCHEMA,
        "status": PARK_PAPER_PREFLIGHT_STATUS if not blockers else "blocked",
        "generated_at": observed_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "max_age_seconds": int(max_age_seconds),
        "source_sha": str(attestation.get("source_sha") or ""),
        "source_tree_sha": str(attestation.get("source_tree_sha") or ""),
        "tracked_tree_clean": bool(attestation.get("tracked_tree_clean")),
        "config_digest": config_digest,
        "instrument": instrument_payload,
        "fee_model": fee_payload,
        "paper_only": True,
        "real_money_eligible": False,
        "blockers": sorted(set(blockers)),
        "operations": {
            "reads_exchange_credentials": False,
            "starts_services": False,
            "submits_orders": False,
            "cancels_orders": False,
            "closes_positions": False,
        },
    }
    write_json(Path(output_root) / PARK_PAPER_PREFLIGHT_PATH, [artifact])
    if blockers:
        raise ParkPaperPreflightError("paper_preflight_blocked", ",".join(sorted(set(blockers))))
    return artifact


def validate_park_paper_preflight(
    artifact: Mapping[str, Any],
    *,
    expected_config_digest: str = "",
    repo_root: Path = ROOT,
    now: datetime | None = None,
) -> None:
    """Validate a Park preflight immediately before direct adapter use."""

    if artifact.get("schema_version") != PARK_PAPER_PREFLIGHT_SCHEMA:
        raise ParkPaperPreflightError("paper_preflight_schema_invalid", "Park preflight schema is invalid")
    if artifact.get("status") != PARK_PAPER_PREFLIGHT_STATUS:
        raise ParkPaperPreflightError("paper_preflight_not_ready", "Park preflight is not ready")
    if artifact.get("paper_only") is not True or artifact.get("real_money_eligible") is not False:
        raise ParkPaperPreflightError("paper_preflight_not_paper_only", "Park preflight is not Paper-only")
    if artifact.get("tracked_tree_clean") is not True:
        raise ParkPaperPreflightError("paper_preflight_source_dirty", "Park preflight source tree is dirty")
    if expected_config_digest and str(artifact.get("config_digest") or "") != expected_config_digest:
        raise ParkPaperPreflightError("paper_preflight_config_mismatch", "Park preflight config digest mismatch")
    current = current_source_attestation(Path(repo_root))
    if str(artifact.get("source_sha") or "") != str(current.get("source_sha") or ""):
        raise ParkPaperPreflightError("paper_preflight_source_sha_mismatch", "Park preflight source SHA mismatch")
    if str(artifact.get("source_tree_sha") or "") != str(current.get("source_tree_sha") or ""):
        raise ParkPaperPreflightError("paper_preflight_source_tree_mismatch", "Park preflight source tree mismatch")
    expires_at = _parse_timestamp(str(artifact.get("expires_at") or ""))
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires_at is None or expires_at <= observed_at:
        raise ParkPaperPreflightError("paper_preflight_stale", "Park preflight is missing or expired")
    instrument = artifact.get("instrument") if isinstance(artifact.get("instrument"), Mapping) else {}
    for field in ("instrument_id", "symbol", "venue", "provider"):
        if not str(instrument.get(field) or "").strip():
            raise ParkPaperPreflightError("paper_preflight_instrument_invalid", "Park instrument contract is incomplete")
    fee_model = artifact.get("fee_model") if isinstance(artifact.get("fee_model"), Mapping) else {}
    if fee_model.get("mode") != "paper_contract" or fee_model.get("real_money_eligible") is not False:
        raise ParkPaperPreflightError("paper_preflight_fee_invalid", "Park fee contract is invalid")
    for field in ("maker_fee_rate", "taker_fee_rate", "funding_rate", "funding_time", "observed_at"):
        if fee_model.get(field) in (None, ""):
            raise ParkPaperPreflightError("paper_preflight_fee_incomplete", f"Park fee contract missing {field}")


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
