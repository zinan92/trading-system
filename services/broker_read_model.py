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
    asset: str = "",
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
    environment = str(descriptor_dict.get("environment") or "")
    instrument_map = config.get("instrument_map") if isinstance(config.get("instrument_map"), Mapping) else {}
    symbol = instrument_map.get(asset) if asset else None
    armed = bool(ready is True and not dry_run and live_enabled and credentials_present)
    return {
        "schema_version": BROKER_READ_MODEL_SCHEMA,
        "adapter": str(descriptor_dict.get("adapter_name") or adapter.__class__.__name__),
        "adapter_name": str(descriptor_dict.get("adapter_name") or adapter.__class__.__name__),
        "provider": provider or None,
        "environment": environment or None,
        "mode": f"{environment}_broker_port" if environment else "broker_port",
        "display_label": _display_name(provider),
        "capabilities": list(descriptor_dict.get("capabilities") or []),
        "credential_env_names": credential_env_names,
        "credentials_present": credentials_present,
        "strategy_id": str(strategy_id or "") or None,
        "profile": str(profile or config.get("profile") or "") or None,
        "endpoint": str(config.get("base_url") or "") or None,
        "symbol": str(symbol or "") or None,
        "dry_run": dry_run,
        "ready": ready,
        "armed": armed,
        "live_endpoint_allowed": bool(environment == "live" and armed),
        "readiness": {
            key: _json_copy(observed[key])
            for key in _SAFE_READINESS_FIELDS
            if key in observed
        },
    }


def broker_reconciliation_status(report: Mapping[str, Any] | None) -> str:
    source = report if isinstance(report, Mapping) else {}
    if not source:
        return ""
    if source.get("confirmation_status") == "cannot_confirm":
        return "cannot_confirm"
    if source.get("suspected_naked_position"):
        return "naked_position_suspected"
    if source.get("reconciled"):
        return "pass"
    if source.get("error"):
        return "error"
    if source.get("drifts") or source.get("drift_count"):
        return "drift"
    status = str(source.get("status") or "missing").strip().lower()
    return status or "missing"


def broker_reconciliation_block_reason(report: Mapping[str, Any] | None) -> str:
    source = report if isinstance(report, Mapping) else {}
    if source.get("suspected_naked_position"):
        detail = source.get("escalation_action") or source.get("reason_code") or "unknown"
        return f"broker suspected naked position: {detail}"
    if source.get("confirmation_status") == "cannot_confirm":
        return f"broker reconciliation cannot confirm venue state: {source.get('error') or 'unknown'}"
    if source.get("error"):
        return f"broker reconciliation failed: {source['error']}"
    drifts = source.get("drifts") if isinstance(source.get("drifts"), list) else []
    if drifts:
        reasons = sorted(
            {
                str(item.get("reason") or "reconciliation drift")
                for item in drifts
                if isinstance(item, Mapping)
            }
        )
        return "broker reconciliation drift: " + ("; ".join(reasons) if reasons else "unknown drift")
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
