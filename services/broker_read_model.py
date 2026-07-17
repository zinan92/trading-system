"""Safe, provider-neutral broker diagnostics for operator read models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.broker_port import broker_port_descriptor
from services.live_env import live_env_value_present


BROKER_READ_MODEL_SCHEMA = "broker-read-model-v1"

_SAFE_READINESS_FIELDS = (
    "provider",
    "mode",
    "status",
    "ready",
    "block_reason",
    "environment",
    "dry_run",
    "live_trading_enabled",
    "allowed_symbols",
    "checked_at",
)


def project_broker_read_model(
    adapter: Any,
    *,
    readiness: Mapping[str, Any] | None = None,
    strategy_id: str = "",
    profile: str = "",
) -> dict[str, Any]:
    """Expose descriptor/readiness facts without secrets or network access."""

    descriptor = getattr(adapter, "descriptor", None)
    if descriptor is None:
        descriptor = broker_port_descriptor(adapter)
    descriptor_dict = descriptor.to_dict() if hasattr(descriptor, "to_dict") else dict(descriptor)
    broker_config = getattr(adapter, "broker_config", {})
    config = broker_config if isinstance(broker_config, Mapping) else {}
    observed = readiness if isinstance(readiness, Mapping) else {}
    credential_env_names = list(descriptor_dict.get("credential_env_names") or [])
    credentials_present = all(live_env_value_present(str(name)) for name in credential_env_names)
    dry_run = bool(observed.get("dry_run", config.get("dry_run", True)))
    ready = observed.get("ready") if isinstance(observed.get("ready"), bool) else None
    live_enabled = bool(observed.get("live_trading_enabled", getattr(adapter, "live_trading_enabled", False)))
    provider = str(descriptor_dict.get("provider") or "")
    return {
        "schema_version": BROKER_READ_MODEL_SCHEMA,
        "adapter_name": str(descriptor_dict.get("adapter_name") or adapter.__class__.__name__),
        "provider": provider or None,
        "environment": str(descriptor_dict.get("environment") or "") or None,
        "display_label": _display_name(provider),
        "capabilities": list(descriptor_dict.get("capabilities") or []),
        "credential_env_names": credential_env_names,
        "credentials_present": credentials_present,
        "strategy_id": str(strategy_id or "") or None,
        "profile": str(profile or config.get("profile") or "") or None,
        "dry_run": dry_run,
        "ready": ready,
        "armed": bool(ready is True and not dry_run and live_enabled and credentials_present),
        "readiness": {
            key: _json_copy(observed[key])
            for key in _SAFE_READINESS_FIELDS
            if key in observed
        },
    }


def broker_reconciliation_status(report: Mapping[str, Any] | None) -> str:
    source = report if isinstance(report, Mapping) else {}
    status = str(source.get("status") or "missing").strip().lower()
    return status or "missing"


def broker_reconciliation_block_reason(report: Mapping[str, Any] | None) -> str:
    source = report if isinstance(report, Mapping) else {}
    for key in ("block_reason", "blocker", "reason", "error"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    status = broker_reconciliation_status(source)
    return "" if status in {"ok", "pass", "ready"} else f"broker reconciliation is {status}"


def _display_name(value: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return " ".join(part.capitalize() for part in normalized.replace("-", "_").split("_") if part)


def _json_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
