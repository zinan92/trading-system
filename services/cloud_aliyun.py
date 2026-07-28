"""Source-bound Alibaba Cloud SWAS passive-host deployment package."""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping


REQUIRED_TEMPLATE_FIELDS = {
    "TRADING_SHA",
    "DATAFEED_SHA",
    "TRADING_ARCHIVE_SHA256",
    "DATAFEED_ARCHIVE_SHA256",
    "TRADING_BUNDLE_SHA256",
    "DATAFEED_BUNDLE_SHA256",
    "PYTHON_VERSION",
    "UV_VERSION",
    "UV_WHEEL_SHA256",
    "CLOUDFLARED_VERSION",
    "CLOUDFLARED_SHA256",
}


class CloudAliyunProvisioner:
    def __init__(self, *, repo_root: Path) -> None:
        self.repo_root = Path(repo_root)

    @staticmethod
    def validate_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
        if spec.get("provider") != "aliyun_swas":
            raise ValueError("aliyun_provider_invalid")
        if spec.get("region") != "ap-southeast-1":
            raise ValueError("aliyun_region_invalid")
        if spec.get("instance_plan") != "swas.s.c2m2s40b1.linux":
            raise ValueError("aliyun_instance_plan_invalid")
        os_spec = spec.get("os") or {}
        if (
            os_spec.get("distribution") != "ubuntu"
            or os_spec.get("version") != "24.04"
            or os_spec.get("architecture") != "x86_64"
        ):
            raise ValueError("aliyun_os_invalid")
        capacity = spec.get("capacity") or {}
        if (
            int(capacity.get("vcpu") or 0) < 2
            or int(capacity.get("ram_gib") or 0) < 2
            or int(capacity.get("system_disk_gib") or 0) < 40
        ):
            raise ValueError("aliyun_capacity_below_minimum")
        network = spec.get("network") or {}
        if network.get("public_application_ports") != []:
            raise ValueError("aliyun_public_application_ports_forbidden")
        if sorted(network.get("loopback_application_ports") or []) != [8100, 8765, 8766]:
            raise ValueError("aliyun_loopback_ports_invalid")
        if spec.get("paper_only") is not True or spec.get("scheduler_enabled") is not False:
            raise ValueError("aliyun_paper_safety_invalid")
        return json.loads(json.dumps(spec))

    @staticmethod
    def source_archive(*, repo: Path, revision: str, destination: Path) -> str:
        revision = _sha(revision, "source_revision")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        prefix = f"{Path(repo).name}/"
        result = subprocess.run(
            ["git", "archive", "--format=tar", f"--prefix={prefix}", revision],
            cwd=str(repo),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("source_archive_failed")
        with destination.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
                zipped.write(result.stdout)
        return _file_sha256(destination)

    @staticmethod
    def source_bundle(*, repo: Path, ref: str, destination: Path) -> str:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "bundle", "create", str(destination), ref],
            cwd=str(repo),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("source_bundle_failed")
        return _file_sha256(destination)

    def render_bootstrap(
        self,
        *,
        trading_sha: str,
        datafeed_sha: str,
        trading_archive_sha256: str,
        datafeed_archive_sha256: str,
        trading_bundle_sha256: str,
        datafeed_bundle_sha256: str,
        runtime: Mapping[str, Any],
    ) -> str:
        values = {
            "TRADING_SHA": _sha(trading_sha, "trading_system_sha"),
            "DATAFEED_SHA": _sha(datafeed_sha, "datafeed_sha"),
            "TRADING_ARCHIVE_SHA256": _sha(
                trading_archive_sha256, "trading_archive_sha256"
            ),
            "DATAFEED_ARCHIVE_SHA256": _sha(
                datafeed_archive_sha256, "datafeed_archive_sha256"
            ),
            "TRADING_BUNDLE_SHA256": _sha(
                trading_bundle_sha256, "trading_bundle_sha256"
            ),
            "DATAFEED_BUNDLE_SHA256": _sha(
                datafeed_bundle_sha256, "datafeed_bundle_sha256"
            ),
            "PYTHON_VERSION": _version(runtime.get("python_version"), "python_version"),
            "UV_VERSION": _version(runtime.get("uv_version"), "uv_version"),
            "UV_WHEEL_SHA256": _sha(runtime.get("uv_wheel_sha256"), "uv_wheel_sha256"),
            "CLOUDFLARED_VERSION": _version(
                runtime.get("cloudflared_version"), "cloudflared_version"
            ),
            "CLOUDFLARED_SHA256": _sha(
                runtime.get("cloudflared_sha256"), "cloudflared_sha256"
            ),
        }
        if set(values) != REQUIRED_TEMPLATE_FIELDS:
            raise ValueError("aliyun_template_fields_incomplete")
        template = (
            self.repo_root / "deploy" / "cloud" / "aliyun-bootstrap.sh.template"
        ).read_text(encoding="utf-8")
        for key, value in values.items():
            template = template.replace(f"@@{key}@@", value)
        if "@@" in template:
            raise ValueError("aliyun_template_placeholder_unresolved")
        return template

    @staticmethod
    def plan(
        *,
        spec: Mapping[str, Any],
        host: str,
        ssh_key: Path,
        render_dir: Path,
    ) -> dict[str, Any]:
        CloudAliyunProvisioner.validate_spec(spec)
        host = _host(host)
        ssh_key = Path(ssh_key).expanduser().resolve()
        render_dir = Path(render_dir).resolve()
        commands = [
            [
                "scp",
                "-i",
                str(ssh_key),
                str(render_dir / "trading-system.tar.gz"),
                str(render_dir / "datafeed.tar.gz"),
                str(render_dir / "trading-system.bundle"),
                str(render_dir / "datafeed.bundle"),
                str(render_dir / "bootstrap.sh"),
                f"root@{host}:/tmp/",
            ],
            [
                "ssh",
                "-i",
                str(ssh_key),
                f"root@{host}",
                "chmod 700 /tmp/bootstrap.sh && /tmp/bootstrap.sh",
            ],
        ]
        return {
            "schema_version": "gridmind-aliyun-deploy-plan-v1",
            "status": "ready",
            "provider": "aliyun_swas",
            "region": spec["region"],
            "host": host,
            "commands": commands,
            "paper_only": True,
            "scheduler_enabled": False,
        }

    @staticmethod
    def apply(plan: Mapping[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        if plan.get("provider") != "aliyun_swas":
            raise ValueError("aliyun_plan_provider_invalid")
        if plan.get("paper_only") is not True or plan.get("scheduler_enabled") is not False:
            raise ValueError("aliyun_plan_safety_invalid")
        commands = plan.get("commands") or []
        if len(commands) != 2:
            raise ValueError("aliyun_plan_commands_invalid")
        if dry_run:
            return {
                "schema_version": "gridmind-aliyun-apply-v1",
                "status": "dry_run",
                "provider": "aliyun_swas",
                "command_count": len(commands),
                "paper_only": True,
                "scheduler_enabled": False,
            }
        for command in commands:
            subprocess.run(list(command), check=True)
        return {
            "schema_version": "gridmind-aliyun-apply-v1",
            "status": "pass",
            "provider": "aliyun_swas",
            "command_count": len(commands),
            "paper_only": True,
            "scheduler_enabled": False,
        }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha(value: Any, name: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) not in {40, 64}:
        raise ValueError(f"{name}_invalid")
    if any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{name}_invalid")
    return text


def _version(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text or any(char not in "0123456789." for char in text):
        raise ValueError(f"{name}_invalid")
    return text


def _host(value: Any) -> str:
    text = str(value or "").strip()
    if not text or any(char not in "0123456789abcdefABCDEF:.-" for char in text):
        raise ValueError("aliyun_host_invalid")
    return text
