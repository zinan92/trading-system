from __future__ import annotations

import hashlib
from types import MappingProxyType

import pytest

from services.codex_newsletter_strategy_proposal import (
    CodexNewsletterStrategyProposal,
    build_codex_newsletter_prompt,
)
from services.strategy_proposal_composition import compose_strategy_proposal
from services.strategy_proposal_port import (
    MAX_NEWSLETTER_CHARS,
    STRATEGY_PROPOSAL_REQUEST_SCHEMA,
    StrategyProposalRequest,
)
from services.strategy_proposal_registry import (
    DuplicateStrategyProposalPlugin,
    InvalidStrategyProposalPlugin,
    StrategyProposalPluginRegistry,
    UnknownStrategyProposalPlugin,
)


def _request(**overrides) -> StrategyProposalRequest:
    values = {
        "cycle_id": "2026-07-10_NIGHT",
        "cycle_hours": 12,
        "market": {"symbol": "GOLD", "recent_closes": [4100.0, 4101.0]},
        "prev_cycle_range": 40.0,
        "volatility_context": {"status": "ready", "minimum_plan_range": 60.0},
        "previous_review": {"status": "missing", "cycle_id": "2026-07-10_DAY"},
        "replan_context": {},
        "newsletter_text": "## 黄金\n区间震荡。",
    }
    values.update(overrides)
    return StrategyProposalRequest(**values)


def test_strategy_proposal_request_is_versioned_bounded_and_deeply_immutable() -> None:
    market = {"symbol": "GOLD", "recent_closes": [4100.0]}
    request = _request(market=market)
    market["symbol"] = "MUTATED"

    assert request.schema_version == STRATEGY_PROPOSAL_REQUEST_SCHEMA
    assert request.market["symbol"] == "GOLD"
    assert isinstance(request.market, MappingProxyType)
    assert request.market["recent_closes"] == (4100.0,)
    with pytest.raises(TypeError):
        request.market["symbol"] = "MUTATED"  # type: ignore[index]
    with pytest.raises(ValueError, match="exceeds"):
        _request(newsletter_text="x" * (MAX_NEWSLETTER_CHARS + 1))


def test_codex_newsletter_adapter_preserves_prompt_and_rejects_non_object() -> None:
    captured: dict[str, str] = {}

    def decide(prompt: str) -> dict:
        captured["prompt"] = prompt
        return {"direction": "neutral"}

    request = _request(replan_context={"status": "confirmed"})
    adapter = CodexNewsletterStrategyProposal({}, decision_provider=decide)

    assert adapter.propose(request) == {"direction": "neutral"}
    assert captured["prompt"] == build_codex_newsletter_prompt(request)
    assert "禁止读取、推断或继承任何人工轨计划" in captured["prompt"]
    assert "旧 range 已失效" in captured["prompt"]
    assert request.newsletter_text in captured["prompt"]

    invalid = CodexNewsletterStrategyProposal({}, decision_provider=lambda _prompt: [])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must return a JSON object"):
        invalid.propose(request)


def test_codex_newsletter_prompt_is_byte_compatible_with_pre_port_planner() -> None:
    request = StrategyProposalRequest(
        cycle_id="2026-07-10_NIGHT",
        cycle_hours=12,
        market={
            "symbol": "GOLD",
            "timeframe": "1m",
            "provider": "binance_usdm",
            "bar_count": 1,
            "start": "2026-07-10T13:00:00+00:00",
            "end": "2026-07-10T13:00:00+00:00",
            "open": 4115.0,
            "high": 4118.0,
            "low": 4110.0,
            "close": 4114.0,
            "recent_closes": [4114.0],
        },
        prev_cycle_range=40.0,
        volatility_context={"status": "ready", "minimum_plan_range": 60.0},
        previous_review={"status": "missing"},
        replan_context={"status": "confirmed"},
        newsletter_text="## 黄金\n区间震荡。",
    )

    digest = hashlib.sha256(build_codex_newsletter_prompt(request).encode()).hexdigest()

    assert digest == "c62aef4a4e745cf4e0c4c024c7cca350e5c6cf55d178657b62d38ca35ea94618"


def test_strategy_proposal_registry_is_explicit_frozen_and_fail_closed() -> None:
    class Proposal:
        def propose(self, request: StrategyProposalRequest) -> dict:
            return {"cycle_id": request.cycle_id}

    registry = StrategyProposalPluginRegistry()
    descriptor = registry.register("fake", lambda _params: Proposal())
    with pytest.raises(DuplicateStrategyProposalPlugin):
        registry.register("FAKE", lambda _params: Proposal())
    registry.freeze()

    assert descriptor.plan_source == "machine_strategy_proposal:fake"
    assert descriptor.required_context == ()
    assert registry.build("fake", {}).propose(_request())["cycle_id"] == "2026-07-10_NIGHT"
    assert len(registry.fingerprint) == 64
    with pytest.raises(UnknownStrategyProposalPlugin, match="unknown strategy proposal plugin"):
        registry.build("unknown", {})
    with pytest.raises(InvalidStrategyProposalPlugin, match="frozen"):
        registry.register("late", lambda _params: Proposal())


def test_strategy_proposal_registry_rejects_invalid_factory_product_and_capability() -> None:
    registry = StrategyProposalPluginRegistry()
    registry.register("invalid", lambda _params: object())
    with pytest.raises(InvalidStrategyProposalPlugin, match=r"without propose\(\)"):
        registry.build("invalid", {})

    with pytest.raises(InvalidStrategyProposalPlugin, match="declare propose capability"):
        StrategyProposalPluginRegistry().register(
            "missing-capability",
            lambda _params: object(),
            capabilities=("explain",),
        )


def test_configured_strategy_proposal_composition_defaults_compatibly_and_rejects_typos() -> None:
    composition = compose_strategy_proposal(
        {"machine_planner": {"model": "test"}},
        decision_provider=lambda _prompt: {"direction": "neutral"},
    )

    assert composition.descriptor.name == "codex_newsletter"
    assert composition.descriptor.plan_source == "machine_ai_newsletter"
    assert composition.descriptor.required_context == ("newsletter",)
    assert len(composition.registry_fingerprint) == 64
    assert composition.audit_dict()["plugin"]["default_decision_mode"] == "ai_newsletter"

    with pytest.raises(UnknownStrategyProposalPlugin, match="typo"):
        compose_strategy_proposal({"machine_planner": {"plugin": "typo"}})
    with pytest.raises(InvalidStrategyProposalPlugin, match="empty"):
        compose_strategy_proposal({"machine_planner": {"plugin": "  "}})
