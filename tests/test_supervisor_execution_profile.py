from __future__ import annotations

from copy import deepcopy

import pytest

from services.supervisor_execution_profile import (
    FAIL_CLOSED,
    PAPER_CONTINUOUS,
    SupervisorExecutionProfileError,
    resolve_supervisor_execution_profile,
)


def _config(profile: str) -> dict:
    return {
        "convergence": {"execution_profile": profile},
        "execution_engine": {
            "authoritative": "nautilus_paper",
            "paper_gate_override_approved": True,
            "real_money_eligible": False,
        },
    }


def test_fail_closed_profile_is_explicit_and_does_not_require_paper() -> None:
    config = {
        "convergence": {"execution_profile": FAIL_CLOSED},
        "execution_engine": {
            "authoritative": "future_live_adapter",
            "real_money_eligible": True,
        },
    }

    assert (
        resolve_supervisor_execution_profile(
            config,
            execution_name="future_live_adapter",
        )
        == FAIL_CLOSED
    )


@pytest.mark.parametrize(
    "convergence",
    [None, {}, {"execution_profile": ""}, {"execution_profile": "other"}],
)
def test_missing_or_unknown_profile_fails_closed(convergence) -> None:
    config = {"convergence": convergence}

    with pytest.raises(
        SupervisorExecutionProfileError,
        match="supervisor_execution_profile_invalid",
    ):
        resolve_supervisor_execution_profile(
            config,
            execution_name="nautilus_paper",
        )


def test_paper_continuous_requires_complete_paper_only_proof() -> None:
    assert (
        resolve_supervisor_execution_profile(
            _config(PAPER_CONTINUOUS),
            execution_name="nautilus_paper",
        )
        == PAPER_CONTINUOUS
    )


@pytest.mark.parametrize(
    ("mutation", "execution_name"),
    [
        ({"authoritative": "future_live_adapter"}, "nautilus_paper"),
        ({}, "future_live_adapter"),
        ({"real_money_eligible": True}, "nautilus_paper"),
        ({"real_money_eligible": None}, "nautilus_paper"),
        ({"paper_gate_override_approved": False}, "nautilus_paper"),
        ({"paper_gate_override_approved": None}, "nautilus_paper"),
        ({"authoritative": "live_paperish"}, "live_paperish"),
        ({"authoritative": "nautilus_paper"}, "legacy_paper"),
    ],
)
def test_paper_continuous_rejects_any_missing_paper_only_proof(
    mutation: dict,
    execution_name: str,
) -> None:
    config = deepcopy(_config(PAPER_CONTINUOUS))
    config["execution_engine"].update(mutation)

    with pytest.raises(
        SupervisorExecutionProfileError,
        match="paper_only_execution_profile_required",
    ):
        resolve_supervisor_execution_profile(
            config,
            execution_name=execution_name,
        )
