from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipelines import standard_broker_external_dca as cli
from services.park_confirmation import ParkConfirmationLedger
from services.standard_broker_external_dca import ExternalDcaPlan, external_dca_plan_digest


ACCOUNT_ADDRESS = "0x" + "11" * 20
RELEASE_SHA = "b" * 40


def _plan_mapping() -> dict[str, object]:
    values: dict[str, object] = {
        "plan_id": "cli-dca-1",
        "plan_version": 1,
        "cycle_id": "cycle-cli-1",
        "strategy_session_id": "session-cli-1",
        "strategy_revision_id": "revision-cli-1",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "profile_id": "hyperliquid-testnet-position-protection",
        "account_fingerprint": cli._account_fingerprint(ACCOUNT_ADDRESS),
        "runtime_id": "runtime-cli-1",
        "release_sha": RELEASE_SHA,
        "capability_revision": cli.PROTECTION_CAPABILITY_REVISION,
        "instrument_id": "BTC-USD-PERP",
        "direction": "long",
        "entry_levels": ["60000", "59000"],
        "entry_quantities": ["0.001", "0.001"],
        "contract_multiplier": "1",
        "target_price": "61000",
        "stop_price": "58000",
        "close_price": "60500",
        "time_in_force": "gtc",
        "quantity_step": "0.001",
        "price_tick": "1",
        "max_slippage": "10",
        "max_notional": "200",
        "max_leverage": "2",
        "account_equity": "100",
        "max_open_orders": 2,
        "max_open_positions": 1,
        "fee_budget_usd": "1",
        "max_loss_usd": "50",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    values["plan_digest"] = external_dca_plan_digest(values)
    return values


def _write_plan(tmp_path: Path) -> tuple[Path, ExternalDcaPlan]:
    values = _plan_mapping()
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path, ExternalDcaPlan.from_mapping(values)


def _write_confirmation(tmp_path: Path, plan: ExternalDcaPlan) -> tuple[Path, Path]:
    output_root = tmp_path / "outputs"
    ledger = ParkConfirmationLedger(output_root, park_user_id="park")
    proposal = ledger.create_proposal(
        proposal_id=f"proposal-{plan.plan_id}",
        strategy_session_id=plan.strategy_session_id,
        strategy_revision_id=plan.strategy_revision_id,
        plan_digest=plan.plan_digest,
        risk_digest=plan.plan_digest,
        expires_at=4102444800,
        execution_environment="testnet",
    )
    decision = ledger.decide(
        proposal_id=proposal["proposal_id"],
        park_user_id="park",
        command_text=f"confirm {plan.plan_digest}",
        current_binding={
            "strategy_session_id": plan.strategy_session_id,
            "strategy_revision_id": plan.strategy_revision_id,
        },
        now=datetime.now(timezone.utc).timestamp(),
    )
    confirmation = {
        "event": "confirmed",
        "execution_authorized": True,
        "execution_environment": "testnet",
        "canary_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
        "operator_id": "park",
        "park_user_id": "park",
        "proposal_id": proposal["proposal_id"],
        "receipt_digest": decision["receipt_digest"],
        "expires_at": plan.expires_at,
    }
    path = tmp_path / "confirmation.json"
    path.write_text(json.dumps(confirmation), encoding="utf-8")
    return path, output_root


def _args(tmp_path: Path, plan_path: Path, confirmation_path: Path | None = None, *, action: str = "start") -> list[str]:
    values = [
        "--action",
        action,
        "--plan",
        str(plan_path),
        "--account-address",
        ACCOUNT_ADDRESS,
        "--approval-id",
        "approval-cli-1",
        "--approved-by",
        "park",
        "--output-root",
        str(tmp_path / "outputs"),
    ]
    if confirmation_path is not None:
        values.extend(["--confirmation", str(confirmation_path)])
    if action in {"start", "next-entry", "flatten"}:
        values.extend(
            [
                "--secret-file",
                str(tmp_path / "private-do-not-print"),
                "--execute-testnet",
                "--acknowledge",
                cli.ACKNOWLEDGEMENT,
            ]
        )
    return values


def test_default_digest_is_read_only_and_redacts_secret_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, _plan = _write_plan(tmp_path)
    secret_path = "/private/never-print-this-secret"

    assert cli.main(["--plan", str(plan_path), "--secret-file", secret_path]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "DIGEST_ONLY"
    assert result["network_invoked"] is False
    assert result["secret_resolved"] is False
    assert secret_path not in json.dumps(result)


def test_preflight_proves_opt_in_profile_without_network_or_secret_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, plan = _write_plan(tmp_path)
    calls: list[object] = []

    class Matrix:
        profile_id = cli.PROTECTION_MATRIX_ID

        @staticmethod
        def supports(_name: str) -> bool:
            return True

    runtime = SimpleNamespace(
        health=SimpleNamespace(external_network=True, invocation_performed=False),
        preflight=lambda **_kwargs: SimpleNamespace(
            accepted=True,
            external_network=True,
            real_money_eligible=False,
            account_address=ACCOUNT_ADDRESS,
            lifecycle_id=plan.runtime_id,
            release_sha=plan.release_sha,
            capability_revision=plan.capability_revision,
        ),
        close=lambda: calls.append("close"),
    )
    binding = SimpleNamespace(
        runtime_session=SimpleNamespace(
            account=SimpleNamespace(address=ACCOUNT_ADDRESS),
            lifecycle_id=plan.runtime_id,
            capability_revision=plan.capability_revision,
        ),
        protection_capabilities=Matrix(),
    )

    def fake_builder(_plan, _args, *, canary):
        calls.append(canary)
        assert canary is False
        return runtime, binding

    monkeypatch.setattr(cli, "_build_external_protection", fake_builder)
    assert cli.main(_args(tmp_path, plan_path, action="preflight")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "PREFLIGHT_READY"
    assert result["profile_id"] == cli.PROTECTION_PROFILE_ID
    assert result["protection_ready"] is True
    assert result["invocation_performed"] is False
    assert calls == [False, "close"]


def test_start_requires_durable_confirmation_before_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, _plan = _write_plan(tmp_path)
    called = False

    def fail_builder(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("protected builder must not run")

    monkeypatch.setattr(cli, "_build_external_protection", fail_builder)
    result_code = cli.main(
        _args(tmp_path, plan_path, tmp_path / "missing-confirmation.json")
    )
    result = json.loads(capsys.readouterr().out)
    assert result_code == 2
    assert result["reason_code"] == "input_unavailable"
    assert called is False


def test_lifecycle_builder_rejects_unprotected_binding() -> None:
    with pytest.raises(cli.ExternalDcaCliError, match="protected_binding_missing_protection"):
        cli._build_lifecycle(Path("/tmp/external-dca-test"), SimpleNamespace(protection=None))


def test_start_stops_after_one_attended_entry_and_protection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, plan = _write_plan(tmp_path)
    confirmation_path, _output_root = _write_confirmation(tmp_path, plan)
    calls: list[object] = []

    class FakeLifecycle:
        current_path = tmp_path / "outputs" / "standard_broker_external_dca" / "current.json"

        def __init__(self) -> None:
            self.state = {
                "status": "ENTRY_FILLED_PENDING_FACTS",
                "entry_order_id": f"{plan.plan_id}:entry:0",
                "next_action": "read_entry_facts",
            }

        def prepare(self, *_args, **_kwargs):
            calls.append("prepare")
            return self.state

        def on_entry_facts(self, *_args, **kwargs):
            calls.append(("facts", kwargs["bundle"]))
            self.state = {**self.state, "status": "PROTECTION_ACTIVE", "next_action": "attended_next_entry"}
            return self.state

        def snapshot(self):
            return self.state

    adapter = SimpleNamespace(read_facts=lambda **_kwargs: calls.append("read_facts") or object())
    lifecycle = FakeLifecycle()
    runtime = SimpleNamespace(close=lambda: calls.append("close"))
    binding = SimpleNamespace(protection=object())

    monkeypatch.setattr(cli, "_build_external_protection", lambda _plan, _args, *, canary: (runtime, binding))
    monkeypatch.setattr(cli, "_build_lifecycle", lambda _root, _binding: (lifecycle, adapter))
    assert cli.main(_args(tmp_path, plan_path, confirmation_path)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "PROTECTION_ACTIVE"
    assert "submit_next_entry" not in calls
    assert calls[0:2] == ["prepare", "read_facts"]
    assert calls[2][0] == "facts"
    assert calls[-1] == "close"


def test_flatten_reuses_durable_state_and_requires_flat_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, plan = _write_plan(tmp_path)
    confirmation_path, _output_root = _write_confirmation(tmp_path, plan)
    calls: list[object] = []

    class FakeLifecycle:
        current_path = tmp_path / "outputs" / "standard_broker_external_dca" / "current.json"

        def flatten(self, *_args, **kwargs):
            calls.append(kwargs)
            return {"status": "FLAT_RECONCILED", "next_action": "record_dca_result"}

        def snapshot(self):
            return {"status": "FLAT_RECONCILED", "next_action": "record_dca_result"}

    lifecycle = FakeLifecycle()
    runtime = SimpleNamespace(close=lambda: calls.append("close"))
    binding = SimpleNamespace(protection=object())
    monkeypatch.setattr(cli, "_build_external_protection", lambda _plan, _args, *, canary: (runtime, binding))
    monkeypatch.setattr(cli, "_build_lifecycle", lambda _root, _binding: (lifecycle, object()))

    assert cli.main(_args(tmp_path, plan_path, confirmation_path, action="flatten")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "FLAT_RECONCILED"
    assert calls[0]["confirmation"]["plan_digest"] == plan.plan_digest
    assert "bundle" not in calls[0]
    assert calls[-1] == "close"


def test_next_entry_submits_one_attended_level_and_reprotects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, plan = _write_plan(tmp_path)
    confirmation_path, _output_root = _write_confirmation(tmp_path, plan)
    calls: list[object] = []

    class FakeLifecycle:
        current_path = tmp_path / "outputs" / "standard_broker_external_dca" / "current.json"

        def __init__(self) -> None:
            self.state = {
                "status": "PROTECTION_ACTIVE",
                "entry_order_id": f"{plan.plan_id}:entry:0",
                "next_action": "submit_next_entry_only_after_attended_price_gate",
            }

        def submit_next_entry(self, *_args, **_kwargs):
            calls.append("submit_next_entry")
            self.state = {
                **self.state,
                "status": "ENTRY_FILLED_PENDING_FACTS",
                "entry_order_id": f"{plan.plan_id}:entry:1",
                "next_action": "read_entry_facts",
            }
            return self.state

        def on_entry_facts(self, *_args, **kwargs):
            calls.append(("facts", kwargs["bundle"]))
            self.state = {**self.state, "status": "PROTECTION_ACTIVE", "next_action": "attended_next_entry"}
            return self.state

        def snapshot(self):
            return self.state

    adapter = SimpleNamespace(read_facts=lambda **_kwargs: calls.append("read_facts") or object())
    lifecycle = FakeLifecycle()
    runtime = SimpleNamespace(close=lambda: calls.append("close"))
    binding = SimpleNamespace(protection=object())
    monkeypatch.setattr(cli, "_build_external_protection", lambda _plan, _args, *, canary: (runtime, binding))
    monkeypatch.setattr(cli, "_build_lifecycle", lambda _root, _binding: (lifecycle, adapter))

    assert cli.main(_args(tmp_path, plan_path, confirmation_path, action="next-entry")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "PROTECTION_ACTIVE"
    assert result["action"] == "next-entry"
    assert calls[0:2] == ["submit_next_entry", "read_facts"]
    assert calls[2][0] == "facts"
    assert calls[-1] == "close"
