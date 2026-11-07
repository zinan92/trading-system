"""Desk Venue Modules (ADR 0007, #1378): a frozen registry of declared capabilities."""
from __future__ import annotations

import pytest

from trading_desk.venues import CAPABILITIES, DEFAULT, VenueModule, build


class Bare(VenueModule):
    kind = "bare"


def test_the_default_registry_declares_what_each_kind_did_before() -> None:
    assert DEFAULT.kinds() == ("hl_testnet", "xau_paper", "watch", "okx_public")
    assert DEFAULT.having("bars_health") == ("hl_testnet", "okx_public")
    assert DEFAULT.having("execute") == ("hl_testnet", "xau_paper")
    assert DEFAULT.having("stop") == DEFAULT.having("rearm_hold") == DEFAULT.having("price_alerts") == ("hl_testnet",)
    assert DEFAULT.having("grid_plan") == ("hl_testnet", "xau_paper")
    assert DEFAULT.having("futures_overview") == ("watch",)


def test_the_registry_is_frozen() -> None:
    with pytest.raises(TypeError):
        DEFAULT.modules["new"] = Bare()  # type: ignore[index]


def test_an_unknown_kind_is_an_error_not_a_default() -> None:
    with pytest.raises(ValueError):
        DEFAULT.of({"kind": "nope"})


def test_an_unknown_capability_is_refused() -> None:
    class Odd(Bare):
        capabilities = frozenset({"teleport"})
    with pytest.raises(ValueError):
        build(Odd())


def test_a_repeated_kind_is_refused() -> None:
    with pytest.raises(ValueError):
        build(Bare(), Bare())


def test_a_capability_the_module_cannot_back_is_dropped() -> None:
    """Declaring execute is not enough: without preview, execute and a started text it stays off."""
    class Claims(Bare):
        capabilities = frozenset({"execute", "preview", "grid_plan", "bars"})
    module = build(Claims()).get("bare")
    assert module.capabilities == frozenset()
    assert not module.can("execute")


def test_capabilities_are_never_inferred_from_methods() -> None:
    class Quiet(Bare):
        plan_template = {"rungs": 1}
        started_text = "x"
        def preview(self, executor, asset, plan):
            return {}
        def execute(self, executor, asset, plan, *, shown_max_loss, judgment_id):
            return {}
    assert build(Quiet()).get("bare").capabilities == frozenset()


def test_every_declared_capability_is_known() -> None:
    for kind in DEFAULT.kinds():
        assert DEFAULT.get(kind).capabilities <= CAPABILITIES
