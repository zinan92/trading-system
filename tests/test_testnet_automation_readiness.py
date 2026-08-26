from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from services.testnet_automation_readiness import (
    TESTNET_AUTOMATION_READINESS_SCHEMA,
    TestnetAutomationReadiness,
    TestnetAutomationReadinessError,
)
from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from services.testnet_soak_readiness import TestnetSoakReadiness
from services.park_recording_track import REQUIRED_CATEGORIES
from services import testnet_soak_readiness as soak_module
from tests.test_testnet_soak_readiness import _evidence


def _observation(index: int, *, family: str = "dca", instrument: str = "BTC-USD-PERP", evidence=None, artifact_root: Path | None = None) -> dict:
    start = datetime(2026, 1, 1, 1, tzinfo=UTC) + timedelta(hours=12 * index)
    attestation = soak_module.current_source_attestation()
    evidence_payload = {key: dict(value) for key, value in (evidence or _evidence((start + timedelta(hours=6)).isoformat())).items()}
    if artifact_root is not None:
        for category, payload in evidence_payload.items():
            if not isinstance(payload, dict):
                payload = {}
            path = artifact_root / f"{family}-{index:02d}-{category}.json"
            artifact = {
                "artifact_kind": category,
                "strategy_session_id": f"session-{family}",
                "strategy_revision_id": f"revision-{family}-1",
                "plan_digest": "sha256:" + "a" * 64,
                "environment": "testnet",
                "broker_id": "hyperliquid",
                "release_sha": attestation["source_sha"],
                "account_fingerprint": "testnet-account-fingerprint",
                "window_index": index,
                "record_window_id": f"testnet-{family}-{index:02d}",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=12)).isoformat(),
                "status": payload.get("status", "pass"),
                "source": "runtime",
                "observed_at": (start + timedelta(hours=6)).isoformat(),
            }
            if category == "market_freshness_trust":
                artifact.update({"fresh": True, "trusted": True})
            path.write_text(__import__("json").dumps(artifact), encoding="utf-8")
            payload.update({
                "artifact_ref": str(path),
                "artifact_sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
                "artifact_kind": category,
            })
            evidence_payload[category] = payload
        for category in sorted(set(REQUIRED_CATEGORIES) - set(evidence_payload)):
            path = artifact_root / f"{family}-{index:02d}-{category}.json"
            artifact = {
                "artifact_kind": category,
                "strategy_session_id": f"session-{family}",
                "strategy_revision_id": f"revision-{family}-1",
                "plan_digest": "sha256:" + "a" * 64,
                "environment": "testnet",
                "broker_id": "hyperliquid",
                "release_sha": attestation["source_sha"],
                "account_fingerprint": "testnet-account-fingerprint",
                "window_index": index,
                "record_window_id": f"testnet-{family}-{index:02d}",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=12)).isoformat(),
                "status": "pass",
                "source": "runtime",
                "observed_at": (start + timedelta(hours=6)).isoformat(),
            }
            path.write_text(__import__("json").dumps(artifact), encoding="utf-8")
            evidence_payload[category] = {
                "status": "pass",
                "source": "runtime",
                "observed_at": (start + timedelta(hours=6)).isoformat(),
                "artifact_ref": str(path),
                "artifact_sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
                "artifact_kind": category,
            }
    evidence_payload["release_account_environment_identity"].update({"release_sha": attestation["source_sha"], "account_fingerprint": "testnet-account-fingerprint", "environment": "testnet", "broker_id": "hyperliquid"})
    return {
        "window_index": index,
        "record_window_id": f"testnet-{family}-{index:02d}",
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=12)).isoformat(),
        "strategy_family": family,
        "instrument_id": instrument,
        "strategy_session_id": f"session-{family}",
        "strategy_revision_id": f"revision-{family}-1",
        "plan_digest": "sha256:" + "a" * 64,
        "broker_id": "hyperliquid",
        "release_sha": attestation["source_sha"],
        "account_fingerprint": "testnet-account-fingerprint",
        "environment": "testnet",
        "source_attestation": attestation,
        "fresh": True,
        "trusted": True,
        "network_io": False,
        "real_money_eligible": False,
        "positions_open": 0,
        "evidence": evidence_payload,
        "execution_mutations": [],
    }


def test_two_window_readiness_is_ready_without_changing_14_window_default(tmp_path: Path, monkeypatch) -> None:
    attestation = {
        "status": "verified",
        "source_sha": "b" * 40,
        "source_tree_sha": "c" * 40,
        "tracked_tree_clean": True,
    }
    monkeypatch.setattr(soak_module, "current_source_attestation", lambda *_args, **_kwargs: attestation)
    readiness = TestnetAutomationReadiness(tmp_path / "outputs", strategy_family="dca")
    for index in range(2):
        row = readiness.record_window(_observation(index, artifact_root=tmp_path))
        assert row["status"] == "pass"

    receipt = readiness.finalize(now="2026-01-02T01:00:00+00:00")
    status = readiness.public_status(now="2026-01-02T01:00:00+00:00")

    assert receipt["status"] == "ready"
    assert receipt["required_window_count"] == 2
    assert receipt["required_day_count"] == 1
    assert receipt["live_enabled"] is False
    assert status["schema_version"] == TESTNET_AUTOMATION_READINESS_SCHEMA
    assert status["strategy_family"] == "dca"
    assert status["instrument_id"] == "BTC-USD-PERP"
    assert TestnetSoakReadiness(tmp_path / "other").window_count == 14


