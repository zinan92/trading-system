from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from pipelines import testnet_automation_proof as cli
from services.park_confirmation import ParkConfirmationLedger
from services.standard_broker_external_execution import (
    STANDARD_BROKER_RELEASE_SHA,
    StandardBrokerExternalExecutionAdapter,
)
from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from tests.test_dca_testnet_lifecycle import _plan as dca_plan
from tests.test_grid_testnet_lifecycle import _plan as grid_plan
from tests.test_standard_broker_external_execution import _Binding


ACCOUNT = "0x" + "11" * 20
RELEASE = "a" * 40


def _args(tmp_path: Path, *, action: str = "preflight") -> list[str]:
    return [
        "--action",
        action,
        "--account-address",
        ACCOUNT,
        "--runtime-id",
        "runtime-proof",
        "--release-sha",
        RELEASE,
        "--approval-id",
        "approval-proof",
        "--approved-by",
        "park",
        "--secret-file",
        str(tmp_path / "never-read-secret"),
        "--output-root",
        str(tmp_path / "outputs"),
    ]


def test_preflight_uses_opt_in_profile_without_execute_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli.StandardBrokerExternalExecutionAdapter,
        "preflight_build_context",
        lambda _context: {
            "status": "PREFLIGHT_READY",
            "transport_profile": cli.PROTECTED_EXTERNAL_PROFILE,
            "capability_revision": cli.PROTECTED_CAPABILITY_REVISION,
            "secret_resolved": False,
        },
    )

    assert cli.main(_args(tmp_path)) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "PREFLIGHT_READY"
    assert result["secret_resolved"] is False
    assert result["transport_profile"] == cli.PROTECTED_EXTERNAL_PROFILE


def test_start_requires_exact_testnet_ack_before_building_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = {
        "schema_version": "strategy-plan-v1",
        "strategy_type": "dca",
        "strategy_plan_id": "proof-plan",
        "strategy_session_id": "proof-session",
        "strategy_revision_id": "proof-revision",
        "plan_digest": "sha256:" + "a" * 64,
        "instrument_id": "BTC-USD-PERP",
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(cli, "_load_json", lambda _path: plan)

    argv = _args(tmp_path, action="start") + [
        "--runtime-id",
        "runtime-1",
        "--strategy-plan",
        str(plan_path),
        "--market",
        str(tmp_path / "market.json"),
        "--confirmation",
        str(tmp_path / "confirmation.json"),
        "--execute-testnet",
    ]

    assert cli.main(argv) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["reason_code"] == "exact_acknowledgement_required"


def test_preflight_rejects_unreviewed_protection_revision_before_building_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli.StandardBrokerExternalExecutionAdapter,
        "preflight_build_context",
        lambda _context: pytest.fail("unreviewed capability must be rejected first"),
    )

    argv = _args(tmp_path) + ["--capability-revision", "unreviewed"]

    assert cli.main(argv) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["reason_code"] == "protected_capability_revision_required"


def test_authoritative_account_snapshot_accepts_typed_stub_broker_facts() -> None:
    binding = _Binding()

    class StubBroker:
        runtime_session = binding.runtime_session
        broker_config = {
            "release_sha": RELEASE,
            "capability_revision": cli.PROTECTED_CAPABILITY_REVISION,
        }

        def read_facts(self, *, instrument_id, now):
            return binding.read_facts(order_id="", instrument_id=instrument_id, now=now)

    account, reconciliation = cli._authoritative_account_snapshot(
        StubBroker(),
        plan={"instrument_id": "BTC-USD-PERP"},
        account_address=ACCOUNT,
    )

    assert account is binding.account
    assert reconciliation is binding.reconciliation


