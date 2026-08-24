from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipelines import standard_broker_external_dca as cli
from services.park_confirmation import ParkConfirmationLedger
from services.dca_plan import build_dca_preview, build_dca_strategy_plan
from services.dualtrack_config import DEFAULT_DUALTRACK_CONFIG
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


def _args(
    tmp_path: Path,
    plan_path: Path | None,
    confirmation_path: Path | None = None,
    *,
    action: str = "start",
    strategy_plan_path: Path | None = None,
    binding_path: Path | None = None,
) -> list[str]:
    values = [
        "--action",
        action,
        "--account-address",
        ACCOUNT_ADDRESS,
        "--approval-id",
        "approval-cli-1",
        "--approved-by",
        "park",
        "--output-root",
        str(tmp_path / "outputs"),
    ]
    if strategy_plan_path is not None:
        values.extend(["--strategy-plan", str(strategy_plan_path)])
        if binding_path is not None:
            values.extend(["--binding", str(binding_path)])
    else:
        values.extend(["--plan", str(plan_path)])
    if confirmation_path is not None:
        values.extend(["--confirmation", str(confirmation_path)])
    if action in {"start", "reconcile-entry", "next-entry", "flatten"}:
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


def _write_canonical_start_inputs(
    tmp_path: Path,
) -> tuple[Path, Path, ExternalDcaPlan, Path]:
    strategy_path = tmp_path / "strategy-plan.json"
    binding_path = tmp_path / "binding.json"
    strategy_path.write_text(json.dumps(_canonical_strategy_plan()), encoding="utf-8")
    binding = _projection_binding()
    binding["account_fingerprint"] = cli._account_fingerprint(ACCOUNT_ADDRESS)
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    projection = cli.project_canonical_dca_plan(
        json.loads(strategy_path.read_text(encoding="utf-8")),
        binding=binding,
    )
    plan = ExternalDcaPlan.from_mapping(projection.plan)
    confirmation_path, _output_root = _write_confirmation(tmp_path, plan)
    return strategy_path, binding_path, plan, confirmation_path


def _canonical_strategy_plan() -> dict[str, object]:
    config = deepcopy(DEFAULT_DUALTRACK_CONFIG)
    config["execution_contract"] = {
        **config["execution_contract"],
        "price_increment": "0.01",
        "quantity_increment": "0.001",
    }
    market = {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "hyperliquid.external_testnet",
        "symbol": "PAXG-USD-PERP",
        "timeframe": "1m",
        "latest_close": 4010.0,
        "latest_timestamp": "2026-07-22T00:19:00+00:00",
        "bars": [
            {
                "timestamp": f"2026-07-22T00:{index:02d}:00+00:00",
                "open": 4010.0,
                "high": 4011.0,
                "low": 4009.0,
                "close": 4010.0,
            }
            for index in range(20)
        ],
    }
    preview = build_dca_preview(
        "2026-07-22_NIGHT",
        {
            "direction": "long",
            "dca": {
                "entry_levels": [4004.0, 3996.0, 3988.0],
                "target_price": 4050.0,
                "stop_price": 3970.0,
                "notional_per_addition": 500.0,
                "max_additions": 3,
                "loop_enabled": False,
            },
            "risk_budget": {"leverage": 10},
        },
        market=market,
        account={"equity": 10_000},
        config=config,
    )
    return build_dca_strategy_plan(
        preview,
        strategy_plan_id="strategy-plan-cli-parity",
        version=1,
        locked_at="2026-07-22T16:00:00+00:00",
    )


def _projection_binding() -> dict[str, object]:
    return {
        "plan_id": "hl-cli-projection-1",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "profile_id": "hyperliquid-testnet-position-protection",
        "account_fingerprint": "sha256:" + "a" * 64,
        "runtime_id": "hl-runtime-cli-1",
        "release_sha": "b" * 40,
        "capability_revision": cli.PROTECTION_CAPABILITY_REVISION,
        "instrument_id": "PAXG-USD-PERP",
        "contract_multiplier": "1",
        "quantity_step": "0.001",
        "price_tick": "0.001",
        "max_slippage": "5",
        "max_notional": "1600",
        "max_leverage": "10",
        "account_equity": "10000",
        "max_open_orders": 3,
        "max_open_positions": 1,
        "fee_budget_usd": "1",
        "max_loss_usd": "50",
        "time_in_force": "gtc",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "close_price": "4050",
        "market_source": {
            "source_id": "hyperliquid.external_testnet",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "instrument_id": "PAXG-USD-PERP",
            "execution_venue": True,
        },
    }


