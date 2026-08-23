from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipelines import standard_broker_testnet_canary as cli
from services.park_confirmation import ParkConfirmationLedger
from services.standard_broker_testnet_canary import (
    TestnetCanaryOrderRequest,
    TestnetCanaryPlan,
    canary_plan_digest,
    market_fact_digest,
)
from services.standard_broker_testnet_canary_facts import (
    CanaryAccountFact,
    CanaryFactBundle,
    CanaryFeeFact,
    CanaryFillFact,
    CanaryPositionFact,
    CanaryReconciliationFact,
    canary_fact_digest,
)


ACCOUNT_ADDRESS = "0x" + "11" * 20
RELEASE_SHA = "b" * 40
FIXED_TIME = "2026-08-23T01:03:00+00:00"


def _plan_mapping() -> dict[str, object]:
    values: dict[str, object] = {
        "canary_id": "cli-canary-1",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "profile_id": "hyperliquid-testnet-default",
        "account_fingerprint": cli._account_fingerprint(ACCOUNT_ADDRESS),
        "runtime_id": "cli-runtime-1",
        "release_sha": RELEASE_SHA,
        "capability_revision": "hyperliquid-testnet-runtime-v1",
        "instrument_id": "BTC-USD-PERP",
        "direction": "buy",
        "quantity": "0.001",
        "contract_multiplier": "1",
        "quantity_step": "0.001",
        "order_type": "limit",
        "entry_price": "60000",
        "price_tick": "1",
        "time_in_force": "gtc",
        "max_slippage": "25",
        "max_notional": "100",
        "max_leverage": "2",
        "account_equity": "63",
        "max_open_orders": 1,
        "max_open_positions": 1,
        "close_price": "59900",
        "fee_reserve_usd": "1",
        "entry_fee_estimate_usd": "0.25",
        "exit_fee_estimate_usd": "0.25",
        "explicit_loss_buffer_usd": "0.5",
        "max_loss_usd": "50",
        "close_mode": "ordinary_reduce_only_close",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    values["plan_digest"] = canary_plan_digest(values)
    return values


def _write_plan(tmp_path: Path) -> tuple[Path, TestnetCanaryPlan]:
    values = _plan_mapping()
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path, TestnetCanaryPlan.from_mapping(values)


def _confirmation(tmp_path: Path, plan: TestnetCanaryPlan, output_root: Path) -> Path:
    ledger = ParkConfirmationLedger(output_root, park_user_id="park")
    proposal = ledger.create_proposal(
        proposal_id=f"proposal-{plan.canary_id}",
        strategy_session_id=f"session:{plan.canary_id}",
        strategy_revision_id=f"revision:{plan.plan_digest[7:23]}",
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
            "strategy_session_id": proposal["strategy_session_id"],
            "strategy_revision_id": proposal["strategy_revision_id"],
        },
        now=1787446800,
    )
    projection = {
        "event": "confirmed",
        "execution_authorized": True,
        "execution_environment": "testnet",
        "canary_id": plan.canary_id,
        "plan_digest": plan.plan_digest,
        "operator_id": "park",
        "park_user_id": "park",
        "confirmation_id": proposal["proposal_id"],
        "proposal_id": proposal["proposal_id"],
        "receipt_digest": decision["receipt_digest"],
        "expires_at": plan.expires_at,
    }
    path = tmp_path / "confirmation.json"
    path.write_text(json.dumps(projection), encoding="utf-8")
    return path


def test_default_action_is_digest_only_and_does_not_expose_inputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, _plan = _write_plan(tmp_path)
    secret_path = "/private/never-print-this-key"

    assert cli.main(["--plan", str(plan_path), "--secret-file", secret_path]) == 0

    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "DIGEST_ONLY"
    assert result["network_invoked"] is False
    assert result["secret_resolved"] is False
    assert secret_path not in output


