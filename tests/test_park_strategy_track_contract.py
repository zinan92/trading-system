from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "contracts" / "park-strategy-track-v1.json"


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_contract_is_single_track_telegram_paper_and_default_off() -> None:
    contract = _contract()

    assert contract["contract_id"] == "park-strategy-track-v1"
    assert contract["authority"] == {
        "owner": "Park",
        "execution_track_count": 1,
        "control_plane": "telegram",
        "runtime_mode": "paper_only",
    }
    assert contract["feature_flag"] == {
        "name": "park_strategy_track_v1",
        "enabled_by_default": False,
        "runtime_wiring_present": False,
        "enablement_rule": (
            "a_later_issue_must_supply_runtime_wiring_tests_and_a_separate_release_decision"
        ),
    }


def test_execution_identity_is_independent_from_recording_windows() -> None:
    contract = _contract()

    assert set(contract["identities"]) == {
        "strategy_session_id",
        "strategy_revision_id",
        "record_window_id",
    }
    assert contract["recording_windows"]["authority"] == (
        "record_facts_generate_12h_package_and_review_only"
    )
    assert set(contract["recording_windows"]["forbidden_effects"]) == {
        "strategy_switch",
        "strategy_replan",
        "cancel_orders",
        "flatten_positions",
    }


def test_clean_slate_and_immutable_active_strategy_are_fail_closed() -> None:
    contract = _contract()

    assert set(contract["clean_slate_admission"]["all_required"]) == {
        "reconciliation_healthy",
        "no_open_positions",
        "no_open_or_accepted_orders",
        "no_unresolved_prior_runtime",
        "no_pending_terminal_actions",
    }
    assert contract["clean_slate_admission"]["on_failure"].endswith(
        "without_runtime_order_or_position_mutation"
    )
    active = contract["active_strategy"]
    assert active["authority"] == "immutable_while_active"
    assert {active[key] for key in ("replacement", "reversal", "parameter_edit", "reconfirmation")} == {
        "forbidden"
    }
    assert active["attempted_change_result"].endswith(
        "without_runtime_order_or_position_mutation"
    )


def test_confirmed_boundaries_are_the_only_automatic_terminal_authority() -> None:
    contract = _contract()

    assert contract["primary_state_machine"] == [
        "IDLE_CLEAN",
        "PLAN_CALCULATED",
        "AWAITING_CONFIRMATION",
        "ACTIVE_LOCKED",
        "BOUNDARY_TRIGGERED",
        "CLOSING",
        "RECONCILED",
        "PAUSED",
    ]
    boundary = contract["boundary_invalidation"]
    assert boundary["automatic_authority"] == "preauthorized_by_exact_park_confirmation"
    assert boundary["ordered_actions"] == [
        "freeze_new_entries",
        "cancel_remaining_strategy_entries",
        "close_all_strategy_owned_positions",
        "reconcile",
        "persist_closure",
        "notify_park",
        "enter_paused",
    ]
    assert contract["structural_failure_overlay"]["position_authority"] == (
        "no_automatic_flatten_without_the_confirmed_boundary_trigger"
    )


def test_excluded_capabilities_and_existing_safety_gates_are_complete() -> None:
    contract = _contract()

    assert set(contract["excluded_capabilities"]) == {
        "autonomous_strategy_track",
        "shadow_strategy",
        "feishu_control_plane",
        "multiple_execution_tracks",
        "live_or_real_money_execution",
        "exchange_key_handling",
    }
    assert set(contract["preserved_gates"]) == {
        "trusted_market",
        "tick_freshness",
        "stale_cycle_state",
        "reconciliation",
        "immutable_fill",
        "park_risk_confirmation",
        "paper_only",
        "release_sha_ownership",
        "boot",
        "supervisor_fail_closed",
    }
