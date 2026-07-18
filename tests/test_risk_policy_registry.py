from __future__ import annotations

from copy import deepcopy

import pytest

from schemas.risk import build_risk_decision, build_risk_request
from services.risk_decision_store import FileRiskDecisionStore
from services.risk_policy_composition import (
    DEFAULT_GRID_RISK_POLICY_PLUGIN,
    build_risk_decision_store,
    build_risk_policy_registry,
    compose_grid_risk_policy,
)
from services.risk_policy_paper import (
    PaperGridRiskDecisionPort,
    grid_risk_evaluator,
    grid_risk_policy,
)
from services.risk_policy_registry import (
    DuplicateRiskPolicyPlugin,
    InvalidRiskPolicyPlugin,
    RiskPolicyPluginRegistry,
    UnknownRiskPolicyPlugin,
)
from services.risk_port import RiskDecisionPort, RiskDecisionStorePort


CONFIG = {
    "max_leverage": 10,
    "strategy_grid": {
        "max_plan_loss_pct": 0.1,
        "capital_utilization_cap": 1,
    },
}


class GenericRiskPolicy:
    name = "generic_risk"

    def evaluator_metadata(self):
        return {
            "name": self.name,
            "version": "generic-risk-v1",
            "source_hashes": {"generic_risk.py": "abc"},
            "code_sha256": "abc",
        }

    def resolve_policy(self, config):
        return {
            "policy_id": "generic-policy-v1",
            "max_leverage": config.get("max_leverage"),
        }

    def evaluate(self, request):
        return build_risk_decision(request)


GENERIC_IMPLEMENTATION = (
    f"{GenericRiskPolicy.__module__}.{GenericRiskPolicy.__qualname__}"
)


def _generic_registry(factory=GenericRiskPolicy) -> RiskPolicyPluginRegistry:
    return (
        RiskPolicyPluginRegistry()
        .register(
            "generic_risk",
            factory,
            implementation=GENERIC_IMPLEMENTATION,
            evaluator=GenericRiskPolicy().evaluator_metadata(),
        )
        .freeze()
    )


def test_default_risk_policy_registry_is_frozen_deterministic_and_complete() -> None:
    first = build_risk_policy_registry()
    second = build_risk_policy_registry()
    descriptor = first.descriptor(DEFAULT_GRID_RISK_POLICY_PLUGIN)
    port = first.build(DEFAULT_GRID_RISK_POLICY_PLUGIN)

    assert first.frozen is True
    assert first.names() == (DEFAULT_GRID_RISK_POLICY_PLUGIN,)
    assert first.fingerprint == second.fingerprint
    assert descriptor.implementation == (
        "services.risk_policy_paper.PaperGridRiskDecisionPort"
    )
    assert descriptor.to_dict()["evaluator"] == grid_risk_evaluator()
    assert isinstance(port, RiskDecisionPort)
    assert isinstance(port, PaperGridRiskDecisionPort)


def test_composition_defaults_only_when_key_is_absent_and_binds_runtime() -> None:
    default = compose_grid_risk_policy(CONFIG)
    explicit = compose_grid_risk_policy(
        {**CONFIG, "risk_policy": {"paper_grid": "paper_grid_risk"}}
    )

    assert default.descriptor == explicit.descriptor
    assert default.registry_fingerprint == explicit.registry_fingerprint
    assert default.port.evaluator_metadata() == explicit.port.evaluator_metadata()
    assert default.port.resolve_policy(CONFIG) == grid_risk_policy(CONFIG)

    for invalid in (
        {**CONFIG, "risk_policy": {"paper_grid": ""}},
        {**CONFIG, "risk_policy": {"paper_grid": "missing"}},
        {**CONFIG, "risk_policy": "paper_grid_risk"},
    ):
        with pytest.raises((ValueError, UnknownRiskPolicyPlugin)):
            compose_grid_risk_policy(invalid)