def test_project_action_derives_external_plan_from_canonical_strategy_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    strategy_path = tmp_path / "strategy-plan.json"
    binding_path = tmp_path / "binding.json"
    strategy_path.write_text(json.dumps(_canonical_strategy_plan()), encoding="utf-8")
    binding_path.write_text(json.dumps(_projection_binding()), encoding="utf-8")

    result_code = cli.main([
        "--action",
        "project",
        "--plan",
        str(strategy_path),
        "--binding",
        str(binding_path),
    ])

    result = json.loads(capsys.readouterr().out)
    assert result_code == 0
    assert result["status"] == "PROJECTED"
    assert result["network_invoked"] is False
    assert result["secret_resolved"] is False
    assert result["source_strategy_plan_id"] == "strategy-plan-cli-parity"
    assert result["projected_plan"]["instrument_id"] == "PAXG-USD-PERP"
    assert result["canonical_semantics"]["loop_enabled"] is False


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
    strategy_path, binding_path, _plan, _confirmation_path = _write_canonical_start_inputs(tmp_path)
    called = False

    def fail_builder(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("protected builder must not run")

    monkeypatch.setattr(cli, "_build_external_protection", fail_builder)
    result_code = cli.main(
        _args(
            tmp_path,
            None,
            tmp_path / "missing-confirmation.json",
            strategy_plan_path=strategy_path,
            binding_path=binding_path,
        )
    )
    result = json.loads(capsys.readouterr().out)
    assert result_code == 2
    assert result["reason_code"] == "input_unavailable"
    assert called is False


def test_start_rejects_hand_authored_external_plan_without_canonical_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, _plan = _write_plan(tmp_path)
    called = False

    def fail_builder(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("protected builder must not run")

    monkeypatch.setattr(cli, "_build_external_protection", fail_builder)
    assert cli.main(_args(tmp_path, plan_path)) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["reason_code"] == "canonical_strategy_plan_required"
    assert called is False


def test_lifecycle_builder_rejects_unprotected_binding() -> None:
    with pytest.raises(cli.ExternalDcaCliError, match="protected_binding_missing_protection"):
        cli._build_lifecycle(Path("/tmp/external-dca-test"), SimpleNamespace(protection=None))


def test_start_stops_after_one_attended_entry_and_protection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    strategy_path, binding_path, plan, confirmation_path = _write_canonical_start_inputs(tmp_path)
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
    assert cli.main(
        _args(
            tmp_path,
            None,
            confirmation_path,
            strategy_plan_path=strategy_path,
            binding_path=binding_path,
        )
    ) == 0
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


@pytest.mark.parametrize(
    ("flag", "value", "reason"),
    [
        ("--acknowledge", "WRONG_NEXT_ENTRY_ACK", "exact_acknowledgement_required"),
        ("--execute-testnet", None, "explicit_execute_flag_required"),
    ],
)
def test_next_entry_has_its_own_exposure_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str | None,
    reason: str,
) -> None:
    plan_path, plan = _write_plan(tmp_path)
    confirmation_path, _output_root = _write_confirmation(tmp_path, plan)
    called = False

    def fail_builder(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("next-entry builder must not run before its exact gate")

    monkeypatch.setattr(cli, "_build_external_protection", fail_builder)
    argv = _args(tmp_path, plan_path, confirmation_path, action="next-entry")
    if flag == "--acknowledge":
        index = argv.index(flag)
        argv[index + 1] = value or ""
    else:
        index = argv.index(flag)
        del argv[index]
    assert cli.main(argv) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["reason_code"] == reason
    assert called is False


def test_next_entry_no_remaining_level_does_not_claim_signer_resolution_or_read_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, plan = _write_plan(tmp_path)
    confirmation_path, _output_root = _write_confirmation(tmp_path, plan)
    calls: list[str] = []

    class FakeLifecycle:
        current_path = tmp_path / "outputs" / "standard_broker_external_dca" / "current.json"

        def __init__(self) -> None:
            self.state = {
                "status": "PROTECTION_ACTIVE",
                "entry_order_id": f"{plan.plan_id}:entry:1",
                "next_action": "await_terminal_target_or_stop",
            }

        def snapshot(self):
            return self.state

        def submit_next_entry(self, *_args, **_kwargs):
            calls.append("submit_next_entry")
            return self.state

    lifecycle = FakeLifecycle()
    adapter = SimpleNamespace(read_facts=lambda **_kwargs: calls.append("read_facts"))
    runtime = SimpleNamespace(close=lambda: calls.append("close"))
    binding = SimpleNamespace(protection=object())
    monkeypatch.setattr(cli, "_build_external_protection", lambda *_args, **_kwargs: (runtime, binding))
    monkeypatch.setattr(cli, "_build_lifecycle", lambda *_args, **_kwargs: (lifecycle, adapter))

    secret_path = str(tmp_path / "never-print-next-entry-secret")
    argv = _args(tmp_path, plan_path, confirmation_path, action="next-entry")
    argv[argv.index("--secret-file") + 1] = secret_path
    assert cli.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    output = json.dumps(result)
    assert result["status"] == "PROTECTION_ACTIVE"
    assert result["secret_resolved"] is False
    assert result["network_invoked"] is True
    assert "read_facts" not in calls
    assert secret_path not in output
    assert calls == ["submit_next_entry", "close"]


def test_next_entry_protection_gate_returns_redacted_blocker_without_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, plan = _write_plan(tmp_path)
    confirmation_path, _output_root = _write_confirmation(tmp_path, plan)
    calls: list[str] = []

    class FakeLifecycle:
        current_path = tmp_path / "outputs" / "standard_broker_external_dca" / "current.json"

        def __init__(self) -> None:
            self.state = {
                "status": "BLOCKED",
                "blocker": "new_entry_requires_active_protection",
                "next_action": "notify_park_and_wait",
            }

        def snapshot(self):
            return self.state

        def submit_next_entry(self, *_args, **_kwargs):
            calls.append("submit_next_entry")
            return self.state

    lifecycle = FakeLifecycle()
    adapter = SimpleNamespace(read_facts=lambda **_kwargs: calls.append("read_facts"))
    runtime = SimpleNamespace(close=lambda: calls.append("close"))
    binding = SimpleNamespace(protection=object())
    monkeypatch.setattr(cli, "_build_external_protection", lambda *_args, **_kwargs: (runtime, binding))
    monkeypatch.setattr(cli, "_build_lifecycle", lambda *_args, **_kwargs: (lifecycle, adapter))

    assert cli.main(_args(tmp_path, plan_path, confirmation_path, action="next-entry")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "new_entry_requires_active_protection"
    assert result["secret_resolved"] is False
    assert "read_facts" not in calls


def test_reconcile_entry_queries_once_without_submitting_another_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, plan = _write_plan(tmp_path)
    confirmation_path, _output_root = _write_confirmation(tmp_path, plan)
    calls: list[str] = []

    class FakeLifecycle:
        current_path = tmp_path / "outputs" / "standard_broker_external_dca" / "current.json"

        def reconcile_entry(self, *_args, **_kwargs):
            calls.append("reconcile_entry")
            return {
                "status": "WAITING_ENTRY",
                "next_action": "attended_reconcile_entry_or_cancel_entry",
            }

        def snapshot(self):
            return {
                "status": "WAITING_ENTRY",
                "next_action": "attended_reconcile_entry_or_cancel_entry",
            }

    runtime = SimpleNamespace(close=lambda: calls.append("close"))
    binding = SimpleNamespace(protection=object())
    monkeypatch.setattr(cli, "_build_external_protection", lambda *_args, **_kwargs: (runtime, binding))
    monkeypatch.setattr(cli, "_build_lifecycle", lambda *_args, **_kwargs: (FakeLifecycle(), object()))

    assert cli.main(_args(tmp_path, plan_path, confirmation_path, action="reconcile-entry")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "WAITING_ENTRY"
    assert result["action"] == "reconcile-entry"
    assert calls == ["reconcile_entry", "close"]
