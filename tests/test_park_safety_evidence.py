from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.park_cutover_guard import REQUIRED_SAFETY_GATES, evaluate_park_cutover
from services.park_safety_evidence import build_park_safety_evidence


class _ReadOnlyPaperAdapter:
    name = "nautilus_paper"

    def __init__(self) -> None:
        self.submit_calls: list[dict] = []
        self.cancel_calls: list[dict] = []

    def snapshot(self, _cycle_id: str) -> dict:
        return {
            "capabilities": {
                "paper_only": True,
                "immutable_fill_guard": True,
            },
        }


def _config() -> dict:
    return {
        "feature_enabled": True,
        "execution_track_count": 1,
        "runtime_mode": "paper_only",
        "control_plane": "telegram",
        "autonomous": False,
        "shadow_mutation": False,
        "feishu_control": False,
        "supervisor_execution_profile": "fail_closed",
        "execution_engine": {
            "authoritative": "nautilus_paper",
            "real_money_eligible": False,
        },
        "paper_execution": {"instrument": {}, "paper_fee_model": {}},
    }


def _patch_read_only_gates(monkeypatch, source: dict, now: datetime) -> None:
    class FakePredeploy:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self) -> dict:
            return {
                "status": "pass",
                "checked_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=15)).isoformat(),
            }

    class FakeRelease:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self) -> dict:
            return {
                "ok": True,
                "source_sha": source["source_sha"],
                "source_tree_sha": source["source_tree_sha"],
                "blocker": "",
            }

    class FakeBoot:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self, _service: str) -> dict:
            return {
                "ok": True,
                "source_sha": source["source_sha"],
                "source_tree_sha": source["source_tree_sha"],
                "blocker": "",
                "boot_receipt": {"status": "pass"},
            }

    monkeypatch.setattr("services.park_safety_evidence.PaperPredeployGate", FakePredeploy)
    monkeypatch.setattr("services.park_safety_evidence.PaperReleaseReceiptGate", FakeRelease)
    monkeypatch.setattr("services.park_safety_evidence.PaperServiceBootGate", FakeBoot)
    monkeypatch.setattr(
        "services.park_safety_evidence.validate_park_paper_preflight",
        lambda *_args, **_kwargs: None,
    )


def test_source_bound_evidence_passes_without_mutation(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)
    source = {
        "source_sha": "a" * 40,
        "source_tree_sha": "b" * 40,
        "tracked_tree_clean": True,
    }
    _patch_read_only_gates(monkeypatch, source, now)
    adapter = _ReadOnlyPaperAdapter()
    artifact = build_park_safety_evidence(
        tmp_path,
        config=_config(),
        repo_root=tmp_path,
        adapter=adapter,
        now=now,
        source_attestation_reader=lambda: source,
    )

    assert artifact["status"] == "pass"
    assert artifact["release_sha"] == source["source_sha"]
    assert artifact["source_tree_sha"] == source["source_tree_sha"]
    assert artifact["immutable_fill"] is True
    assert artifact["release_sha_ownership"] is True
    assert artifact["boot"] is True
    assert artifact["supervisor_fail_closed"] is True
    assert artifact["operations"]["mutations"] == []
    assert adapter.submit_calls == []
    assert adapter.cancel_calls == []

    runtime_evidence = {
        **artifact,
        "trusted_market": True,
        "tick_freshness": True,
        "stale_cycle_state": True,
        "reconciliation": True,
        "park_risk_confirmation": True,
    }
    result = evaluate_park_cutover(_config(), safety_evidence=runtime_evidence)
    assert result["status"] == "pass"
    assert result["mutations"] == []


def test_missing_adapter_capability_stays_blocked(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)
    source = {
        "source_sha": "a" * 40,
        "source_tree_sha": "b" * 40,
        "tracked_tree_clean": True,
    }
    _patch_read_only_gates(monkeypatch, source, now)
    artifact = build_park_safety_evidence(
        tmp_path,
        config=_config(),
        repo_root=tmp_path,
        now=now,
        source_attestation_reader=lambda: source,
    )

    assert artifact["status"] == "blocked"
    assert artifact["immutable_fill"] is False
    assert "immutable_fill_capability_missing" in artifact["blockers"]
    assert "safety_gate_failed:immutable_fill" in artifact["blockers"]


def test_cutover_rejects_expired_evidence() -> None:
    now = datetime.now(timezone.utc)
    evidence = {
        "release_sha": "a" * 40,
        "boot_verified": True,
        "status": "pass",
        "expires_at": (now - timedelta(seconds=1)).isoformat(),
        **{gate: True for gate in REQUIRED_SAFETY_GATES},
    }
    result = evaluate_park_cutover(
        {**_config(), "feature_enabled": True},
        safety_evidence=evidence,
    )
    assert result["status"] == "blocked"
    assert "safety_evidence_stale" in result["blockers"]
