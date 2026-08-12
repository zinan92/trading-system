from __future__ import annotations

import json
from pathlib import Path

from services.cloud_service_boot import CloudPaperServiceBootGate
from services.journal_store import write_json


SHA = "a" * 40
TREE = "b" * 40


def write_preflight(output_root: Path, *, source_sha: str = SHA) -> None:
    write_json(
        output_root / "cloud" / "preflight" / "current.json",
        [
            {
                "schema_version": "cloud-paper-preflight-v1",
                "status": "pass",
                "paper_only": True,
                "control_actions_executed": 0,
                "checks": [
                    {
                        "id": "source_attestation",
                        "status": "pass",
                        "source_sha": source_sha,
                        "source_tree_sha": TREE,
                    }
                ],
            }
        ],
    )


def attestation() -> dict:
    return {
        "source_sha": SHA,
        "source_tree_sha": TREE,
        "tracked_tree_clean": True,
    }


def test_cloud_service_boot_accepts_matching_preflight(tmp_path: Path):
    write_preflight(tmp_path)

    result = CloudPaperServiceBootGate(
        tmp_path,
        source_attestation=attestation,
    ).verify("dashboard")

    assert result["ok"] is True
    assert result["status"] == "pass"
    receipt = json.loads(
        (tmp_path / "cloud" / "service_boot" / "dashboard_current.json").read_text()
    )[-1]
    assert receipt["source_sha"] == SHA


def test_cloud_service_boot_blocks_mismatched_source(tmp_path: Path):
    write_preflight(tmp_path, source_sha="c" * 40)

    result = CloudPaperServiceBootGate(
        tmp_path,
        source_attestation=attestation,
    ).verify("dualtrack-live-tick")

    assert result["ok"] is False
    assert result["blocker"] == "cloud_paper_source_sha_mismatch"


def test_cloud_service_boot_keeps_live_tick_independent_of_provider_readiness(
    tmp_path: Path,
):
    write_preflight(tmp_path)

    result = CloudPaperServiceBootGate(
        tmp_path,
        source_attestation=attestation,
    ).verify("dualtrack-live-tick")

    assert result["ok"] is True
    assert result["status"] == "pass"


def test_cloud_service_boot_blocks_non_allowlisted_service(tmp_path: Path):
    write_preflight(tmp_path)

    result = CloudPaperServiceBootGate(
        tmp_path,
        source_attestation=attestation,
    ).verify("live-broker")

    assert result["ok"] is False
    assert result["blocker"] == "cloud_paper_service_not_allowlisted"


def test_daily_and_deadman_boot_require_the_read_only_timer_contract(tmp_path: Path):
    write_preflight(tmp_path)

    result = CloudPaperServiceBootGate(
        tmp_path,
        source_attestation=attestation,
        timer_contract=lambda: {"status": "blocked"},
    ).verify("deadman-ping")

    assert result["ok"] is False
    assert result["blocker"] == "cloud_paper_timer_contract_not_passing"


def test_provider_readiness_boot_requires_its_canonical_timer(tmp_path: Path):
    write_preflight(tmp_path)

    result = CloudPaperServiceBootGate(
        tmp_path,
        source_attestation=attestation,
        timer_contract=lambda: {
            "status": "blocked",
            "service_status": {"ai-provider-readiness": "pass"},
            "checked_at": "2026-08-06T00:00:00+00:00",
            "provider_readiness": {},
        },
    ).verify("ai-provider-readiness")

    assert result["ok"] is True
    assert result["timer_contract"]["service_status"] == "pass"


def test_provider_timer_failure_cannot_silence_deadman_boot(tmp_path: Path):
    write_preflight(tmp_path)

    result = CloudPaperServiceBootGate(
        tmp_path,
        source_attestation=attestation,
        timer_contract=lambda: {
            "status": "blocked",
            "service_status": {
                "deadman-ping": "pass",
                "ai-provider-readiness": "blocked",
            },
            "provider_readiness": {
                "current": {
                    "ok": False,
                    "failure_code": "strategy_recommendation_provider_timeout",
                }
            },
        },
    ).verify("deadman-ping")

    assert result["ok"] is True
    assert result["timer_contract"]["service_status"] == "pass"


def test_deadman_failure_cannot_silence_watchdog_boot(tmp_path: Path):
    write_preflight(tmp_path)

    result = CloudPaperServiceBootGate(
        tmp_path,
        source_attestation=attestation,
        timer_contract=lambda: {
            "status": "blocked",
            "service_status": {
                "deadman-ping": "blocked",
                "deadman-watchdog": "pass",
            },
        },
    ).verify("deadman-watchdog")

    assert result["ok"] is True
    assert result["timer_contract"]["service_status"] == "pass"
