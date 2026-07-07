from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.connector_catalog import ConnectorCatalog
from services.journal_store import load_json, write_json


ONBOARDING_SCHEMA_VERSION = "connector-onboarding-dry-run-v1"

_SECRET_FIELD_NAMES = {
    "api_key",
    "api_secret",
    "credential_value",
    "credential_values",
    "license",
    "password",
    "private_key",
    "rsa_key",
    "secret",
    "secrets",
    "tiger_id",
    "token",
}
_SECRET_VALUE_MARKERS = ("BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY", "tiger_id=", "license=", "private_key=")


def reject_secret_payload(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            normalized = key_text.strip().lower().replace("-", "_")
            if normalized in _SECRET_FIELD_NAMES:
                raise ValueError(f"raw credential field is not allowed in onboarding dry-run: {path + key_text}")
            reject_secret_payload(child, path=f"{path}{key_text}.")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            reject_secret_payload(child, path=f"{path}{index}.")
        return
    if isinstance(value, str) and any(marker in value for marker in _SECRET_VALUE_MARKERS):
        raise ValueError("raw credential value is not allowed in onboarding dry-run")


class ConnectorOnboardingDryRun:
    """Validate a connector onboarding request without storing credentials.

    The payload may confirm that a credential will be supplied through an env var
    or owner-only file, but it must not include raw secret values.
    """

    def __init__(self, output_root: Path | None = None, *, config: dict | None = None, load_live_env: bool | None = None) -> None:
        self._explicit_config = config is not None
        self._load_live_env = (not self._explicit_config) if load_live_env is None else bool(load_live_env)
        self.config = config if config is not None else load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(self.config.get("output_root", "outputs"))

    def evaluate(self, payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        self._reject_secret_payload(payload)
        catalog = ConnectorCatalog(
            config=self.config,
            output_root=self.output_root,
            load_live_env=self._load_live_env,
        ).snapshot()
        connector_id = str(payload.get("connector_id") or "").strip()
        if not connector_id:
            raise ValueError("connector_id is required")
        connector = self._connector(catalog, connector_id)
        requested_roles = self._requested_roles(payload, connector)
        confirmations = payload.get("credential_confirmations", {})
        if confirmations is None:
            confirmations = {}
        if not isinstance(confirmations, dict):
            raise ValueError("credential_confirmations must be an object keyed by credential name")

        credential_checks = [self._credential_check(row, confirmations.get(str(row.get("name") or ""), {})) for row in connector.get("credential_requirements", []) if isinstance(row, dict)]
        role_checks = [self._role_check(role, connector) for role in requested_roles]
        blockers = [item for item in [*role_checks, *credential_checks] if item["status"] != "pass"]
        result = {
            "schema_version": ONBOARDING_SCHEMA_VERSION,
            "checked_at": self._now(),
            "status": "ready_for_operator_setup" if not blockers else "blocked",
            "connector_id": connector["id"],
            "label": connector.get("label", connector["id"]),
            "requested_roles": requested_roles,
            "environment": str(payload.get("environment") or (connector.get("environments") or [""])[0] or ""),
            "checks": [*role_checks, *credential_checks],
            "blockers": blockers,
            "credential_requirements": self._credential_requirements(connector),
            "next_actions": self._next_actions(connector, blockers),
            "safety": {
                "dry_run": True,
                "stores_credentials": False,
                "credential_values_exposed": False,
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "creates_order_control_endpoint": False,
            },
        }
        if persist:
            self._write(result)
        return result

    def _connector(self, catalog: dict[str, Any], connector_id: str) -> dict[str, Any]:
        for connector in catalog.get("connectors", []):
            if isinstance(connector, dict) and str(connector.get("id") or "") == connector_id:
                return connector
        known = [str(item.get("id") or "") for item in catalog.get("connectors", []) if isinstance(item, dict)]
        raise ValueError(f"unknown connector_id: {connector_id}; known connectors: {', '.join(known)}")

    def _requested_roles(self, payload: dict[str, Any], connector: dict[str, Any]) -> list[str]:
        roles = payload.get("requested_roles")
        if roles is None:
            roles = connector.get("roles", [])
        if not isinstance(roles, list) or not all(isinstance(item, str) for item in roles):
            raise ValueError("requested_roles must be a list of role strings")
        return [item for item in roles if item]

    def _role_check(self, role: str, connector: dict[str, Any]) -> dict[str, Any]:
        capabilities = {str(item.get("name") or ""): item for item in connector.get("capabilities", []) if isinstance(item, dict)}
        capability = capabilities.get(role, {})
        passed = bool(capability) and str(capability.get("status") or "") == "ready"
        readiness = capability.get("readiness", {}) if isinstance(capability.get("readiness"), dict) else {}
        acceptance = capability.get("acceptance", {}) if isinstance(capability.get("acceptance"), dict) else {}
        return {
            "name": f"role:{role}",
            "status": "pass" if passed else "fail",
            "summary": self._role_summary(role, passed, capability, readiness, acceptance),
            "evidence": {
                "role": role,
                "catalog_status": str(capability.get("status") or "missing"),
                "credential_required": capability.get("credential_required") is True,
                "requires_operator_authorization": capability.get("requires_operator_authorization") is True,
                "readiness": self._readiness_summary(readiness),
                "acceptance": self._acceptance_summary(acceptance),
            },
        }

    def _role_summary(
        self,
        role: str,
        passed: bool,
        capability: dict[str, Any],
        readiness: dict[str, Any],
        acceptance: dict[str, Any],
    ) -> str:
        if passed:
            return f"{role} is ready in the catalog."
        if role == "price_feed" and acceptance.get("status") == "pending_market_open":
            next_window = acceptance.get("next_trading_window", {}) if isinstance(acceptance.get("next_trading_window"), dict) else {}
            start = str(next_window.get("start") or "")
            return f"{role} is waiting for Tiger market-hours acceptance; rerun after {start or 'the next trading window'}."
        if role == "price_feed" and acceptance.get("status") == "blocked":
            return f"{role} is blocked by Tiger price-feed acceptance."
        if role == "price_feed" and readiness:
            return f"{role} is not ready; Tiger price-feed readiness status is {readiness.get('status', 'missing')}."
        status = str(capability.get("status") or "missing")
        return f"{role} is not ready in the catalog (status={status})."

    def _readiness_summary(self, readiness: dict[str, Any]) -> dict[str, Any]:
        if not readiness:
            return {"status": "missing", "ready_for_price_feed": False, "blocker_count": 0, "checked_at": ""}
        return {
            "status": str(readiness.get("status") or "missing"),
            "ready_for_price_feed": readiness.get("ready_for_price_feed") is True,
            "blocker_count": int(readiness.get("blocker_count") or 0),
            "checked_at": str(readiness.get("checked_at") or ""),
        }

    def _acceptance_summary(self, acceptance: dict[str, Any]) -> dict[str, Any]:
        if not acceptance:
            return {"status": "missing", "ready_for_price_feed": False, "exit_code": None, "blocker_count": 0}
        next_window = acceptance.get("next_trading_window", {}) if isinstance(acceptance.get("next_trading_window"), dict) else {}
        return {
            "status": str(acceptance.get("status") or "missing"),
            "ready_for_price_feed": acceptance.get("ready_for_price_feed") is True,
            "exit_code": acceptance.get("exit_code"),
            "blocker_count": int(acceptance.get("blocker_count") or 0),
            "operator_action": str(acceptance.get("operator_action") or ""),
            "operator_status": str(acceptance.get("operator_status") or ""),
            "operator_summary": str(acceptance.get("operator_summary") or ""),
            "next_command": str(acceptance.get("next_command") or ""),
            "next_trading_window": {
                "start": str(next_window.get("start") or ""),
                "end": str(next_window.get("end") or ""),
                "trading_date": str(next_window.get("trading_date") or ""),
            },
            "can_enable_broker_orders_from_this_gate": acceptance.get("can_enable_broker_orders_from_this_gate") is True,
            "checked_at": str(acceptance.get("checked_at") or ""),
        }

    def _credential_check(self, requirement: dict[str, Any], confirmation: Any) -> dict[str, Any]:
        if confirmation is None:
            confirmation = {}
        if not isinstance(confirmation, dict):
            raise ValueError(f"credential confirmation for {requirement.get('name')} must be an object")
        kind = str(requirement.get("kind") or "")
        if kind == "file_path":
            confirmed = (
                confirmation.get("present") is True
                and confirmation.get("file_exists") is True
                and confirmation.get("owner_only") is True
            )
            catalog_ready = requirement.get("present") is True and requirement.get("file_exists") is True and requirement.get("owner_only") is True
            passed = confirmed or catalog_ready
            need = "confirm env var points to an existing owner-only file"
        else:
            confirmed = confirmation.get("present") is True
            catalog_ready = requirement.get("present") is True
            passed = confirmed or catalog_ready
            need = "confirm env var is present"
        return {
            "name": f"credential:{requirement.get('name')}",
            "status": "pass" if passed else "fail",
            "summary": f"{requirement.get('name')} is ready." if passed else f"{requirement.get('name')} needs setup: {need}.",
            "evidence": {
                "name": requirement.get("name"),
                "kind": kind,
                "required_for": requirement.get("required_for", []),
                "catalog_present": requirement.get("present") is True,
                "catalog_file_exists": requirement.get("file_exists") is True,
                "catalog_owner_only": requirement.get("owner_only") is True,
                "confirmed_present": confirmation.get("present") is True,
                "confirmed_file_exists": confirmation.get("file_exists") is True,
                "confirmed_owner_only": confirmation.get("owner_only") is True,
                "value_exposed": False,
            },
        }

    def _credential_requirements(self, connector: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for item in connector.get("credential_requirements", []):
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "name": item.get("name"),
                    "kind": item.get("kind"),
                    "required_for": item.get("required_for", []),
                    "present": item.get("present") is True,
                    "file_exists": item.get("file_exists") is True,
                    "owner_only": item.get("owner_only") is True,
                    "value_exposed": False,
                }
            )
        return rows

    def _next_actions(self, connector: dict[str, Any], blockers: list[dict[str, Any]]) -> list[str]:
        if not blockers:
            return [
                f"{connector.get('label', connector.get('id'))} connector dry-run is ready.",
                "Keep credentials outside git and rerun the read-only catalog before enabling any network path.",
            ]
        return [str(item.get("summary") or "") for item in blockers if item.get("summary")]

    def _reject_secret_payload(self, value: Any, path: str = "") -> None:
        reject_secret_payload(value, path)

    def _write(self, payload: dict[str, Any]) -> None:
        base = self.output_root / "connector_onboarding"
        write_json(base / "current.json", [payload])
        connector_id = payload.get("connector_id") or "unknown"
        dated = base / f"{connector_id}.json"
        rows = load_json(dated)
        rows.append(payload)
        write_json(dated, rows)

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
