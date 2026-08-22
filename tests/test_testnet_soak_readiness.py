from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

from services.testnet_soak_readiness import TestnetSoakReadiness
from services.park_recording_track import REQUIRED_CATEGORIES
from services.paper_release_receipt import current_source_attestation


def _evidence(observed_at: str = "2026-01-01T01:00:00+00:00") -> dict:
    evidence = {key: {"status": "pass", "source": "testnet-runtime-receipt", "observed_at": observed_at, "artifact_ref": f"outputs/testnet/{key}.json"} for key in (
        "orders_fills_positions_reconciliation",
        "protection_coverage",
        "capability_status",
        "market_freshness_trust",
        "runtime_health",
        "retry_outcomes",
        "release_account_environment_identity",
        "recording_package",
    )}
    evidence["release_account_environment_identity"].update({"release_sha": "b" * 40, "account_fingerprint": "testnet-account-fingerprint", "environment": "testnet", "broker_id": "hyperliquid"})
    evidence["market_freshness_trust"].update({"fresh": True, "trusted": True})
    for category in sorted(set(REQUIRED_CATEGORIES) | {"orders_fills_positions_reconciliation", "protection_coverage", "capability_status", "market_freshness_trust", "runtime_health", "retry_outcomes", "release_account_environment_identity", "recording_package"}):
        evidence.setdefault(category, {"source": "testnet-runtime-receipt", "observed_at": observed_at, "artifact_ref": f"outputs/testnet/{category}.json", "status": "pass", "fact": True})
        evidence[category]["artifact_kind"] = category
    return evidence


def _observation(index: int, *, evidence=None, mutations=None) -> dict:
    start = datetime(2026, 1, 1, 1, tzinfo=UTC) + timedelta(hours=12 * index)
    attestation = current_source_attestation()
    evidence_payload = evidence or _evidence((start + timedelta(hours=12)).isoformat())
    for category, payload in evidence_payload.items():
        path = Path(f"/tmp/testnet-soak-{index}-{category}.json")
        path.write_text(json.dumps({"artifact_kind": category, "category": category, "window": index, "window_index": index, "record_window_id": f"soak-window-{index:02d}", "starts_at": (start).isoformat(), "ends_at": (start + timedelta(hours=12)).isoformat(), "strategy_session_id": "session-continuous", "strategy_revision_id": "revision-dca-1", "plan_digest": "sha256:" + "a" * 64, "environment": "testnet", "release_sha": attestation["source_sha"], "account_fingerprint": "testnet-account-fingerprint"}), encoding="utf-8")
        payload["artifact_ref"] = str(path)
        payload["artifact_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        payload["artifact_kind"] = category
    evidence_payload["release_account_environment_identity"]["release_sha"] = attestation["source_sha"]
    return {
        "window_index": index,
        "record_window_id": f"soak-window-{index:02d}",
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=12)).isoformat(),
        "strategy_session_id": "session-continuous",
        "strategy_revision_id": "revision-dca-1",
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
        "execution_mutations": mutations or [],
    }


def test_seven_day_soak_requires_fourteen_windows_and_keeps_live_disabled(tmp_path: Path) -> None:
    soak = TestnetSoakReadiness(tmp_path / "outputs")
    for index in range(14):
        row = soak.record_window(_observation(index))
        assert row["status"] == "pass"

    receipt = soak.finalize(now="2026-01-08T01:00:00+00:00")

    assert receipt["status"] == "ready"
    assert receipt["window_count"] == 14
    assert receipt["day_count"] == 7
    assert receipt["live_enabled"] is False
    assert receipt["live_writes_enabled"] is False
    assert soak.public_status(now="2026-01-08T01:00:00+00:00")["status"] == "ready"


def test_soak_failure_is_durable_blocker_not_a_pass(tmp_path: Path) -> None:
    soak = TestnetSoakReadiness(tmp_path / "outputs")
    for index in range(14):
        evidence = _evidence()
        if index == 6:
            evidence["protection_coverage"] = {"status": "blocked", "source": "testnet-runtime-receipt", "observed_at": "2026-01-04T01:00:00+00:00", "artifact_ref": "outputs/testnet/protection.json", "reason": "coverage_unknown"}
        soak.record_window(_observation(index, evidence=evidence))

    receipt = soak.finalize()

    assert receipt["status"] == "blocked"
    assert any(item.get("code") == "gate_protection_coverage_not_ready" for item in receipt["blockers"])
    assert receipt["next_action"] == "notify_park_and_wait"
    assert soak.public_status()["status"] == "blocked"


def test_soak_boundaries_reject_mutation_and_replay_is_idempotent(tmp_path: Path) -> None:
    soak = TestnetSoakReadiness(tmp_path / "outputs")
    first = soak.record_window(_observation(0))
    replay = soak.record_window(_observation(0))
    assert replay == first

    blocked = soak.record_window(_observation(1, mutations=[{"action": "flatten"}]))
    assert blocked["status"] == "blocked"
    assert any(item.get("code") == "boundary_execution_mutation" for item in blocked["blockers"])


def test_soak_can_collect_canonical_category_artifacts(tmp_path: Path) -> None:
    soak = TestnetSoakReadiness(tmp_path / "outputs")
    observation = _observation(0)
    artifact_paths = {}
    for category in sorted(set(REQUIRED_CATEGORIES) | {"orders_fills_positions_reconciliation", "protection_coverage", "capability_status", "market_freshness_trust", "runtime_health", "retry_outcomes", "release_account_environment_identity", "recording_package"}):
        path = tmp_path / f"{category}.json"
        payload = {"status": "pass", "source": "runtime-receipt", "observed_at": observation["ends_at"], "artifact_ref": str(path), "artifact_sha256": "pending", "artifact_kind": category, "strategy_session_id": observation["strategy_session_id"], "strategy_revision_id": observation["strategy_revision_id"], "plan_digest": observation["plan_digest"], "environment": "testnet", "release_sha": observation["release_sha"], "account_fingerprint": observation["account_fingerprint"]}
        if category == "release_account_environment_identity":
            payload.update({"release_sha": observation["release_sha"], "account_fingerprint": observation["account_fingerprint"], "environment": "testnet", "broker_id": observation["broker_id"]})
        if category == "market_freshness_trust":
            payload.update({"fresh": True, "trusted": True})
        path.write_text(json.dumps(payload), encoding="utf-8")
        artifact_paths[category] = path
    row = soak.record_window_from_artifacts(observation, artifact_paths=artifact_paths)
    assert row["status"] == "pass"
