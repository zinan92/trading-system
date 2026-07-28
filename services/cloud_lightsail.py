"""Dry-run-first AWS Lightsail provisioning for the passive Cloud Paper host."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from services.journal_store import write_json


APPLICATION_PORTS = {8100, 8765, 8766}
ALLOWED_AWS_ACTIONS = {
    "get-regions",
    "get-blueprints",
    "get-bundles",
    "get-instance",
    "get-instance-state",
    "get-instance-port-states",
    "create-instances",
    "put-instance-public-ports",
}
REQUIRED_TEMPLATE_FIELDS = {
    "TRADING_SHA",
    "DATAFEED_SHA",
    "PYTHON_VERSION",
    "UV_VERSION",
    "UV_WHEEL_SHA256",
    "CLOUDFLARED_VERSION",
    "CLOUDFLARED_SHA256",
}


@dataclass(frozen=True)
class LightsailSelection:
    region: str
    availability_zone: str
    blueprint_id: str
    bundle_id: str
    monthly_price_usd: float
    cpu_count: int
    ram_gb: float


class CloudLightsailProvisioner:
    def __init__(
        self,
        *,
        repo_root: Path,
        output_root: Path,
        command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.output_root = Path(output_root)
        self.command_runner = command_runner or subprocess.run
        self.now = now or (lambda: datetime.now(timezone.utc))

    def catalog(self, *, region: str) -> dict[str, Any]:
        """Read provider capabilities without provisioning or returning identity."""
        return {
            "regions": self._aws_json(
                ["lightsail", "get-regions", "--include-availability-zones"],
                region=region,
            ).get("regions")
            or [],
            "blueprints": self._aws_json(
                ["lightsail", "get-blueprints", "--include-inactive"],
                region=region,
            ).get("blueprints")
            or [],
            "bundles": self._aws_json(
                ["lightsail", "get-bundles", "--include-inactive"],
                region=region,
            ).get("bundles")
            or [],
        }

    @staticmethod
    def select(spec: Mapping[str, Any], catalog: Mapping[str, Any]) -> LightsailSelection:
        region = str(spec.get("region") or "")
        os_spec = spec.get("os") or {}
        capacity = spec.get("capacity") or {}
        region_row = next(
            (
                row
                for row in catalog.get("regions") or []
                if isinstance(row, dict) and str(row.get("name") or "") == region
            ),
            None,
        )
        if region_row is None:
            raise ValueError("lightsail_region_unavailable")
        zones = sorted(
            str(row.get("zoneName") or "")
            for row in region_row.get("availabilityZones") or []
            if isinstance(row, dict) and str(row.get("state") or "").lower() == "available"
        )
        if not zones:
            raise ValueError("lightsail_availability_zone_unavailable")

        platform = str(os_spec.get("platform") or "")
        group = str(os_spec.get("group") or "").lower()
        version_prefix = str(os_spec.get("version_prefix") or "")
        blueprints = [
            row
            for row in catalog.get("blueprints") or []
            if isinstance(row, dict)
            and row.get("isActive") is True
            and str(row.get("type") or "").lower() == "os"
            and str(row.get("platform") or "") == platform
            and str(row.get("group") or "").lower() == group
            and str(row.get("version") or "").startswith(version_prefix)
        ]
        if not blueprints:
            raise ValueError("lightsail_ubuntu_blueprint_unavailable")
        blueprint = sorted(
            blueprints,
            key=lambda row: (str(row.get("version") or ""), str(row.get("blueprintId") or "")),
            reverse=True,
        )[0]

        minimum_vcpu = int(capacity.get("minimum_vcpu") or 0)
        minimum_ram = float(capacity.get("minimum_ram_gb") or 0)
        maximum_price = float(capacity.get("maximum_monthly_usd") or 0)
        bundles = [
            row
            for row in catalog.get("bundles") or []
            if isinstance(row, dict)
            and row.get("isActive") is True
            and int(row.get("cpuCount") or 0) >= minimum_vcpu
            and float(row.get("ramSizeInGb") or 0) >= minimum_ram
            and float(row.get("price") or float("inf")) <= maximum_price
            and platform in (row.get("supportedPlatforms") or [])
        ]
        if not bundles:
            raise ValueError("lightsail_bundle_capacity_unavailable")
        bundle = sorted(
            bundles,
            key=lambda row: (
                float(row.get("price") or float("inf")),
                float(row.get("ramSizeInGb") or 0),
                str(row.get("bundleId") or ""),
            ),
        )[0]
        return LightsailSelection(
            region=region,
            availability_zone=zones[0],
            blueprint_id=str(blueprint.get("blueprintId") or ""),
            bundle_id=str(bundle.get("bundleId") or ""),
            monthly_price_usd=float(bundle.get("price") or 0),
            cpu_count=int(bundle.get("cpuCount") or 0),
            ram_gb=float(bundle.get("ramSizeInGb") or 0),
        )

    def render_user_data(
        self,
        *,
        trading_sha: str,
        datafeed_sha: str,
        runtime: Mapping[str, Any],
    ) -> str:
        values = {
            "TRADING_SHA": _sha(trading_sha, "trading_system_sha"),
            "DATAFEED_SHA": _sha(datafeed_sha, "datafeed_sha"),
            "PYTHON_VERSION": _safe_version(runtime.get("python_version"), "python_version"),
            "UV_VERSION": _safe_version(runtime.get("uv_version"), "uv_version"),
            "UV_WHEEL_SHA256": _sha(runtime.get("uv_wheel_sha256"), "uv_wheel_sha256"),
            "CLOUDFLARED_VERSION": _safe_version(
                runtime.get("cloudflared_version"),
                "cloudflared_version",
            ),
            "CLOUDFLARED_SHA256": _sha(
                runtime.get("cloudflared_sha256"),
                "cloudflared_sha256",
            ),
        }
        if set(values) != REQUIRED_TEMPLATE_FIELDS:
            raise ValueError("lightsail_template_fields_incomplete")
        template = (
            self.repo_root / "deploy" / "cloud" / "cloud-init.sh.template"
        ).read_text(encoding="utf-8")
        for key, value in values.items():
            template = template.replace(f"@@{key}@@", value)
        if "@@" in template:
            raise ValueError("lightsail_template_placeholder_unresolved")
        if any(marker in template.lower() for marker in ("aws_secret", "private_key", "api_token")):
            raise ValueError("lightsail_user_data_secret_marker")
        return template

    def plan(
        self,
        *,
        spec: Mapping[str, Any],
        selection: LightsailSelection,
        operator_cidr: str,
        key_pair_name: str,
        user_data_path: Path,
    ) -> dict[str, Any]:
        cidr = _operator_cidr(operator_cidr)
        key_name = _safe_name(key_pair_name, "key_pair_name")
        instance_name = _safe_name(spec.get("instance_name"), "instance_name")
        if selection.region != str(spec.get("region") or ""):
            raise ValueError("lightsail_selection_region_mismatch")
        commands = [
            [
                "aws",
                "lightsail",
                "create-instances",
                "--region",
                selection.region,
                "--instance-names",
                instance_name,
                "--availability-zone",
                selection.availability_zone,
                "--blueprint-id",
                selection.blueprint_id,
                "--bundle-id",
                selection.bundle_id,
                "--key-pair-name",
                key_name,
                "--user-data",
                f"file://{Path(user_data_path)}",
            ],
            [
                "aws",
                "lightsail",
                "put-instance-public-ports",
                "--region",
                selection.region,
                "--instance-name",
                instance_name,
                "--port-infos",
                (
                    "fromPort=22,toPort=22,protocol=tcp,"
                    f"cidrs={cidr}"
                ),
            ],
        ]
        self._validate_commands(commands)
        return {
            "schema_version": "gridmind-lightsail-provision-plan-v1",
            "status": "ready",
            "provider": "aws_lightsail",
            "region": selection.region,
            "instance_name": instance_name,
            "selection": {
                "availability_zone": selection.availability_zone,
                "blueprint_id": selection.blueprint_id,
                "bundle_id": selection.bundle_id,
                "monthly_price_usd": selection.monthly_price_usd,
                "cpu_count": selection.cpu_count,
                "ram_gb": selection.ram_gb,
            },
            "network": {
                "ssh_cidr": cidr,
                "public_ports": [22],
                "public_application_ports": [],
            },
            "commands": commands,
            "paper_only": True,
            "scheduler_enabled": False,
            "strategy_actions": 0,
            "secret_values_recorded": False,
        }

    def apply(self, plan: Mapping[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        commands = [list(row) for row in plan.get("commands") or []]
        self._validate_commands(commands)
        if dry_run:
            return {
                "status": "dry_run",
                "provider": "aws_lightsail",
                "region": plan.get("region"),
                "instance_name": plan.get("instance_name"),
                "paper_only": True,
                "scheduler_enabled": False,
                "strategy_actions": 0,
                "secret_values_recorded": False,
            }
        for command in commands:
            result = self.command_runner(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                receipt = self._receipt(
                    status="blocked",
                    plan=plan,
                    reason=f"aws_{command[2].replace('-', '_')}_failed",
                    detail=f"returncode={result.returncode}",
                )
                self._write_receipt(receipt)
                return receipt
        receipt = self._receipt(
            status="provisioning",
            plan=plan,
            reason="",
            detail="Cloud-init and passive service receipts must pass before cutover.",
        )
        self._write_receipt(receipt)
        return receipt

    def verify_instance(self, *, region: str, instance_name: str) -> dict[str, Any]:
        name = _safe_name(instance_name, "instance_name")
        state = self._aws_json(
            ["lightsail", "get-instance-state", "--instance-name", name],
            region=region,
        )
        ports = self._aws_json(
            ["lightsail", "get-instance-port-states", "--instance-name", name],
            region=region,
        )
        open_ports = []
        for row in ports.get("portStates") or []:
            if not isinstance(row, dict) or str(row.get("state") or "").lower() != "open":
                continue
            start = int(row.get("fromPort") or 0)
            end = int(row.get("toPort") or start)
            open_ports.extend(range(start, end + 1))
        exposed = sorted(APPLICATION_PORTS.intersection(open_ports))
        status = "pass" if state.get("state", {}).get("name") == "running" and not exposed else "blocked"
        receipt = {
            "schema_version": "gridmind-lightsail-host-verification-v1",
            "checked_at": self.now().astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "provider": "aws_lightsail",
            "region": region,
            "instance_name": name,
            "instance_state": state.get("state", {}).get("name"),
            "public_application_ports": exposed,
            "paper_only": True,
            "scheduler_enabled": False,
            "secret_values_recorded": False,
        }
        self._write_receipt(receipt)
        return receipt

    def _aws_json(self, arguments: list[str], *, region: str) -> dict[str, Any]:
        command = ["aws", *arguments, "--region", region, "--output", "json", "--no-cli-pager"]
        self._validate_commands([command])
        result = self.command_runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "AWS_PAGER": ""},
        )
        if result.returncode != 0:
            raise RuntimeError(f"aws_{arguments[1].replace('-', '_')}_failed")
        payload = json.loads(str(result.stdout or "{}"))
        if not isinstance(payload, dict):
            raise ValueError("aws_response_not_object")
        return payload

    @staticmethod
    def _validate_commands(commands: list[list[str]]) -> None:
        if not commands:
            raise ValueError("lightsail_commands_missing")
        for command in commands:
            if len(command) < 3 or command[:2] != ["aws", "lightsail"]:
                raise ValueError("lightsail_command_not_allowlisted")
            if command[2] not in ALLOWED_AWS_ACTIONS:
                raise ValueError("lightsail_action_not_allowlisted")
            rendered = " ".join(command).lower()
            if any(marker in rendered for marker in ("secret", "private-key", "api-token")):
                raise ValueError("lightsail_command_secret_marker")

    def _receipt(
        self,
        *,
        status: str,
        plan: Mapping[str, Any],
        reason: str,
        detail: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": "gridmind-lightsail-provision-receipt-v1",
            "checked_at": self.now().astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "reason": reason,
            "detail": detail,
            "provider": "aws_lightsail",
            "region": plan.get("region"),
            "instance_name": plan.get("instance_name"),
            "plan_hash": _hash_json(plan),
            "paper_only": True,
            "scheduler_enabled": False,
            "strategy_actions": 0,
            "secret_values_recorded": False,
        }

    def _write_receipt(self, receipt: Mapping[str, Any]) -> None:
        write_json(
            self.output_root / "cloud" / "provision" / "provider_current.json",
            [dict(receipt)],
        )


def _sha(value: Any, field: str) -> str:
    rendered = str(value or "").lower()
    if len(rendered) != 64 and len(rendered) != 40:
        raise ValueError(f"{field}_invalid")
    if any(char not in "0123456789abcdef" for char in rendered):
        raise ValueError(f"{field}_invalid")
    return rendered


def _safe_version(value: Any, field: str) -> str:
    rendered = str(value or "")
    if not rendered or any(char not in "0123456789." for char in rendered):
        raise ValueError(f"{field}_invalid")
    return rendered


def _safe_name(value: Any, field: str) -> str:
    rendered = str(value or "")
    if not rendered or len(rendered) > 63:
        raise ValueError(f"{field}_invalid")
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in rendered):
        raise ValueError(f"{field}_invalid")
    return rendered


def _operator_cidr(value: str) -> str:
    network = ipaddress.ip_network(str(value), strict=True)
    if network.version != 4 or network.prefixlen < 24:
        raise ValueError("operator_cidr_must_be_restricted_ipv4")
    return str(network)


def _hash_json(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