def test_readiness_blocks_evidence_gap_and_replay_conflict(tmp_path: Path, monkeypatch) -> None:
    attestation = {
        "status": "verified",
        "source_sha": "b" * 40,
        "source_tree_sha": "c" * 40,
        "tracked_tree_clean": True,
    }
    monkeypatch.setattr(soak_module, "current_source_attestation", lambda *_args, **_kwargs: attestation)
    readiness = TestnetAutomationReadiness(tmp_path / "outputs", strategy_family="grid")
    first = readiness.record_window(_observation(0, family="grid", artifact_root=tmp_path))
    replay = readiness.record_window(_observation(0, family="grid", artifact_root=tmp_path))
    assert replay == first

    evidence = _evidence()
    evidence["protection_coverage"] = {
        "status": "blocked",
        "source": "runtime",
        "observed_at": "2026-01-01T06:00:00+00:00",
    }
    blocked = readiness.record_window(_observation(1, family="grid", evidence=evidence, artifact_root=tmp_path))
    assert blocked["status"] == "blocked"
    assert readiness.finalize(now="2026-01-02T01:00:00+00:00")["status"] == "blocked"

    with pytest.raises(TestnetAutomationReadinessError, match="strategy_family_mismatch"):
        readiness.record_window(_observation(0, family="dca"))


def test_readiness_rejects_instrument_mismatch(tmp_path: Path) -> None:
    readiness = TestnetAutomationReadiness(tmp_path / "outputs", strategy_family="dca")
    with pytest.raises(TestnetAutomationReadinessError, match="instrument_id_mismatch"):
        readiness.record_window(_observation(0, instrument="ETH-USD-PERP"))


def test_coordinator_projects_soak_window_and_final_receipt(tmp_path: Path, monkeypatch) -> None:
    attestation = {
        "status": "verified",
        "source_sha": "b" * 40,
        "source_tree_sha": "c" * 40,
        "tracked_tree_clean": True,
    }
    monkeypatch.setattr(soak_module, "current_source_attestation", lambda *_args, **_kwargs: attestation)
    coordinator = TestnetAutomationCoordinator(tmp_path / "outputs", clock=lambda: "2026-01-01T01:00:00+00:00")
    coordinator.activate(
        {
            "strategy_family": "dca",
            "strategy_session_id": "session-dca",
            "strategy_revision_id": "revision-dca-1",
            "plan_digest": "sha256:" + "a" * 64,
            "account_fingerprint": "sha256:" + "d" * 64,
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "transport_profile": "hyperliquid-testnet-default",
            "instrument_id": "BTC-USD-PERP",
            "runtime_id": "runtime-dca",
            "release_sha": "b" * 40,
            "capability_revision": "hyperliquid-testnet-runtime-v1",
        },
        command_id="activate-dca",
    )
    for index in range(2):
        coordinator.record_soak_window(
            _observation(index, artifact_root=tmp_path),
            strategy_family="dca",
            instrument_id="BTC-USD-PERP",
            timestamp=(datetime(2026, 1, 1, 1, tzinfo=UTC) + timedelta(hours=12 * index)).isoformat(),
        )
    result = coordinator.finalize_soak(
        strategy_family="dca",
        instrument_id="BTC-USD-PERP",
        now="2026-01-02T01:00:00+00:00",
    )
    assert result["status"] == "soak_ready"
    assert result["soak"]["window_count"] == 2


def test_automation_soak_cli_records_one_window_without_network(tmp_path: Path, monkeypatch) -> None:
    attestation = {
        "status": "verified",
        "source_sha": "b" * 40,
        "source_tree_sha": "c" * 40,
        "tracked_tree_clean": True,
    }
    monkeypatch.setattr(soak_module, "current_source_attestation", lambda *_args, **_kwargs: attestation)
    observation = _observation(0, artifact_root=tmp_path, family="grid")
    observation_path = tmp_path / "observation.json"
    observation_path.write_text(json.dumps(observation), encoding="utf-8")
    artifacts = [
        f"{category}={payload['artifact_ref']}"
        for category, payload in observation["evidence"].items()
        if payload.get("artifact_kind")
    ]
    from pipelines.testnet_soak import main

    result = main(
        [
            "--output-root",
            str(tmp_path / "outputs"),
            "--observation",
            str(observation_path),
            "--automation",
            "--strategy-family",
            "grid",
            "--instrument-id",
            "BTC-USD-PERP",
            *sum((["--artifact", item] for item in artifacts), []),
        ]
    )
    assert result == 0