def test_digest_mismatch_is_a_nonzero_read_only_blocker(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    values = _plan_mapping()
    values["plan_digest"] = "sha256:" + "c" * 64
    plan_path = tmp_path / "mismatched-plan.json"
    plan_path.write_text(json.dumps(values), encoding="utf-8")

    assert cli.main(["--plan", str(plan_path)]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED"
    assert result["reason_code"] == "plan_digest_mismatch"
    assert result["network_invoked"] is False


def test_preflight_starts_release_bound_runtime_without_secret_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, plan = _write_plan(tmp_path)
    calls: list[str] = []

    class FakeRuntime:
        health = SimpleNamespace(external_network=True, invocation_performed=False)

        def close(self) -> None:
            calls.append("close")

    def fake_build_runtime(selected_plan, _args):
        assert selected_plan.plan_digest == plan.plan_digest
        calls.append("build")
        return FakeRuntime(), object(), object()

    monkeypatch.setattr(cli, "_build_runtime", fake_build_runtime)
    assert cli.main(
        [
            "--action",
            "preflight",
            "--plan",
            str(plan_path),
            "--account-address",
            ACCOUNT_ADDRESS,
            "--approval-id",
            "approval-1",
            "--approved-by",
            "park",
            "--secret-file",
            str(tmp_path / "unread-secret"),
        ]
    ) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "PREFLIGHT_READY"
    assert result["invocation_performed"] is False
    assert result["secret_resolved"] is False
    assert calls == ["build", "close"]


def test_preflight_rejects_missing_runtime_health(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, _plan = _write_plan(tmp_path)
    monkeypatch.setattr(cli, "_build_runtime", lambda _plan, _args: (object(), object(), object()))

    assert cli.main(
        [
            "--action",
            "preflight",
            "--plan",
            str(plan_path),
            "--account-address",
            ACCOUNT_ADDRESS,
            "--approval-id",
            "approval-1",
            "--approved-by",
            "park",
        ]
    ) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["reason_code"] == "runtime_health_missing"


def test_run_requires_durable_confirmation_before_building_external_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, _plan = _write_plan(tmp_path)
    called = False

    def fail_builder(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("external builder must not run")

    monkeypatch.setattr(cli, "_build_external_port", fail_builder)
    result_code = cli.main(
        [
            "--action",
            "run",
            "--plan",
            str(plan_path),
            "--account-address",
            ACCOUNT_ADDRESS,
            "--secret-file",
            str(tmp_path / "secret"),
            "--approval-id",
            "approval-1",
            "--approved-by",
            "park",
            "--execute-testnet",
            "--acknowledge",
            cli.ACKNOWLEDGEMENT,
            "--confirmation",
            str(tmp_path / "missing-confirmation.json"),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert result_code == 2
    assert output["reason_code"] == "input_unavailable"
    assert called is False


def test_run_requires_confirmation_to_exist_in_park_ledger_before_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, plan = _write_plan(tmp_path)
    output_root = tmp_path / "outputs"
    confirmation_path = _confirmation(tmp_path, plan, output_root)
    # The projection is valid, but this simulates a different output root with
    # no durable Park ledger. No network-capable builder may start.
    missing_root = tmp_path / "missing-ledger"
    called = False

    def fail_builder(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("external builder must not run")

    monkeypatch.setattr(cli, "_build_external_port", fail_builder)
    result_code = cli.main(
        [
            "--action",
            "run",
            "--plan",
            str(plan_path),
            "--confirmation",
            str(confirmation_path),
            "--account-address",
            ACCOUNT_ADDRESS,
            "--secret-file",
            str(tmp_path / "secret"),
            "--approval-id",
            "approval-1",
            "--approved-by",
            "park",
            "--output-root",
            str(missing_root),
            "--execute-testnet",
            "--acknowledge",
            cli.ACKNOWLEDGEMENT,
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert result_code == 2
    assert result["reason_code"] == "durable_confirmation_missing"
    assert called is False


@dataclass
class _Receipt:
    order_id: str
    client_order_id: str
    broker_order_id: str
    state: str
    original_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    average_fill_price: Decimal
    instrument_id: str
    side: str
    quantity: Decimal
    order_type: str
    time_in_force: str
    limit_price: Decimal
    account_fingerprint: str
    lifecycle_id: str
    release_sha: str

    broker_id: str = "hyperliquid"
    environment: str = "testnet"

    class _Provenance:
        source = "nautilus-hyperliquid.testnet"
        execution_scope = "hypercore:default"
        transport_state = "external_testnet"
        mapping_revision = "hyperliquid-testnet-runtime-v1"

    provenance = _Provenance()


class _FilledPort:
    def __init__(self, plan: TestnetCanaryPlan) -> None:
        self.plan = plan
        self.requests: dict[str, TestnetCanaryOrderRequest] = {}
        self.fact_reads = 0

    def preflight(self) -> dict[str, object]:
        return {
            "canary_ready": True,
            "host_ready": True,
            "environment": "testnet",
            "transport_profile": "hyperliquid-testnet-default",
            "transport_state": "external_testnet",
            "account_fingerprint": self.plan.account_fingerprint,
            "runtime_id": self.plan.runtime_id,
            "release_sha": self.plan.release_sha,
            "capability_revision": self.plan.capability_revision,
            "network_io": True,
            "real_money_eligible": False,
            "broker_operation_invoked": False,
            "preflight_io_performed": False,
            "protection_ready": False,
            "protection_gap": "external_protection_unavailable",
        }

    def market_fact(self, *, instrument_id: str, now: datetime) -> dict[str, object]:
        value: dict[str, object] = {
            "instrument_id": instrument_id,
            "contract_multiplier": "1",
            "price": "60000",
            "freshness": "fresh",
            "observed_at": now.isoformat(),
            "max_age_seconds": "120",
            "source": "nautilus-hyperliquid.testnet",
            "transport_state": "external_testnet",
            "mapping_revision": self.plan.capability_revision,
        }
        value["fact_digest"] = market_fact_digest(value)
        return value

    def _receipt(self, request: TestnetCanaryOrderRequest, order_id: str) -> _Receipt:
        return _Receipt(
            order_id=order_id,
            client_order_id=request.idempotency_key,
            broker_order_id=f"broker-{order_id}",
            state="filled",
            original_quantity=request.quantity,
            filled_quantity=request.quantity,
            remaining_quantity=Decimal("0"),
            average_fill_price=request.limit_price,
            instrument_id=request.instrument_id,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            limit_price=request.limit_price,
            account_fingerprint=self.plan.account_fingerprint,
            lifecycle_id=self.plan.runtime_id,
            release_sha=self.plan.release_sha,
        )

    def submit(self, request: TestnetCanaryOrderRequest) -> _Receipt:
        self.requests[request.order_id] = request
        return self._receipt(request, request.order_id)

    def query(self, order_id: str) -> _Receipt:
        return self._receipt(self.requests[order_id], order_id)

    def query_by_idempotency_key(self, key: str) -> _Receipt:
        for request in self.requests.values():
            if request.idempotency_key == key:
                return self._receipt(request, request.order_id)
        raise RuntimeError("missing idempotency key")

    def replace(self, order_id: str, request: TestnetCanaryOrderRequest) -> _Receipt:
        self.requests[order_id] = request
        return self._receipt(request, order_id)

    def cancel(self, order_id: str) -> _Receipt:
        return self.query(order_id)

    def read_facts(self, *, order_id: str, instrument_id: str) -> CanaryFactBundle:
        self.fact_reads += 1
        is_entry = self.fact_reads == 1
        cursor = "cursor-entry" if is_entry else "cursor-final"
        occurred_at = "2026-08-23T01:02:00+00:00" if is_entry else "2026-08-23T01:02:30+00:00"
        quantity = self.plan.quantity if is_entry else Decimal("0")
        side = self.plan.direction if is_entry else "sell"
        fill = CanaryFillFact(
            fill_id=f"fill-{cursor}",
            order_id=order_id,
            instrument_id=instrument_id,
            side=side,
            price=self.plan.entry_price if is_entry else self.plan.close_price,
            quantity=self.plan.quantity,
            occurred_at=occurred_at,
            account_fingerprint=self.plan.account_fingerprint,
            runtime_id=self.plan.runtime_id,
            release_sha=self.plan.release_sha,
            capability_revision=self.plan.capability_revision,
            broker_order_id=f"broker-{order_id}",
            client_order_id=self.requests[order_id].idempotency_key,
            cursor=cursor,
        )
        fill = replace(fill, fact_digest=canary_fact_digest(fill))
        fee = CanaryFeeFact(
            fee_id=f"fee-{cursor}",
            fill_id=fill.fill_id,
            amount_usd=Decimal("0.2"),
            currency="USD",
            occurred_at=occurred_at,
            account_fingerprint=self.plan.account_fingerprint,
            runtime_id=self.plan.runtime_id,
            release_sha=self.plan.release_sha,
            capability_revision=self.plan.capability_revision,
            cursor=cursor,
            fee_source="actual_fill",
            fee_state="actual",
        )
        fee = replace(fee, fact_digest=canary_fact_digest(fee))
        account = CanaryAccountFact(
            account_fingerprint=self.plan.account_fingerprint,
            equity_usd=Decimal("62.8"),
            cursor=cursor,
            observed_at=occurred_at,
            runtime_id=self.plan.runtime_id,
            release_sha=self.plan.release_sha,
            capability_revision=self.plan.capability_revision,
        )
        account = replace(account, fact_digest=canary_fact_digest(account))
        positions = (
            CanaryPositionFact(
                instrument_id=instrument_id,
                signed_quantity=quantity,
                cursor=cursor,
                observed_at=occurred_at,
                account_fingerprint=self.plan.account_fingerprint,
                runtime_id=self.plan.runtime_id,
                release_sha=self.plan.release_sha,
                capability_revision=self.plan.capability_revision,
            ),
        ) if is_entry else ()
        positions = tuple(replace(item, fact_digest=canary_fact_digest(item)) for item in positions)
        reconciliation = CanaryReconciliationFact(
            coherent=True,
            freshness="fresh",
            cursor=cursor,
            open_order_ids=(),
            signed_position_quantity=quantity,
            observed_at=occurred_at,
            account_fingerprint=self.plan.account_fingerprint,
            runtime_id=self.plan.runtime_id,
            release_sha=self.plan.release_sha,
            capability_revision=self.plan.capability_revision,
            evidence_digest="sha256:" + "e" * 64,
        )
        reconciliation = replace(reconciliation, fact_digest=canary_fact_digest(reconciliation))
        return CanaryFactBundle(
            fills=(fill,),
            fees=(fee,),
            account=account,
            positions=positions,
            open_orders=(),
            reconciliation=reconciliation,
        )


def test_run_fake_binding_reaches_flat_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, plan = _write_plan(tmp_path)
    output_root = tmp_path / "outputs"
    confirmation_path = _confirmation(tmp_path, plan, output_root)
    port = _FilledPort(plan)
    runtime = SimpleNamespace(
        close=lambda: None,
        health=SimpleNamespace(external_network=True, invocation_performed=False),
    )
    monkeypatch.setattr(cli, "_build_external_port", lambda _plan, _args: (runtime, port))
    monkeypatch.setattr(cli, "_timestamp", lambda: FIXED_TIME)

    result_code = cli.main(
        [
            "--action",
            "run",
            "--plan",
            str(plan_path),
            "--confirmation",
            str(confirmation_path),
            "--account-address",
            ACCOUNT_ADDRESS,
            "--secret-file",
            str(tmp_path / "protected-secret"),
            "--approval-id",
            "approval-1",
            "--approved-by",
            "park",
            "--output-root",
            str(output_root),
            "--execute-testnet",
            "--acknowledge",
            cli.ACKNOWLEDGEMENT,
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert result_code == 0
    assert result["status"] == "FLAT_RECONCILED"
    assert len(port.requests) == 2
    assert json.loads((output_root / "standard_broker_testnet_canary" / "current.json").read_text())[-1]["status"] == "FLAT_RECONCILED"
