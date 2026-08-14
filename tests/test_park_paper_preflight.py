from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.journal_store import write_json
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
                "schema_version": "instrument-definition-v1",
                "instrument_id": "XAUUSDT.BINANCE",
                "symbol": "XAUUSDT",
                "venue": "BINANCE",
                "asset_class": "commodity",
                "market_type": "usd_m_futures",
                "contract_type": "TRADIFI_PERPETUAL",
                "status": "TRADING",
                "base_currency": "XAU",
                "quote_currency": "USDT",
                "settlement_currency": "USDT",
                "margin_currency": "USDT",
                "is_inverse": False,
                "price_precision": 2,
                "price_increment": "0.01",
                "min_price": "0.01",
                "max_price": "200000",
                "size_precision": 3,
                "size_increment": "0.001",
                "min_quantity": "0.001",
                "max_quantity": "10000",
                "min_notional": "5",
                "margin_init_rate": "0.0500",
                "margin_maint_rate": "0.0250",
                "contract_multiplier": "1",
                "provider": "binance_usdm_futures",
                "source_mode": "binance_usdm_futures",
                "served_from": "upstream",
                "execution_venue": True,
                "is_synthetic": False,
                "upstream_server_time": 1783667819466,
                "derived_fields": {
                    "contract_multiplier": "usd_m_notional_equals_price_times_quantity",
                },
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
    monkeypatch.setattr(
        "services.execution_plugin_composition.validate_park_paper_preflight",
        lambda *_args, **_kwargs: None,
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


def test_preflight_artifact_digest_binds_instrument_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.park_paper_preflight.current_source_attestation",
        lambda repo_root: _attestation(),
    )
    generated = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
    artifact = build_park_paper_preflight(tmp_path, _config(), now=generated)
    tampered = dict(artifact)
    tampered["instrument"] = {**artifact["instrument"], "price_precision": 8}
    with pytest.raises(ParkPaperPreflightError, match="artifact payload digest mismatch"):
        validate_park_paper_preflight(
            tampered,
            expected_config_digest=str(artifact["config_digest"]),
            now=generated + timedelta(seconds=1),
        )


def test_direct_factory_rejects_missing_preflight(tmp_path: Path) -> None:
    from services.execution_plugin_composition import build_park_direct_paper_adapter

    with pytest.raises(RuntimeError, match="preflight"):
        build_park_direct_paper_adapter(
            tmp_path / "output",
            config={"execution_engine": {"real_money_eligible": False}},
            nautilus_python=__import__("sys").executable,
            preflight_path=tmp_path / "missing.json",
            environ={"TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED": "1"},
        )


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


def test_nautilus_replay_scrubs_host_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.dualtrack_nautilus_execution_adapter import NautilusExecutionAdapter

    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["env"] = dict(kwargs["env"])
        write_json(Path(args[0][-1]), [{"schema_version": "dualtrack-execution-v1"}])
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setenv("BINANCE_API_KEY", "must-not-cross-boundary")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN", "must-not-cross-boundary")
    monkeypatch.setattr(
        "services.dualtrack_nautilus_execution_adapter.subprocess.run",
        fake_run,
    )
    adapter = object.__new__(NautilusExecutionAdapter)
    adapter.nautilus_python = Path(sys.executable)
    result = adapter._subprocess_replay(
        tmp_path / "preflight.json",
        tmp_path / "input.json",
        tmp_path / "output.json",
    )

    assert result["schema_version"] == "dualtrack-execution-v1"
    environment = captured["env"]
    assert "BINANCE_API_KEY" not in environment
    assert "TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN" not in environment
    assert environment["PYTHONPATH"].startswith(str(Path(__file__).resolve().parents[1]))


def test_park_mutation_gate_is_locked_until_runtime_admission() -> None:
    from services.park_paper_mutation_gate import ParkPaperMutationGate

    gate = ParkPaperMutationGate()
    with pytest.raises(RuntimeError, match="requires runtime admission"):
        gate.require()
    gate.grant(
        {
            "issuer": "ParkPaperRuntime.run_once",
            "strategy_session_id": "session-1",
            "strategy_revision_id": "revision-1",
            "plan_digest": "sha256:" + "a" * 64,
            "park_confirmation_digest": "sha256:" + "a" * 64,
            "cutover_status": "pass",
        }
    )
    gate.require()
    gate.revoke()
    with pytest.raises(RuntimeError, match="requires runtime admission"):
        gate.require()