def test_registry_rejects_unfrozen_duplicate_late_and_unknown_plugins() -> None:
    evaluator = GenericRiskPolicy().evaluator_metadata()
    registry = RiskPolicyPluginRegistry().register(
        "generic_risk",
        GenericRiskPolicy,
        evaluator=evaluator,
    )

    with pytest.raises(InvalidRiskPolicyPlugin, match="frozen before use"):
        registry.build("generic_risk")
    with pytest.raises(DuplicateRiskPolicyPlugin):
        registry.register(
            "generic_risk",
            GenericRiskPolicy,
            evaluator=evaluator,
        )

    registry.freeze()
    with pytest.raises(InvalidRiskPolicyPlugin, match="registry is frozen"):
        registry.register(
            "late",
            GenericRiskPolicy,
            evaluator=evaluator,
        )
    with pytest.raises(UnknownRiskPolicyPlugin):
        registry.build("missing")


def test_registry_rejects_runtime_name_implementation_and_evaluator_drift() -> None:
    class WrongName(GenericRiskPolicy):
        name = "wrong"

    class WrongEvaluator(GenericRiskPolicy):
        def evaluator_metadata(self):
            value = deepcopy(super().evaluator_metadata())
            value["code_sha256"] = "changed"
            return value

    for factory, message in (
        (WrongName, "identity mismatch"),
        (WrongEvaluator, "implementation mismatch"),
    ):
        registry = _generic_registry(factory)
        with pytest.raises(InvalidRiskPolicyPlugin, match=message):
            registry.build("generic_risk")

    registry = (
        RiskPolicyPluginRegistry()
        .register(
            "generic_risk",
            GenericRiskPolicy,
            implementation=GENERIC_IMPLEMENTATION,
            evaluator={
                **GenericRiskPolicy().evaluator_metadata(),
                "code_sha256": "registered-drift",
            },
        )
        .freeze()
    )
    with pytest.raises(InvalidRiskPolicyPlugin, match="evaluator identity"):
        registry.build("generic_risk")


def test_selected_paper_policy_blocks_a_request_bound_to_another_evaluator() -> None:
    port = PaperGridRiskDecisionPort()
    evaluator = dict(port.evaluator_metadata())
    evaluator["version"] = "forged-v2"
    request = build_risk_request(
        checked_at="2026-07-18T01:00:00+00:00",
        scope="paper_manual_order",
        action_class="reduce_only",
        candidate={
            "kind": "manual_order",
            "event": "flatten",
            "trade_id": "trade-1",
        },
        account={"status": "not_required"},
        market={"status": "not_required"},
        execution={"status": "not_required"},
        policy=port.resolve_policy(CONFIG),
        evaluator=evaluator,
    )

    decision = port.evaluate(request).to_dict()

    assert decision["allow_reduce_only"] is False
    assert decision["primary_blocker"]["code"] == "risk_evaluator_mismatch"


def test_file_store_is_a_write_only_audit_port_and_rejects_tampering(
    tmp_path,
) -> None:
    store = build_risk_decision_store(tmp_path / "outputs")
    request = build_risk_request(
        checked_at="2026-07-18T01:00:00+00:00",
        scope="paper_grid",
        action_class="cancel",
        candidate={"cycle_id": "2026-07-18_DAY"},
        account={},
        market={},
        execution={},
        policy={"policy_id": "cancel-v1"},
        evaluator=grid_risk_evaluator(),
    )
    decision = build_risk_decision(request)

    assert isinstance(store, RiskDecisionStorePort)
    assert isinstance(store, FileRiskDecisionStore)
    assert not hasattr(store, "authorize")
    assert not hasattr(store, "load")
    assert store.persist(decision)["decision_id"] == decision.decision_id

    tampered = decision.to_dict()
    tampered["allow_cancel"] = False
    with pytest.raises(ValueError):
        store.persist(tampered)
