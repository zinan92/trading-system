from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.park_paper_preflight import (
    ParkPaperPreflightError,
    build_park_paper_preflight,
    validate_park_paper_preflight,
)


def _config() -> dict:
    return {
        "feature_enabled": True,
        "runtime_mode": "paper_only",
        "control_plane": "telegram",
        "paper_execution": {
            "instrument": {
                "instrument_id": "XAUUSDT.BINANCE",
                "symbol": "XAUUSDT",
                "venue": "BINANCE",
                "provider": "park_paper_config",
            },
            "paper_fee_model": {
                "mode": "paper_contract",
                "maker_fee_rate": "0",
                "taker_fee_rate": "0.000400",
                "funding_rate": "0",
                "source": "park_paper_config",
                "environment": "paper",
                "real_money_eligible": False,
            },
        },
    }


def _attestation() -> dict[str, object]:
    return {
        "source_sha": "a" * 40,
        "source_tree_sha": "b" * 40,
        "tracked_tree_clean": True,
    }


def test_builds_source_bound_credential_free_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "services.park_paper_preflight.current_source_attestation",
        lambda repo_root: _attestation(),
    )
    generated = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
    artifact = build_park_paper_preflight(tmp_path, _config(), now=generated)

    assert artifact["status"] == "ready_for_park_paper"
    assert artifact["paper_only"] is True
    assert artifact["real_money_eligible"] is False
    assert artifact["source_sha"] == "a" * 40
    assert artifact["operations"]["reads_exchange_credentials"] is False
    assert artifact["fee_model"]["mode"] == "paper_contract"
    validate_park_paper_preflight(
        artifact,
        expected_config_digest=str(artifact["config_digest"]),
        now=generated + timedelta(seconds=1),
    )


def test_missing_or_real_money_fee_contract_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "services.park_paper_preflight.current_source_attestation",
        lambda repo_root: _attestation(),
    )
    missing = _config()
    missing["paper_execution"].pop("paper_fee_model")
    with pytest.raises(ParkPaperPreflightError, match="paper_fee_contract_missing"):
        build_park_paper_preflight(tmp_path / "missing", missing)

    real_money = _config()
    real_money["paper_execution"]["paper_fee_model"]["real_money_eligible"] = True
    with pytest.raises(ParkPaperPreflightError, match="paper_fee_real_money_forbidden"):
        build_park_paper_preflight(tmp_path / "real-money", real_money)

    invalid_rate = _config()
    invalid_rate["paper_execution"]["paper_fee_model"]["taker_fee_rate"] = "not-a-rate"
    with pytest.raises(ParkPaperPreflightError, match="paper_fee_taker_fee_rate_invalid"):
        build_park_paper_preflight(tmp_path / "invalid-rate", invalid_rate)


def test_preflight_rejects_source_digest_and_expiry_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.park_paper_preflight.current_source_attestation",
        lambda repo_root: _attestation(),
    )
    generated = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
    artifact = build_park_paper_preflight(tmp_path, _config(), now=generated)
    with pytest.raises(ParkPaperPreflightError, match="config digest mismatch"):
        validate_park_paper_preflight(artifact, expected_config_digest="sha256:wrong", now=generated)
    with pytest.raises(ParkPaperPreflightError, match="expired"):
        validate_park_paper_preflight(
            artifact,
            expected_config_digest=str(artifact["config_digest"]),
            now=generated + timedelta(seconds=901),
        )


def test_direct_park_factory_does_not_consult_legacy_shadow_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.execution_plugin_composition import build_park_direct_paper_adapter

    class FakeAdapter:
        name = "nautilus_paper"

        def __init__(self, output_root, **kwargs):
            self.output_root = output_root
            self.kwargs = kwargs

    monkeypatch.setattr(
        "services.dualtrack_nautilus_execution_adapter.NautilusExecutionAdapter",
        FakeAdapter,
    )
    adapter = build_park_direct_paper_adapter(
        tmp_path / "output",
        config={"execution_engine": {"real_money_eligible": False}},
        nautilus_python=__import__("sys").executable,
        preflight_path=tmp_path / "output" / "park_strategy" / "paper_preflight_current.json",
        environ={"TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED": "1"},
    )

    assert adapter.name == "nautilus_paper"
    assert adapter.kwargs["storage_namespace"] == "nautilus_authoritative"
    assert not (tmp_path / "output" / "dualtrack" / "cutover" / "shadow_gate_current.json").exists()


def test_direct_park_factory_requires_attended_paper_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.execution_plugin_composition import build_park_direct_paper_adapter

    with pytest.raises(RuntimeError, match="attended approval"):
        build_park_direct_paper_adapter(
            tmp_path / "output",
            config={"execution_engine": {"real_money_eligible": False}},
            nautilus_python=__import__("sys").executable,
            preflight_path=tmp_path / "output" / "park_strategy" / "paper_preflight_current.json",
            environ={},
        )
