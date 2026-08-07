from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.paper_start_facts import (
    PaperStartFactsError,
    PaperStartFactsStore,
    authoritative_account_from_start_facts,
    build_start_facts,
    same_execution_authority,
    validate_start_facts,
)
from services.paper_supervisor_classifier import classify_blocker
from services.paper_supervisor_store import PaperSupervisorStore
from services.strategy_control_plane import (
    StrategyControlMachineError,
    StrategyControlPlane,
)


CYCLE = "2026-08-07_DAY"
NOW = "2026-08-07T01:12:05+00:00"


def _market(price: float = 4246.43) -> dict:
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "binance_usdm_futures",
        "source_mode": "execution_venue",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": price,
        "latest_timestamp": "2026-08-07T01:12:00+00:00",
        "batch_id": "batch-1",
        "bars": [
            {
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
            }
            for _ in range(15)
        ],
        "trust": {"status": "trusted"},
        "strategy_timeframes": {
            "1d": {"latest_close": price, "bars": [[1, price]]},
            "4h": {"latest_close": price, "bars": [[1, price]]},
        },
    }


def _snapshot(equity: float = 10_000.0) -> dict:
    return {
        "account": {
            "equity": equity,
            "ending_cash": equity,
            "starting_cash": 10_000.0,
        },
        "orders": [],
        "positions": [],
        "fills": [],
    }


def _policy() -> dict:
    return {
        "binding_id": "paper-supervisor-grid",
        "binding_version": 2,
        "binding_digest": "a" * 64,
        "policy_id": "park-paper-grid",
        "policy_version": 2,
        "policy_digest": "b" * 64,
        "policy_expires_at": "2026-08-30T01:00:00+00:00",
        "passed": True,
    }


def _source() -> dict:
    return {
        "source_sha": "c" * 40,
        "source_tree_sha": "d" * 40,
        "tracked_tree_clean": True,
    }


def _facts(
    *,
    market: dict | None = None,
    snapshot: dict | None = None,
) -> dict:
    return build_start_facts(
        cycle_id=CYCLE,
        observed_at=NOW,
        market=market or _market(),
        execution_snapshot=snapshot or _snapshot(),
        execution_adapter_name="nautilus_paper",
        execution_contract={
            "schema_version": "dualtrack-execution-contract-v1",
            "execution_instrument_id": "XAUUSDT",
            "price_precision": 2,
        },
        outer_policy_preflight=_policy(),
        source_attestation=_source(),
    )


def test_start_facts_are_content_addressed_append_only_and_idempotent(
    tmp_path: Path,
) -> None:
    store = PaperStartFactsStore(tmp_path)
    first = _facts()

    recorded = store.record(first)
    repeated = store.record(first)

    assert recorded == repeated == validate_start_facts(first)
    assert store.records(CYCLE) == [first]
    assert store.require(CYCLE, first["start_facts_digest"]) == first
    assert authoritative_account_from_start_facts(first) == {
        "equity": 10_000.0,
        "ending_cash": 10_000.0,
        "starting_cash": 10_000.0,
        "execution_account_source": "canonical_paper_start_facts",
        "start_facts_digest": first["start_facts_digest"],
    }


def test_execution_authority_changes_only_for_executable_facts() -> None:
    base = _facts()
    planning_only_market = _market()
    planning_only_market["strategy_timeframes"]["4h"]["bars"] = [
        [2, 4246.43]
    ]
    planning_changed = _facts(market=planning_only_market)
    price_changed = _facts(market=_market(4246.44))
    equity_changed = _facts(snapshot=_snapshot(9999.0))

    assert base["start_facts_digest"] != planning_changed[
        "start_facts_digest"
    ]
    assert same_execution_authority(base, planning_changed) is True
    assert same_execution_authority(base, price_changed) is False
    assert same_execution_authority(base, equity_changed) is False


def test_start_facts_store_rejects_tampering(tmp_path: Path) -> None:
    store = PaperStartFactsStore(tmp_path)
    facts = store.record(_facts())
    path = (
        tmp_path
        / "dualtrack"
        / "supervisor"
        / "start_facts"
        / f"{CYCLE}.json"
    )
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows[0]["execution"]["account"]["equity"] = 1.0
    path.write_text(json.dumps(rows), encoding="utf-8")

    with pytest.raises(
        PaperStartFactsError,
        match="paper_start_facts_invalid",
    ):
        store.require(CYCLE, facts["start_facts_digest"])


def test_start_facts_reject_untrusted_market() -> None:
    market = _market()
    market["fresh"] = False

    with pytest.raises(
        PaperStartFactsError,
        match="trusted_market_provenance_invalid",
    ):
        _facts(market=market)


def test_prepare_start_rejects_changed_authority_before_any_intent_or_order(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(
        tmp_path,
        source_attestation=_source,
    )
    bound = plane.start_facts.record(_facts())
    current = _facts(market=_market(4246.44))
    plane._activate_plan(
        {
            "schema_version": "strategy-plan-v1",
            "strategy_plan_id": "strategy-plan-2026-08-07_DAY-1-test",
            "cycle_id": CYCLE,
            "version": 1,
            "status": "active",
            "strategy_type": "grid",
            "direction": "neutral",
            "style": "steady",
            "locked_at": NOW,
            "cycle_risk_envelope_id": "envelope-test",
            "start_facts_digest": bound["start_facts_digest"],
            "range": {"low": 4200.0, "high": 4300.0},
            "grid": {"count": 10, "mode": "arithmetic"},
        }
    )
    attempt_id = "supervisor-attempt-start-facts-stale"
    store = PaperSupervisorStore(tmp_path)
    with store.try_lease(CYCLE, holder_id="test") as lease:
        assert lease is not None
        lease.record_pre_intent_started(
            attempt_id=attempt_id,
            observed_at=NOW,
            phase_scope="create_or_prepare",
        )
        with pytest.raises(StrategyControlMachineError) as rejected:
            plane.control(
                CYCLE,
                "prepare_start",
                {
                    "strategy_type": "grid",
                    "supervisor_attempt_id": attempt_id,
                    "cycle_risk_envelope_id": "envelope-test",
                    "start_facts_digest": bound[
                        "start_facts_digest"
                    ],
                },
                market=_market(4246.44),
                account={"equity": 1.0},
                current_start_facts=current,
                now=NOW,
            )

    assert rejected.value.code == "start_facts_stale"
    assert rejected.value.evidence["orders_created"] == 0
    assert rejected.value.evidence["start_intent_persisted"] is False
    assert classify_blocker(
        control_code=rejected.value.code
    )["classification"] == "structural"
    assert not plane._prepared_starts_path(CYCLE).exists()


def test_dca_confirmation_is_minted_after_start_facts_identity(
    tmp_path: Path,
) -> None:
    facts = _facts()
    preview = StrategyControlPlane(
        tmp_path,
        source_attestation=_source,
    ).preview(
        CYCLE,
        {
            "strategy_type": "dca",
            "direction": "long",
            "start_facts_digest": facts["start_facts_digest"],
            "dca": {
                "entry_levels": [4240.0, 4230.0],
                "target_price": 4260.0,
                "stop_price": 4200.0,
                "notional_per_addition": 1000.0,
                "max_additions": 2,
                "loop_enabled": False,
            },
        },
        market=_market(),
        account={"equity": 10_000.0},
    )

    assert preview["start_facts_digest"] == facts[
        "start_facts_digest"
    ]
    assert preview["manual_confirmation"]["preview_id"] == preview[
        "preview_id"
    ]
