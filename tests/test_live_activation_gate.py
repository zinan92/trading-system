from pathlib import Path

from services.live_activation_gate import LiveActivationGate


def _readiness() -> dict:
    return {
        "status": "ready",
        "environment": "testnet",
        "window_count": 14,
        "required_window_count": 14,
        "receipt_digest": "sha256:" + "a" * 64,
        "live_enabled": False,
        "live_writes_enabled": False,
    }


def _preflight(gate: LiveActivationGate) -> dict:
    return gate.preflight(
        broker_id="hyperliquid",
        account_id="0xmainnet-account",
        environment_fingerprint="hyperliquid:mainnet:acct-a",
        release_sha="b" * 40,
        credential_source="HL_MAINNET_CREDENTIAL",
        instrument_scope="default_perpetuals",
        strategy_scope="dca",
        readiness=_readiness(),
        capabilities={"operations": ["preflight", "submit_order", "cancel_order", "protection_order", "reconciliation"]},
    )


def test_live_preflight_is_read_only_and_binds_exact_identity(tmp_path: Path) -> None:
    gate = LiveActivationGate(tmp_path / "outputs", park_user_id="park")
    result = _preflight(gate)
    assert result["status"] == "ready_for_activation"
    assert result["environment"] == "mainnet"
    assert result["network_io"] is False
    assert result["live_writes_enabled"] is False


def test_live_activation_requires_exact_confirmation_and_is_idempotent(tmp_path: Path) -> None:
    gate = LiveActivationGate(tmp_path / "outputs", park_user_id="park")
    proposal = gate.prepare_activation(_preflight(gate), plan_digest="sha256:" + "c" * 64, expires_at=4102444800)
    rejected = gate.confirm(activation_digest=proposal["activation_digest"], command_text="confirm sha256:" + "d" * 64, park_user_id="park", now=1787350000)
    assert rejected["event"] == "activation_rejected"

    # A fresh gate is used to model a new exact proposal after rejection.
    gate = LiveActivationGate(tmp_path / "fresh", park_user_id="park")
    proposal = gate.prepare_activation(_preflight(gate), plan_digest="sha256:" + "c" * 64, expires_at=4102444800)
    confirmed = gate.confirm(activation_digest=proposal["activation_digest"], command_text=f"confirm {proposal['activation_digest']}", park_user_id="park", now=1787350000)
    replay = gate.confirm(activation_digest=proposal["activation_digest"], command_text=f"confirm {proposal['activation_digest']}", park_user_id="park", now=1787350001)
    assert confirmed == replay
    assert confirmed["live_writes_enabled"] is False
    assert gate.public_status()["status"] == "activated_pending_canary"


def test_live_preflight_blocks_grid_and_missing_soak_or_capabilities(tmp_path: Path) -> None:
    gate = LiveActivationGate(tmp_path / "outputs", park_user_id="park")
    result = gate.preflight(
        broker_id="hyperliquid",
        account_id="",
        environment_fingerprint="paper",
        release_sha="not-a-sha",
        credential_source="raw-secret",
        instrument_scope="hip3",
        strategy_scope="grid",
        readiness={"status": "blocked", "environment": "paper"},
        capabilities={"operations": []},
    )
    assert result["status"] == "blocked"
    assert "live_scope_must_be_dca" in result["blockers"]
    assert result["live_writes_enabled"] is False
