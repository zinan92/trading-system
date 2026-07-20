from copy import deepcopy

import pytest

from schemas.risk import (
    build_risk_decision,
    build_risk_request,
    validate_risk_decision,
    validate_risk_request,
)


def request(**overrides):
    values = {
        "checked_at": "2026-07-18T01:00:00+00:00",
        "scope": "paper_grid",
        "action_class": "increase_exposure",
        "candidate": {"strategy_plan_id": "plan-1", "commands": [{"side": "buy", "price": 100.0}]},
        "account": {"snapshot_id": "accounting-1", "equity": 10_000.0},
        "market": {"provider": "binance_usdm", "price": 100.0, "fresh": True},
        "execution": {"state_id": "execution-1", "positions": []},
        "policy": {"policy_id": "grid-risk-v1", "max_leverage": 10.0},
        "evaluator": {"name": "paper-grid-risk", "version": "1"},
    }
    values.update(overrides)
    return build_risk_request(**values)


def blocker(code: str = "plan_loss_budget_exceeded") -> dict:
    return {
        "code": code,
        "source": "paper_grid.max_plan_loss",
        "message": "candidate loss exceeds budget",
        "evidence": {"candidate": 1200.0, "limit": 1000.0},
    }


def test_risk_request_is_immutable_content_addressed_and_component_bound() -> None:
    candidate = {"strategy_plan_id": "plan-1", "commands": [{"side": "buy", "price": 100.0}]}
    built = request(candidate=candidate)
    candidate["commands"][0]["price"] = 999.0

    payload = built.to_dict()
    assert payload["candidate"]["commands"][0]["price"] == 100.0
    assert set(payload["bindings"]) == {
        "candidate_sha256",
        "account_sha256",
        "market_sha256",
        "execution_sha256",
        "policy_sha256",
        "evaluator_sha256",
    }
    assert validate_risk_request(payload).request_id == built.request_id
    changed = request(candidate={**payload["candidate"], "strategy_plan_id": "plan-2"})
    assert changed.request_id != built.request_id


def test_risk_contract_rejects_unknown_action_naive_time_nan_and_non_json() -> None:
    with pytest.raises(ValueError, match="unsupported risk action"):
        request(action_class="buy_more")
    with pytest.raises(ValueError, match="timezone"):
        request(checked_at="2026-07-18T01:00:00")
    with pytest.raises(ValueError):
        request(account={"equity": float("nan")})
    with pytest.raises(TypeError, match="non-JSON"):
        request(candidate={"commands": {"not", "json"}})


def test_decision_derives_flags_and_detects_every_tampered_surface() -> None:
    built_request = request()
    allowed = build_risk_decision(built_request, metrics={"projected_leverage": 2.0})
    blocked = build_risk_decision(built_request, blockers=[blocker()])

    assert allowed.outcome == "allow"
    assert allowed.allow_exposure_increase is True
    assert blocked.outcome == "block"
    assert blocked.allow_exposure_increase is False
    assert blocked.to_dict()["primary_blocker"]["code"] == "plan_loss_budget_exceeded"
    assert validate_risk_decision(blocked.to_dict()).decision_id == blocked.decision_id

    for path, replacement in (
        (("decision_id",), "risk-decision-forged"),
        (("allow_exposure_increase",), True),
        (("bindings", "account_sha256"), "forged"),
        (("request", "account", "equity"), 99_999.0),
        (("metrics", "projected_leverage"), 0.01),
    ):
        tampered = deepcopy(blocked.to_dict())
        cursor = tampered
        for key in path[:-1]:
            cursor = cursor[key]
        cursor[path[-1]] = replacement
        with pytest.raises(ValueError):
            validate_risk_decision(tampered)


def test_decision_is_stale_for_a_different_current_request() -> None:
    original = request()
    decision = build_risk_decision(original)
    current = request(execution={"state_id": "execution-2", "positions": []})

    with pytest.raises(ValueError, match="stale"):
        validate_risk_decision(decision, expected_request=current)


@pytest.mark.parametrize(
    ("action", "allowed_field"),
    (("reduce_only", "allow_reduce_only"), ("cancel", "allow_cancel")),
)
def test_safe_action_permissions_are_explicit_and_malformed_identity_can_still_block(
    action: str,
    allowed_field: str,
) -> None:
    safe_request = request(action_class=action)
    allowed = build_risk_decision(safe_request)
    blocked = build_risk_decision(safe_request, blockers=[blocker("close_identity_invalid")])

    assert allowed.outcome == "allow"
    assert getattr(allowed, allowed_field) is True
    assert blocked.outcome == "block"
    assert getattr(blocked, allowed_field) is False
