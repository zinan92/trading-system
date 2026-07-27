from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from services.cloud_lightsail import (
    CloudLightsailProvisioner,
    LightsailSelection,
)


TRADING_SHA = "a" * 40
DATAFEED_SHA = "b" * 40


def _spec() -> dict:
    return {
        "region": "ap-southeast-1",
        "instance_name": "gridmind-paper-sg",
        "os": {
            "group": "ubuntu",
            "version_prefix": "24.04",
            "platform": "LINUX_UNIX",
        },
        "capacity": {
            "minimum_vcpu": 2,
            "minimum_ram_gb": 2,
            "maximum_monthly_usd": 12,
        },
        "runtime": {
            "python_version": "3.13.7",
            "uv_version": "0.9.27",
            "uv_wheel_sha256": "c" * 64,
            "cloudflared_version": "2026.7.1",
            "cloudflared_sha256": "d" * 64,
        },
    }


def _catalog() -> dict:
    return {
        "regions": [
            {
                "name": "ap-southeast-1",
                "availabilityZones": [
                    {"zoneName": "ap-southeast-1b", "state": "available"},
                    {"zoneName": "ap-southeast-1a", "state": "available"},
                ],
            }
        ],
        "blueprints": [
            {
                "blueprintId": "ubuntu_22_04",
                "group": "ubuntu",
                "version": "22.04",
                "platform": "LINUX_UNIX",
                "type": "os",
                "isActive": True,
            },
            {
                "blueprintId": "ubuntu_24_04",
                "group": "ubuntu",
                "version": "24.04.2",
                "platform": "LINUX_UNIX",
                "type": "os",
                "isActive": True,
            },
        ],
        "bundles": [
            {
                "bundleId": "small",
                "cpuCount": 2,
                "ramSizeInGb": 1,
                "price": 7,
                "supportedPlatforms": ["LINUX_UNIX"],
                "isActive": True,
            },
            {
                "bundleId": "medium",
                "cpuCount": 2,
                "ramSizeInGb": 2,
                "price": 12,
                "supportedPlatforms": ["LINUX_UNIX"],
                "isActive": True,
            },
            {
                "bundleId": "large",
                "cpuCount": 2,
                "ramSizeInGb": 4,
                "price": 24,
                "supportedPlatforms": ["LINUX_UNIX"],
                "isActive": True,
            },
        ],
    }


def _provisioner(tmp_path: Path, runner=None) -> CloudLightsailProvisioner:
    repo = tmp_path / "repo"
    template = repo / "deploy" / "cloud" / "cloud-init.sh.template"
    template.parent.mkdir(parents=True)
    template.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                'TRADING="@@TRADING_SHA@@"',
                'DATAFEED="@@DATAFEED_SHA@@"',
                'PYTHON="@@PYTHON_VERSION@@"',
                'UV="@@UV_VERSION@@"',
                'UV_SHA="@@UV_WHEEL_SHA256@@"',
                'CF="@@CLOUDFLARED_VERSION@@"',
                'CF_SHA="@@CLOUDFLARED_SHA256@@"',
                "systemctl is-enabled gridmind-live-tick.timer && exit 1 || true",
            )
        ),
        encoding="utf-8",
    )
    return CloudLightsailProvisioner(
        repo_root=repo,
        output_root=tmp_path / "outputs",
        command_runner=runner,
    )


def test_selects_lowest_eligible_active_bundle_and_ubuntu(tmp_path: Path) -> None:
    selected = _provisioner(tmp_path).select(_spec(), _catalog())

    assert selected == LightsailSelection(
        region="ap-southeast-1",
        availability_zone="ap-southeast-1a",
        blueprint_id="ubuntu_24_04",
        bundle_id="medium",
        monthly_price_usd=12,
        cpu_count=2,
        ram_gb=2,
    )


def test_selection_blocks_missing_capacity_or_region(tmp_path: Path) -> None:
    provisioner = _provisioner(tmp_path)
    bad = _catalog()
    bad["bundles"] = []

    with pytest.raises(ValueError, match="bundle_capacity"):
        provisioner.select(_spec(), bad)
    missing = _catalog()
    missing["regions"] = []
    with pytest.raises(ValueError, match="region_unavailable"):
        provisioner.select(_spec(), missing)


def test_render_is_source_bound_and_secret_free(tmp_path: Path) -> None:
    provisioner = _provisioner(tmp_path)

    rendered = provisioner.render_user_data(
        trading_sha=TRADING_SHA,
        datafeed_sha=DATAFEED_SHA,
        runtime=_spec()["runtime"],
    )

    assert TRADING_SHA in rendered
    assert DATAFEED_SHA in rendered
    assert "@@" not in rendered
    assert "gridmind-live-tick.timer" in rendered
    assert "api_token" not in rendered.lower()


