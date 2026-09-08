from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipelines.testnet_rollout_gate import evaluate_rollout, main


IDENTITY = {
    "trading_system_release_sha": "a" * 40,
    "standard_broker_release_sha": "b" * 40,
    "transport_profile": "hyperliquid-testnet-position-protection",
    "account_fingerprint": "sha256:" + "c" * 64,
    "instrument_id": "BTC-USD-PERP",
    "capability_revision": "hyperliquid-testnet-position-protection-runtime-v1",
    "owner_epoch": 7,
}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _evidence(root: Path, *, family: str = "dca", missing: str | None = None) -> None:
    owner = {"status": "active", "active_owner_id": "local-mac", "epoch": 7}
    _write(root / "testnet_automation/scheduler_ownership/current.json", json.dumps(owner) if False else owner)
    base = {**IDENTITY, "strategy_family": family, "environment": "testnet", "broker_id": "hyperliquid", "status": "pass", "observed_at": "2026-09-08T01:00:00+00:00"}
    if missing != "s2":
        _write(root / f"m5-s2/{family}/attended.json", {**base, "stage": "m5-s2", "execution_mutation": False})
    for index in range(2):
        if missing == f"window-{index}":
            continue
        _write(root / f"m5-s3/{family}/window-{index}.json", {**base, "stage": "m5-s3", "window_index": index})


def test_no_evidence_is_incomplete_and_never_authorizes(tmp_path: Path) -> None:
    result = evaluate_rollout(tmp_path, now="2026-09-08T02:00:00+00:00")
    assert result["status"] == "incomplete"
    assert result["execution_authorized"] is False
    assert result["families"]["dca"]["status"] == "incomplete"


def test_both_families_ready_with_separate_window_counts(tmp_path: Path) -> None:
    _evidence(tmp_path, family="dca")
    _evidence(tmp_path, family="grid")
    result = evaluate_rollout(tmp_path, now="2026-09-08T02:00:00+00:00", expected=IDENTITY)
    assert result["status"] == "ready"
    assert result["families"]["dca"]["m5_s3_window_count"] == 2
    assert result["families"]["grid"]["m5_s3_window_count"] == 2
    assert result["live_enabled"] is False


def test_missing_window_after_collection_is_blocked(tmp_path: Path) -> None:
    _evidence(tmp_path, family="dca", missing="window-1")
    result = evaluate_rollout(tmp_path, now="2026-09-08T02:00:00+00:00", expected=IDENTITY)
    assert result["status"] == "blocked"
    assert any(item["code"] == "m5_s3_windows_incomplete" for item in result["families"]["dca"]["blockers"])


def test_identity_drift_and_stale_evidence_block(tmp_path: Path) -> None:
    _evidence(tmp_path, family="dca")
    path = tmp_path / "m5-s3/dca/window-0.json"
    payload = json.loads(path.read_text())
    payload["account_fingerprint"] = "sha256:" + "d" * 64
    payload["observed_at"] = "2026-09-01T01:00:00+00:00"
    _write(path, payload)
    result = evaluate_rollout(tmp_path, now="2026-09-08T02:00:00+00:00", expected=IDENTITY)
    codes = {item["code"] for item in result["families"]["dca"]["blockers"]}
    assert result["status"] == "blocked"
    assert "account_fingerprint_mismatch" in codes
    assert "evidence_stale" in codes


def test_cli_writes_only_repository_report(tmp_path: Path, monkeypatch, capsys) -> None:
    report_dir = tmp_path / "docs/evidence"
    rc = main(["--output-root", str(tmp_path / "online"), "--date", "2026-09-08", "--write-report", "--evidence-dir", str(report_dir)])
    assert rc == 1
    assert (report_dir / "testnet-rollout-gate-2026-09-08.md").exists()
    assert not (tmp_path / "online" / "docs").exists()
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"
