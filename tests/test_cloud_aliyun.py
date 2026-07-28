from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from services.cloud_aliyun import CloudAliyunProvisioner


def _spec() -> dict:
    return {
        "provider": "aliyun_swas",
        "region": "ap-southeast-1",
        "instance_plan": "swas.s.c2m2s40b1.linux",
        "os": {
            "distribution": "ubuntu",
            "version": "24.04",
            "architecture": "x86_64",
        },
        "capacity": {"vcpu": 2, "ram_gib": 2, "system_disk_gib": 40},
        "network": {
            "public_application_ports": [],
            "loopback_application_ports": [8100, 8765, 8766],
        },
        "paper_only": True,
        "scheduler_enabled": False,
    }


def test_spec_keeps_application_ports_private() -> None:
    assert CloudAliyunProvisioner.validate_spec(_spec())["network"][
        "public_application_ports"
    ] == []
    exposed = _spec()
    exposed["network"]["public_application_ports"] = [8765]
    with pytest.raises(ValueError, match="public_application_ports_forbidden"):
        CloudAliyunProvisioner.validate_spec(exposed)


def test_source_archive_is_repeatable_and_bound_to_revision(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "value.txt").write_text("bound\n", encoding="utf-8")
    subprocess.run(["git", "add", "value.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "source"], cwd=repo, check=True, capture_output=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    first_sha = CloudAliyunProvisioner.source_archive(
        repo=repo, revision=revision, destination=first
    )
    second_sha = CloudAliyunProvisioner.source_archive(
        repo=repo, revision=revision, destination=second
    )
    assert first_sha == second_sha
    assert first.read_bytes() == second.read_bytes()


def test_source_bundle_is_repeatable(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "value.txt").write_text("bound\n", encoding="utf-8")
    subprocess.run(["git", "add", "value.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "source"], cwd=repo, check=True, capture_output=True)
    first = tmp_path / "first.bundle"
    second = tmp_path / "second.bundle"
    assert CloudAliyunProvisioner.source_bundle(
        repo=repo, ref="HEAD", destination=first
    ) == CloudAliyunProvisioner.source_bundle(
        repo=repo, ref="HEAD", destination=second
    )
    assert first.read_bytes() == second.read_bytes()


def test_dry_run_does_not_execute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan = CloudAliyunProvisioner.plan(
        spec=_spec(),
        host="203.0.113.10",
        ssh_key=tmp_path / "id_ed25519",
        render_dir=tmp_path,
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    result = CloudAliyunProvisioner.apply(plan, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["scheduler_enabled"] is False


def test_plan_upload_names_match_bootstrap_contract(tmp_path: Path) -> None:
    plan = CloudAliyunProvisioner.plan(
        spec=_spec(),
        host="203.0.113.10",
        ssh_key=tmp_path / "id_ed25519",
        render_dir=tmp_path,
    )
    upload = plan["commands"][0]
    assert str(tmp_path / "trading-system.tar.gz") in upload
    assert str(tmp_path / "datafeed.tar.gz") in upload
    assert str(tmp_path / "trading-system.bundle") in upload
    assert str(tmp_path / "datafeed.bundle") in upload
    bootstrap = (
        Path(__file__).parents[1] / "deploy/cloud/aliyun-bootstrap.sh.template"
    ).read_text(encoding="utf-8")
    assert "TRADING_ARCHIVE=/tmp/trading-system.tar.gz" in bootstrap
    assert "DATAFEED_ARCHIVE=/tmp/datafeed.tar.gz" in bootstrap


def test_bootstrap_keeps_valid_uv_wheel_filename() -> None:
    bootstrap = (
        Path(__file__).parents[1] / "deploy/cloud/aliyun-bootstrap.sh.template"
    ).read_text(encoding="utf-8")
    assert (
        'UV_WHEEL="/tmp/uv-${UV_VERSION}-py3-none-'
        'manylinux_2_17_x86_64.manylinux2014_x86_64.whl"'
    ) in bootstrap


def test_bootstrap_uses_datafeed_health_contract() -> None:
    bootstrap = (
        Path(__file__).parents[1] / "deploy/cloud/aliyun-bootstrap.sh.template"
    ).read_text(encoding="utf-8")
    assert "http://127.0.0.1:8100/api/health" in bootstrap
    assert "http://127.0.0.1:8100/health" not in bootstrap


def test_plan_rejects_scheduler_activation(tmp_path: Path) -> None:
    spec = _spec()
    spec["scheduler_enabled"] = True
    with pytest.raises(ValueError, match="paper_safety_invalid"):
        CloudAliyunProvisioner.plan(
            spec=spec,
            host="203.0.113.10",
            ssh_key=tmp_path / "id_ed25519",
            render_dir=tmp_path,
        )