def test_checked_in_template_hands_preflight_receipts_back_to_service_user() -> None:
    template = (
        Path(__file__).parents[1] / "deploy" / "cloud" / "cloud-init.sh.template"
    ).read_text(encoding="utf-8")

    preflight = template.index("pipelines.cloud_paper_preflight --json")
    ownership = template.index(
        "chown -R gridmind:gridmind /var/lib/gridmind/outputs",
        preflight,
    )
    dashboard = template.index("--action activate-dashboard --apply", ownership)
    assert preflight < ownership < dashboard


def test_render_rejects_source_mismatch_and_unresolved_fields(tmp_path: Path) -> None:
    provisioner = _provisioner(tmp_path)

    with pytest.raises(ValueError, match="trading_system_sha_invalid"):
        provisioner.render_user_data(
            trading_sha="main",
            datafeed_sha=DATAFEED_SHA,
            runtime=_spec()["runtime"],
        )
    template = (
        provisioner.repo_root / "deploy" / "cloud" / "cloud-init.sh.template"
    )
    template.write_text(template.read_text() + "\n@@UNKNOWN@@\n")
    with pytest.raises(ValueError, match="placeholder_unresolved"):
        provisioner.render_user_data(
            trading_sha=TRADING_SHA,
            datafeed_sha=DATAFEED_SHA,
            runtime=_spec()["runtime"],
        )


def test_plan_exposes_only_restricted_ssh_and_keeps_scheduler_off(tmp_path: Path) -> None:
    provisioner = _provisioner(tmp_path)
    selected = provisioner.select(_spec(), _catalog())
    plan = provisioner.plan(
        spec=_spec(),
        selection=selected,
        operator_cidr="203.0.113.9/32",
        key_pair_name="gridmind-paper",
        user_data_path=tmp_path / "cloud-init.sh",
    )

    assert plan["network"]["public_ports"] == [22]
    assert plan["network"]["public_application_ports"] == []
    assert plan["scheduler_enabled"] is False
    assert all(command[:2] == ["aws", "lightsail"] for command in plan["commands"])
    assert not any(
        str(port) in json.dumps(plan["commands"])
        for port in (8100, 8765, 8766)
    )


def test_plan_rejects_broad_operator_cidr(tmp_path: Path) -> None:
    provisioner = _provisioner(tmp_path)
    with pytest.raises(ValueError, match="operator_cidr"):
        provisioner.plan(
            spec=_spec(),
            selection=provisioner.select(_spec(), _catalog()),
            operator_cidr="0.0.0.0/0",
            key_pair_name="gridmind-paper",
            user_data_path=tmp_path / "cloud-init.sh",
        )


def test_apply_is_dry_run_by_default(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    provisioner = _provisioner(tmp_path, runner=runner)
    plan = provisioner.plan(
        spec=_spec(),
        selection=provisioner.select(_spec(), _catalog()),
        operator_cidr="203.0.113.9/32",
        key_pair_name="gridmind-paper",
        user_data_path=tmp_path / "cloud-init.sh",
    )

    result = provisioner.apply(plan)

    assert result["status"] == "dry_run"
    assert result["scheduler_enabled"] is False
    assert calls == []


def test_failed_provider_command_writes_secret_free_blocked_receipt(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 2, "", "sensitive provider text")

    provisioner = _provisioner(tmp_path, runner=runner)
    plan = provisioner.plan(
        spec=_spec(),
        selection=provisioner.select(_spec(), _catalog()),
        operator_cidr="203.0.113.9/32",
        key_pair_name="gridmind-paper",
        user_data_path=tmp_path / "cloud-init.sh",
    )

    result = provisioner.apply(plan, dry_run=False)

    assert result["status"] == "blocked"
    assert result["secret_values_recorded"] is False
    assert "sensitive" not in json.dumps(result)
    assert len(calls) == 1
    receipt = json.loads(
        (tmp_path / "outputs" / "cloud" / "provision" / "provider_current.json").read_text()
    )[-1]
    assert receipt["status"] == "blocked"


def test_verify_blocks_public_application_ports(tmp_path: Path) -> None:
    responses = iter(
        (
            {"state": {"name": "running"}},
            {
                "portStates": [
                    {"fromPort": 22, "toPort": 22, "protocol": "tcp", "state": "open"},
                    {"fromPort": 8765, "toPort": 8765, "protocol": "tcp", "state": "open"},
                ]
            },
        )
    )

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, json.dumps(next(responses)), "")

    result = _provisioner(tmp_path, runner=runner).verify_instance(
        region="ap-southeast-1",
        instance_name="gridmind-paper-sg",
    )

    assert result["status"] == "blocked"
    assert result["public_application_ports"] == [8765]
