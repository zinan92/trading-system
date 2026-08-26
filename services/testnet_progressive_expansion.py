"""Progressive, single-slice admission for the full Testnet universe."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence


EXPANSION_SCHEMA = "testnet-progressive-expansion-v1"


class TestnetProgressiveExpansionError(ValueError):
    """Stable expansion blocker."""

    __test__ = False

    def __init__(self, code: str, evidence: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.evidence = dict(evidence or {})
        super().__init__(self.code)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TestnetProgressiveExpansionError(f"{field}_invalid") from exc
    if not result.is_finite() or result <= 0:
        raise TestnetProgressiveExpansionError(f"{field}_invalid")
    return result


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def bounded_canary_caps(equity: Any) -> dict[str, Any]:
    """Return the approved first-execution caps from freshly read equity."""

    amount = _decimal(equity, "equity")
    return {
        "gross_notional": _decimal_text(min(amount * Decimal("0.10"), Decimal("100"))),
        "worst_case_loss": _decimal_text(min(amount * Decimal("0.05"), Decimal("50"))),
        "max_open_positions": 1,
    }


def _candidate_identity(candidate: Mapping[str, Any]) -> tuple[str, str, int]:
    if not isinstance(candidate, Mapping):
        raise TestnetProgressiveExpansionError("candidate_shape_invalid")
    asset = str(candidate.get("asset") or "").strip().upper()
    instrument = str(candidate.get("instrument_id") or "").strip()
    try:
        rank = int(candidate.get("rank"))
    except (TypeError, ValueError) as exc:
        raise TestnetProgressiveExpansionError("candidate_rank_invalid") from exc
    if not asset or not instrument or rank <= 0:
        raise TestnetProgressiveExpansionError("candidate_identity_invalid")
    return asset, instrument, rank


def _scope(preflight: Mapping[str, Any]) -> str:
    return "account" if str(preflight.get("scope") or "").lower() == "account" else "pair"


class TestnetProgressiveExpansion:
    """Admit one pair at a time after pair-specific preflight."""

    __test__ = False

    def __init__(self) -> None:
        self._selected_asset: str | None = None

    def admit(
        self,
        candidate: Mapping[str, Any],
        *,
        preflight: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        canary: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
        equity: Any,
        side_effect_started: bool = False,
        selected_asset: str | None = None,
    ) -> dict[str, Any]:
        asset, instrument, rank = _candidate_identity(candidate)
        if side_effect_started and str(selected_asset or self._selected_asset or "").upper() != asset:
            raise TestnetProgressiveExpansionError(
                "asset_switch_forbidden",
                {"selected_asset": selected_asset or self._selected_asset, "requested_asset": asset},
            )
        if not callable(preflight) or not callable(canary):
            raise TestnetProgressiveExpansionError("expansion_ports_required")
        try:
            readiness = preflight(dict(candidate))
        except Exception as exc:  # noqa: BLE001 - unknown preflight is a local blocker.
            readiness = {"status": "blocked", "reason": "pair_preflight_unknown", "scope": "pair", "detail": type(exc).__name__}
        if not isinstance(readiness, Mapping):
            readiness = {"status": "blocked", "reason": "pair_preflight_invalid", "scope": "pair"}
        readiness_status = str(readiness.get("status") or "").lower()
        if readiness_status not in {"pass", "ready", "ok"}:
            reason = str(readiness.get("reason") or "pair_preflight_blocked")
            scope = _scope(readiness)
            return {
                "schema_version": EXPANSION_SCHEMA,
                "status": "portfolio_hold" if scope == "account" else "pair_blocked",
                "global_hold": scope == "account",
                "selected_asset": None,
                "blocker": reason,
                "candidate": {"asset": asset, "instrument_id": instrument, "rank": rank},
                "preflight": dict(readiness),
                "canary": None,
                "caps": bounded_canary_caps(equity),
            }
        caps = bounded_canary_caps(equity)
        try:
            outcome = canary(dict(candidate), caps)
        except Exception as exc:  # noqa: BLE001 - a canary outcome is unknown, never retried here.
            outcome = {"status": "unknown", "reason": "canary_unknown", "scope": "pair", "detail": type(exc).__name__}
        if not isinstance(outcome, Mapping):
            outcome = {"status": "unknown", "reason": "canary_result_invalid", "scope": "pair"}
        outcome_status = str(outcome.get("status") or "").lower()
        if outcome_status not in {"pass", "ready", "ok", "admitted"}:
            reason = str(outcome.get("reason") or "canary_unknown")
            scope = _scope(outcome)
            return {
                "schema_version": EXPANSION_SCHEMA,
                "status": "portfolio_hold" if scope == "account" else "pair_blocked",
                "global_hold": scope == "account",
                "selected_asset": None,
                "blocker": reason,
                "candidate": {"asset": asset, "instrument_id": instrument, "rank": rank},
                "preflight": dict(readiness),
                "canary": dict(outcome),
                "caps": caps,
            }
        self._selected_asset = asset
        return {
            "schema_version": EXPANSION_SCHEMA,
            "status": "admitted",
            "global_hold": False,
            "selected_asset": asset,
            "selected_instrument_id": instrument,
            "blocker": None,
            "candidate": {"asset": asset, "instrument_id": instrument, "rank": rank},
            "preflight": dict(readiness),
            "canary": dict(outcome),
            "caps": caps,
        }

    def admit_ranked(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        preflight: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        canary: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
        equity: Any,
    ) -> dict[str, Any]:
        rows = sorted(candidates, key=lambda item: _candidate_identity(item)[2])
        inventory: list[dict[str, Any]] = []
        for candidate in rows:
            outcome = self.admit(
                candidate,
                preflight=preflight,
                canary=canary,
                equity=equity,
            )
            row = {
                "asset": outcome["candidate"]["asset"],
                "instrument_id": outcome["candidate"]["instrument_id"],
                "rank": outcome["candidate"]["rank"],
                "status": "blocked" if outcome["status"] == "pair_blocked" else outcome["status"],
                "scope": "account" if outcome["global_hold"] else "pair",
                "blockers": [outcome["blocker"]] if outcome.get("blocker") else [],
            }
            inventory.append(row)
            if outcome["status"] == "admitted":
                return {
                    **outcome,
                    "inventory": inventory,
                }
            if outcome["global_hold"]:
                return {
                    **outcome,
                    "inventory": inventory,
                }
        return {
            "schema_version": EXPANSION_SCHEMA,
            "status": "blocked",
            "global_hold": False,
            "selected_asset": None,
            "selected_instrument_id": None,
            "blocker": "no_candidate_admitted",
            "candidate": None,
            "preflight": None,
            "canary": None,
            "caps": bounded_canary_caps(equity),
            "inventory": inventory,
        }


__all__ = [
    "EXPANSION_SCHEMA",
    "TestnetProgressiveExpansion",
    "TestnetProgressiveExpansionError",
    "bounded_canary_caps",
]