def _authoritative_market_fixture(*, broker_price: str = "60000", broker_observed_at: str | None = None) -> tuple[dict, dict, object]:
    observed_at = datetime.fromisoformat("2026-09-10T01:00:00+00:00")
    market = {
        "bid": "59999", "ask": "60005", "mid": "60000", "observed_at": observed_at.isoformat(),
        "source": "nautilus-hyperliquid.testnet", "mapping_revision": cli.PROTECTED_CAPABILITY_REVISION,
    }
    plan = {"risk_budget": {"max_slippage": "50"}, "price_tick": "1"}
    raw = {
        "instrument_id": "BTC-USD-PERP", "price": broker_price, "freshness": "fresh",
        "observed_at": broker_observed_at or observed_at.isoformat(),
        "source": market["source"], "transport_state": "external_testnet",
        "mapping_revision": market["mapping_revision"],
    }

    class StubBroker:
        def market_fact(self, *, instrument_id: str, now: object) -> dict:
            return {**raw, "instrument_id": instrument_id}

    return market, plan, StubBroker()


@pytest.mark.parametrize(
    ("broker_price", "expected_reason"),
    [("60000", None), ("60003", None), ("60007", "market_price_mismatch"), ("60060", "market_price_mismatch")],
)
def test_authoritative_market_uses_bounded_tolerance_and_bbo(
    broker_price: str, expected_reason: str | None
) -> None:
    market, plan, broker = _authoritative_market_fixture(broker_price=broker_price)
    if expected_reason:
        with pytest.raises(cli.TestnetAutomationProofError, match=expected_reason):
            cli._authoritative_market(broker, market=market, plan=plan, instrument_id="BTC-USD-PERP")
        return

    # The second success case is inside the 10 bps / max-slippage envelope but
    # outside the narrow document midpoint, proving equality is not required.
    result = cli._authoritative_market(broker, market=market, plan=plan, instrument_id="BTC-USD-PERP")
    fact = result["broker_market_fact"]
    assert fact["supplied_mid"] == "60000"
    assert fact["broker_price"] == broker_price
    assert fact["deviation"] == str(abs(Decimal(broker_price) - Decimal("60000")))
    assert fact["tolerance"] == "50"
    assert fact["observed_delta_s"] == "0.0"


def test_authoritative_market_rejects_observation_delta_over_configured_limit() -> None:
    market, plan, broker = _authoritative_market_fixture(
        broker_observed_at="2026-09-10T01:00:11+00:00"
    )
    with pytest.raises(cli.TestnetAutomationProofError, match="market_observation_mismatch"):
        cli._authoritative_market(broker, market=market, plan=plan, instrument_id="BTC-USD-PERP")


@pytest.mark.parametrize(
    ("family", "plan_factory", "expected_submits"),
    [("dca", dca_plan, 1), ("grid", grid_plan, 2)],
)
def test_start_drives_canonical_lifecycle_through_protected_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    family: str,
    plan_factory,
    expected_submits: int,
) -> None:
    plan = plan_factory()
    plan.update(
        {
            "strategy_session_id": f"proof-{family}-session",
            "strategy_revision_id": f"proof-{family}-revision",
            "strategy_plan_id": f"proof-{family}-plan",
            "plan_digest": "sha256:" + "a" * 64,
            "instrument_id": "BTC-USD-PERP",
        }
    )
    if family == "dca":
        plan["dca"] = {
            **plan["dca"],
            "entry_levels": [60000.0, 59000.0],
            "target_price": 61000.0,
            "stop_price": 58000.0,
        }
    observed_at = datetime.now(timezone.utc).replace(microsecond=0)
    market = {
        "execution_ready": True,
        "fresh": True,
        "is_synthetic": False,
        "fallback_policy": "none",
        "observed_at": observed_at.isoformat(),
        "bid": "59999",
        "ask": "60005",
        "mid": "60000",
        "mark": "60000",
        "oracle": "60000",
        "impact": "60000",
        "depth_notional": "100000",
        "max_slippage": "50",
        "max_oracle_deviation_bps": "50",
        "source": "nautilus-hyperliquid.testnet",
        "cursor": "market-cursor-proof",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "instrument_id": "BTC-USD-PERP",
        "asset_index": 0,
        "mapping_revision": cli.PROTECTED_CAPABILITY_REVISION,
        "universe_revision": "hyperliquid-default-perp-v1",
        "connection_epoch": "epoch-proof",
    }
    output_root = tmp_path / "outputs"
    coordinator = TestnetAutomationCoordinator(output_root)
    activation = {
        "strategy_family": family,
        "strategy_session_id": plan["strategy_session_id"],
        "strategy_revision_id": plan["strategy_revision_id"],
        "plan_digest": plan["plan_digest"],
        "account_fingerprint": cli._fingerprint(ACCOUNT),
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "transport_profile": cli.PROTECTED_EXTERNAL_PROFILE,
        "instrument_id": "BTC-USD-PERP",
        "runtime_id": "runtime-1",
        "release_sha": RELEASE,
        "capability_revision": cli.PROTECTED_CAPABILITY_REVISION,
    }
    activation_row = coordinator.activate(activation, command_id=f"preseed-{family}")
    ledger = ParkConfirmationLedger(output_root, park_user_id="park")
    proposal = ledger.create_proposal(
        proposal_id=f"proposal-{family}",
        strategy_session_id=plan["strategy_session_id"],
        strategy_revision_id=plan["strategy_revision_id"],
        plan_digest=plan["plan_digest"],
        risk_digest=plan["plan_digest"],
        expires_at=time.time() + 600,
        execution_environment="testnet",
    )
    decision = ledger.decide(
        proposal_id=proposal["proposal_id"],
        park_user_id="park",
        command_text=f"confirm {plan['plan_digest']}",
        current_binding={
            "strategy_session_id": plan["strategy_session_id"],
            "strategy_revision_id": plan["strategy_revision_id"],
        },
        now=time.time(),
    )
    confirmation = {
        "event": "confirmed",
        "execution_authorized": True,
        "execution_environment": "testnet",
        "plan_digest": plan["plan_digest"],
        "confirmation_id": proposal["proposal_id"],
        "proposal_id": proposal["proposal_id"],
        "receipt_digest": decision["receipt_digest"],
        "confirmed_at": decision["confirmed_at"],
        "operator_id": "park",
        "activation_id": activation_row["activation_id"],
    }
    plan_path = tmp_path / "plan.json"
    market_path = tmp_path / "market.json"
    confirmation_path = tmp_path / "confirmation.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    market_path.write_text(json.dumps(market), encoding="utf-8")
    confirmation_path.write_text(json.dumps(confirmation), encoding="utf-8")

    binding = _Binding()
    original_preflight = binding.preflight
    binding.preflight = lambda: {
        **original_preflight(),
        "account_fingerprint": cli._fingerprint(ACCOUNT),
    }
    binding.market_fact = lambda *, instrument_id, now: {
        "instrument_id": instrument_id,
        "price": "60003",
        "freshness": "fresh",
        "observed_at": now.isoformat(),
        "source": "nautilus-hyperliquid.testnet",
        "transport_state": "external_testnet",
        "mapping_revision": cli.PROTECTED_CAPABILITY_REVISION,
    }
    binding.reconciliation.observed_at = observed_at
    adapter = StandardBrokerExternalExecutionAdapter(
        binding,
        runtime=SimpleNamespace(close=lambda: None),
        broker_config={
            "release_sha": RELEASE,
            "standard_broker_release_sha": STANDARD_BROKER_RELEASE_SHA,
            "runtime_id": "runtime-proof",
            "account_id": ACCOUNT,
            "instrument_binding": {"instrument_id": "BTC-USD-PERP"},
            "ledger_namespace": "ledger-proof",
        },
    )
    monkeypatch.setattr(cli, "build_broker_execution_port", lambda _context: adapter)

    argv = _args(tmp_path, action="start") + [
        "--runtime-id",
        "runtime-1",
        "--strategy-plan",
        str(plan_path),
        "--market",
        str(market_path),
        "--confirmation",
        str(confirmation_path),
        "--output-root",
        str(output_root),
        "--execute-testnet",
        "--acknowledge",
        cli.ACKNOWLEDGEMENT,
    ]
    assert cli.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == f"{family}_running"
    assert result["lifecycle_status"] in {"waiting_entry", "active"}
    broker_market_fact = result["broker_market_fact"]
    assert broker_market_fact["supplied_mid"] == "60000"
    assert broker_market_fact["broker_price"] == "60003"
    assert broker_market_fact["deviation"] == "3"
    assert broker_market_fact["tolerance"] == "50.0"
    assert float(broker_market_fact["observed_delta_s"]) <= 10
    assert len([name for name, _ in binding.calls if name == "submit"]) == expected_submits
