"""Assemble an attended Testnet proof from Dashboard durable evidence.

This module is deliberately a small adapter around the existing proof CLI.  It
does not create a strategy, infer Park approval, or change the Coordinator
contract.  In dry-run mode all Coordinator writes happen in a temporary copy
of the supplied output root.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Mapping, Sequence
from unittest.mock import patch

from pipelines import testnet_automation_proof as proof
from services.dashboard_control_plane import canonical_preview_digest
from services.journal_store import load_json, write_json
from services.park_confirmation_ledger import (
    DurableParkConfirmationError,
    is_dashboard_confirm_and_run_record,
    parse_durable_confirmation,
)
from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from services.strategy_control_plane import StrategyControlMachineError
from services.hyperliquid_testnet_market_reader import HyperliquidTestnetMarketReader
from services.dashboard_control_plane import public_catalog_loader
from services.grid_testnet_lifecycle import GridTestnetLifecycle, GridTestnetLifecycleError


PREVIEWS = Path("dashboard_control_plane/previews.json")
DASHBOARD_CONFIRMATIONS = Path("dashboard_control_plane/confirmations.json")
PARK_CONFIRMATIONS = Path("park_strategy/confirmations.jsonl")


class ProofDriverError(ValueError):
    def __init__(self, reason_code: str, **details: Any) -> None:
        self.reason_code = reason_code
        self.details = details
        super().__init__(reason_code)


_MARKET_BBO_FIELDS = ("bid", "ask", "mid")
_MARKET_READ_ATTEMPTS = 5

# These are the fields consumed immediately before GridTestnetLifecycle can
# submit its first order.  Keep this list explicit: adding a lifecycle gate
# without adding its Dashboard projection must fail closed in this driver.
LIFECYCLE_RISK_FIELDS = (
    "maximum_loss_at_full_depth",
    "equity",
    "leverage_limit",
    "max_notional",
    "max_open_orders",
    "max_open_positions",
    "max_slippage",
)


def _bbo_check(market: Mapping[str, Any]) -> dict[str, Any]:
    """Return an auditable BBO check without normalising or changing facts."""

    try:
        bid, ask, mid = (Decimal(str(market[field])) for field in _MARKET_BBO_FIELDS)
        passed = bid < ask and bid <= mid <= ask
    except (KeyError, InvalidOperation, TypeError, ValueError):
        bid = ask = mid = None
        passed = False
    return {
        "bid": str(bid) if bid is not None else market.get("bid"),
        "mid": str(mid) if mid is not None else market.get("mid"),
        "ask": str(ask) if ask is not None else market.get("ask"),
        "passed": passed,
    }


def build_market_document(
    preview: Mapping[str, Any], binding_market: Mapping[str, Any], *, instrument_id: str
) -> dict[str, Any]:
    """Combine Dashboard quality with the protected binding fact.

    Missing fields remain missing, except for the protected profile's explicit
    no-fallback invariant, so proof validation stays fail-closed.
    """
    dashboard_market = preview.get("market")
    if not isinstance(dashboard_market, Mapping) or not isinstance(binding_market, Mapping):
        raise ProofDriverError("market_fact_invalid")
    binding = dict(binding_market)
    source = str(binding.get("source") or "").strip().lower()
    price = binding.get("price")
    observed_at = binding.get("observed_at")
    if not source or price in (None, "") or observed_at in (None, ""):
        raise ProofDriverError("market_fact_invalid")

    market = dict(dashboard_market)
    # Keep Dashboard's richer quality fields when the adapter exposes only its
    # sparse contract, while binding metadata remains authoritative when set.
    market.update({key: value for key, value in binding.items() if value is not None})
    market["source"] = source
    market["instrument_id"] = str(binding.get("instrument_id") or instrument_id)
    market["mid"] = price
    market["observed_at"] = observed_at
    # The protected exact-source Testnet profile has no fallback by contract.
    market.setdefault("fallback_policy", "none")
    if "fresh" not in market and "freshness" in binding:
        market["fresh"] = str(binding["freshness"]).lower() == "fresh"

    missing = [field for field in proof._MARKET_REQUIRED if field not in market]
    if missing:
        raise ProofDriverError("market_facts_missing", fields=missing)
    if not _bbo_check(market)["passed"]:
        raise ProofDriverError("market_bbo_inconsistent", market_check=_bbo_check(market))
    return market


def read_coherent_market(
    preview: Mapping[str, Any],
    broker: object,
    *,
    instrument_id: str,
    sleep_fn: Any = time.sleep,
    market_reader: Any = None,
    max_attempts: int = _MARKET_READ_ATTEMPTS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read a complete market snapshot and retry only an inconsistent BBO.

    Some protected bindings expose only the executable ticker. In that case
    the credential-free Hyperliquid reader supplies the complete single-read
    market snapshot; the binding price and identity are still checked against
    it before it is admitted to the proof inputs.
    """

    checks: list[dict[str, Any]] = []
    reader = market_reader or HyperliquidTestnetMarketReader()
    for attempt in range(1, max_attempts + 1):
        observed_at = datetime.now(timezone.utc)
        try:
            raw_binding = broker.market_fact(instrument_id=instrument_id, now=observed_at)
        except Exception as exc:  # noqa: BLE001 - redact provider details.
            raise ProofDriverError("market_fact_unavailable") from exc
        if not isinstance(raw_binding, Mapping):
            raise ProofDriverError("market_fact_invalid")
        try:
            if all(field in raw_binding for field in proof._MARKET_REQUIRED):
                market = build_market_document(preview, raw_binding, instrument_id=instrument_id)
            else:
                try:
                    raw_reader = reader.read(instrument_id)
                except Exception as exc:  # noqa: BLE001 - redact provider details.
                    raise ProofDriverError("market_fact_unavailable") from exc
                if not isinstance(raw_reader, Mapping):
                    raise ProofDriverError("market_fact_invalid")
                if str(raw_binding.get("price")) != str(raw_reader.get("price")):
                    raise ProofDriverError("market_price_mismatch")
                combined = dict(raw_reader)
                # Binding identity is authoritative for the later proof gate.
                for field in ("source", "mapping_revision"):
                    if raw_binding.get(field) not in (None, ""):
                        combined[field] = raw_binding[field]
                market = build_market_document(preview, combined, instrument_id=instrument_id)
        except ProofDriverError as exc:
            if exc.reason_code != "market_bbo_inconsistent":
                raise
            check = dict(exc.details.get("market_check") or {})
            check.update({"attempt": attempt})
            checks.append(check)
            if attempt == max_attempts:
                raise ProofDriverError("market_bbo_inconsistent", attempts=checks) from exc
            sleep_fn(1.0)
            continue
        check = _bbo_check(market)
        check.update({"attempt": attempt})
        checks.append(check)
        return market, checks
    raise ProofDriverError("market_bbo_inconsistent", attempts=checks)


def _rows(path: Path) -> list[dict[str, Any]]:
    try:
        if path.suffix == ".jsonl":
            value = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            value = load_json(path)
    except Exception as exc:  # noqa: BLE001 - redact filesystem details.
        raise ProofDriverError("evidence_unavailable") from exc
    values = value if isinstance(value, list) else [value]
    return [dict(row) for row in values if isinstance(row, Mapping)]


def _latest_activation(output_root: Path, activation_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    previews = _rows(output_root / PREVIEWS)
    confirmations = _rows(output_root / DASHBOARD_CONFIRMATIONS)
    matches = [row for row in confirmations if row.get("activation_id") == activation_id and row.get("status") == "confirmed"]
    if not matches:
        raise ProofDriverError("dashboard_confirmation_not_found")
    confirmation = matches[-1]
    digest = str(confirmation.get("preview_digest") or confirmation.get("plan_digest") or "")
    candidates = [row for row in previews if str(row.get("preview_digest") or "") == digest]
    if not candidates:
        raise ProofDriverError("dashboard_preview_not_found")
    preview = candidates[-1]
    if canonical_preview_digest(preview) != digest:
        raise ProofDriverError("dashboard_preview_digest_invalid")
    for field in ("strategy_family", "instrument_id", "plan_digest", "strategy_session_id", "strategy_revision_id"):
        if field in confirmation and field in preview and field != "plan_digest" and confirmation.get(field) != preview.get(field):
            raise ProofDriverError("dashboard_identity_mismatch", field=field)
    if str(preview.get("plan_digest") or digest) != digest:
        raise ProofDriverError("dashboard_plan_digest_mismatch")
    return preview, confirmation


def build_plan(
    preview: Mapping[str, Any],
    confirmation: Mapping[str, Any],
    *,
    catalog_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project the immutable Dashboard preview into the proof plan shape."""
    body = preview.get("preview")
    if not isinstance(body, Mapping):
        raise ProofDriverError("dashboard_preview_payload_missing")
    family = str(preview.get("strategy_family") or body.get("strategy_type") or "").lower()
    if family not in {"dca", "grid"}:
        raise ProofDriverError("strategy_family_invalid")
    preview_market = preview.get("market") if isinstance(preview.get("market"), Mapping) else {}
    instrument = preview.get("instrument") if isinstance(preview.get("instrument"), Mapping) else {}
    execution_context = {
        "venue_profile_id": preview.get("venue_profile_id"),
        "broker_id": preview.get("broker_id") or body.get("broker_id") or preview_market.get("broker_id") or (body.get("market") or {}).get("broker_id"),
        "environment": preview.get("environment") or body.get("environment") or preview_market.get("environment") or (body.get("market") or {}).get("environment"),
        "transport_profile": preview.get("transport_profile"),
        "instrument_id": preview.get("instrument_id"),
        "runtime_id": preview.get("runtime_id"),
        "capability_revision": preview.get("capability_revision"),
        "price_tick": preview.get("price_tick") or instrument.get("price_tick") or instrument.get("price_increment"),
        "quantity_step": preview.get("quantity_step") or instrument.get("quantity_step") or instrument.get("size_increment"),
        "minimum_quantity": preview.get("minimum_quantity") or instrument.get("minimum_quantity") or instrument.get("min_quantity"),
        "minimum_notional": preview.get("minimum_notional") or instrument.get("minimum_notional") or instrument.get("min_notional"),
    }
    if catalog_rows is not None:
        execution_context.update(_catalog_instrument_constraints(
            catalog_rows, instrument_id=str(preview.get("instrument_id") or "").strip()
        ))
    cycle_id = next(
        (
            value
            for value in (
                preview.get("cycle_id"),
                body.get("cycle_id"),
                confirmation.get("cycle_id"),
                (preview.get("recording_window") or {}).get("cycle_id")
                if isinstance(preview.get("recording_window"), Mapping)
                else None,
                (confirmation.get("recording_window") or {}).get("cycle_id")
                if isinstance(confirmation.get("recording_window"), Mapping)
                else None,
            )
            if str(value or "").strip()
        ),
        "",
    )
    if not cycle_id:
        raise ProofDriverError("dashboard_cycle_id_missing")
    execution_context["cycle_id"] = cycle_id
    plan: dict[str, Any] = {
        "schema_version": "strategy-plan-v1",
        "strategy_type": family,
        "strategy_plan_id": f"dashboard-plan:{str(preview['preview_digest']).replace('sha256:', '')[:16]}",
        "version": int(preview.get("version") or body.get("version") or body.get("plan_version") or 1),
        "cycle_id": cycle_id,
        "locked_at": str(body.get("market", {}).get("timestamp") or datetime.now(timezone.utc).isoformat()),
        "strategy_session_id": confirmation.get("strategy_session_id"),
        "strategy_revision_id": confirmation.get("strategy_revision_id"),
        "plan_digest": preview.get("preview_digest"),
        "instrument_id": preview.get("instrument_id"),
        "direction": body.get("direction"),
        "execution_context": execution_context,
    }
    if not plan["strategy_session_id"] or not plan["strategy_revision_id"]:
        raise ProofDriverError("dashboard_strategy_identity_missing")
    if family == "grid":
        orders = body.get("orders")
        if not isinstance(orders, list) or not orders:
            raise ProofDriverError("dashboard_grid_orders_missing")
        plan["lower_price_boundary"] = body.get("range", {}).get("low")
        plan["upper_price_boundary"] = body.get("range", {}).get("high")
        plan["lower_boundary"] = plan["lower_price_boundary"]
        plan["upper_boundary"] = plan["upper_price_boundary"]
        body_grid = body.get("grid") if isinstance(body.get("grid"), Mapping) else {}
        preview_grid = preview.get("grid") if isinstance(preview.get("grid"), Mapping) else {}
        hard_stop = body_grid.get("hard_stop") or body_grid.get("hard_stop_price")
        if hard_stop not in (None, "") and preview_grid.get("hard_stop_source") != "operator_configured":
            if str(body.get("direction") or "").lower() == "long" and float(hard_stop) >= float(plan["lower_price_boundary"]):
                hard_stop = None
            elif str(body.get("direction") or "").lower() == "short" and float(hard_stop) <= float(plan["upper_price_boundary"]):
                hard_stop = None
        if hard_stop in (None, "") and preview_grid.get("hard_stop_source") == "operator_configured":
            hard_stop = preview_grid.get("hard_stop") or preview_grid.get("hard_stop_price")
        plan["hard_stop"] = hard_stop
        plan["grid"] = {
            "rungs": [
                {"rung": int(row.get("level", index)), "side": row.get("side"), "price": row.get("price"),
                 "quantity": row.get("quantity") or row.get("size"),
                 "tp": row.get("tp") or row.get("take_profit"),
                 "hard_stop": row.get("sl") or row.get("hard_stop") or row.get("stop_loss")
                 or body_grid.get("hard_stop") or body_grid.get("hard_stop_price")
                 or preview_grid.get("hard_stop") or preview_grid.get("hard_stop_price")}
                for index, row in enumerate(orders, start=1) if isinstance(row, Mapping)
            ],
            "mode": body_grid.get("mode") or preview_grid.get("mode"),
            "spacing": body_grid.get("spacing") or preview_grid.get("spacing"),
            "hard_stop": hard_stop,
        }
    else:
        entries = body.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ProofDriverError("dashboard_dca_entries_missing")
        dca = body.get("dca") if isinstance(body.get("dca"), Mapping) else {}
        plan["dca"] = {
            "entry_levels": [row.get("price") for row in entries if isinstance(row, Mapping)],
            "notional_per_addition": dca.get("notional_per_addition"),
            "max_additions": dca.get("max_additions"),
            "target_price": dca.get("target_price"),
            "stop_price": dca.get("stop_price"),
            "loop_enabled": False,
        }
    risk = {}
    for source in (preview.get("risk"), body.get("risk")):
        if isinstance(source, Mapping):
            risk.update(source)
    risk_gate = preview.get("risk_gate") if isinstance(preview.get("risk_gate"), Mapping) else {}
    plan["risk"] = dict(risk)
    if family == "grid":
        plan["risk_gate"] = dict(risk_gate)
        plan["risk_budget"] = _grid_risk_budget(
            preview=preview,
            body=body,
            orders=orders,
            risk=risk,
            risk_gate=risk_gate,
        )
    return plan


def _catalog_instrument_constraints(
    rows: Sequence[Mapping[str, Any]], *, instrument_id: str
) -> dict[str, Any]:
    """Project Dashboard catalog identity through standard-broker facts."""

    row = next(
        (item for item in rows if str(item.get("instrument_id") or item.get("symbol") or "").strip() == instrument_id),
        None,
    )
    if row is None:
        raise ProofDriverError("instrument_catalog_instrument_missing", instrument_id=instrument_id)
    size_decimals = row.get("size_decimals", row.get("szDecimals"))
    if size_decimals in (None, ""):
        raise ProofDriverError("instrument_catalog_facts_missing", fields=["size_decimals"])
    try:
        # This is the public standard-broker Hyperliquid mapping: quantity step,
        # minimum notional, minimum quantity, and price rule are canonical facts.
        from standard_broker.adapters.hyperliquid import HyperliquidInstrumentAdapter

        asset = str(row.get("asset") or instrument_id.split("-", 1)[0]).strip()
        facts = HyperliquidInstrumentAdapter.from_meta(
            {"universe": [{"name": asset, "szDecimals": int(size_decimals),
                            "maxLeverage": row.get("max_leverage", row.get("maxLeverage"))}]},
            revision="dashboard-catalog",
        ).get(instrument_id)
        price_tick = Decimal(1).scaleb(-facts.price_rule.max_decimal_places)
    except (ImportError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ProofDriverError("instrument_catalog_facts_invalid") from exc
    return {
        "price_tick": str(price_tick),
        "quantity_step": str(facts.quantity_step),
        "minimum_quantity": str(facts.minimum_quantity or facts.quantity_step),
        "minimum_notional": str(facts.minimum_notional),
        "max_leverage": row.get("max_leverage", row.get("maxLeverage")),
    }


def _first_value(*sources: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _grid_risk_budget(
    *, preview: Mapping[str, Any], body: Mapping[str, Any], orders: Sequence[Any],
    risk: Mapping[str, Any], risk_gate: Mapping[str, Any],
) -> dict[str, Any]:
    """Project Dashboard risk facts into the lifecycle's exact input shape."""

    grid = body.get("grid") if isinstance(body.get("grid"), Mapping) else {}
    preview_grid = preview.get("grid") if isinstance(preview.get("grid"), Mapping) else {}
    account = preview.get("account") if isinstance(preview.get("account"), Mapping) else {}
    body_account = body.get("account") if isinstance(body.get("account"), Mapping) else {}
    market = preview.get("market") if isinstance(preview.get("market"), Mapping) else {}
    # Effective notional is the subtractive Portfolio Gate result.  It is the
    # only notional permitted to reach an execution lifecycle.
    values = {
        "maximum_loss_at_full_depth": _first_value(
            risk, preview.get("risk") if isinstance(preview.get("risk"), Mapping) else {},
            keys=("maximum_loss_at_full_depth", "max_loss", "maximum_loss"),
        ),
        "equity": _first_value(account, body_account, risk, keys=("equity", "account_equity")),
        "leverage_limit": _first_value(
            risk, grid, preview_grid,
            keys=("leverage_limit", "selected_leverage", "effective_leverage", "leverage"),
        ),
        "max_notional": _first_value(
            risk_gate, risk, grid, preview_grid,
            keys=("effective_notional", "gross_notional", "max_notional", "maximum_notional", "total_grid_notional"),
        ),
        "max_open_orders": _first_value(
            risk, grid, preview_grid, keys=("max_open_orders", "maximum_open_orders")
        ),
        "max_open_positions": _first_value(
            risk, grid, preview_grid, keys=("max_open_positions", "maximum_open_positions")
        ),
        "max_slippage": _first_value(
            risk, grid, preview_grid, market, keys=("max_slippage", "slippage_budget")
        ),
    }
    # The preview's order list is the authoritative full-depth ladder.  A
    # missing explicit capacity is not guessed: the ladder itself can prove
    # the minimum capacity needed for this immutable plan.
    if values["max_open_orders"] in (None, ""):
        values["max_open_orders"] = len(orders)
    if values["max_open_positions"] in (None, ""):
        values["max_open_positions"] = len(orders)
    # Preserve display/audit facts without making lifecycle calculations use
    # a second, independently derived risk model.
    for key in ("estimated_margin_at_full_depth", "estimated_margin", "margin", "actual_leverage_at_full_depth", "actual_leverage", "size_precision", "price_precision"):
        value = _first_value(risk, grid, preview_grid, keys=(key,))
        if value in (None, "") and key in {"size_precision", "price_precision"}:
            instrument = preview.get("instrument") if isinstance(preview.get("instrument"), Mapping) else {}
            value = instrument.get(key)
        if value not in (None, ""):
            values[key] = value
    return values


def validate_grid_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Run every Grid lifecycle pre-write check offline and return evidence."""

    checks = [{"name": field, "passed": False, "source": "plan.risk_budget"} for field in LIFECYCLE_RISK_FIELDS]
    risk = plan.get("risk_budget") if isinstance(plan.get("risk_budget"), Mapping) else {}
    for check in checks:
        value = risk.get(check["name"])
        try:
            check["passed"] = value not in (None, "") and float(value) > 0
        except (TypeError, ValueError):
            check["passed"] = False
        if not check["passed"]:
            raise ProofDriverError("lifecycle_preflight_missing", fields=[check["name"]], checks=checks)
    try:
        identity = GridTestnetLifecycle._identity(plan)
        # _rungs is state-free but remains an instance method for lifecycle
        # cohesion; this validator never invokes the broker or writes state.
        rungs = GridTestnetLifecycle(Path("."), object())._rungs(plan, identity)
        GridTestnetLifecycle._validate_full_depth_risk(plan, rungs)
        venue_check = _validate_grid_instrument_constraints(plan, rungs)
        class _OfflineBroker:
            # The lifecycle reaches its pre-write gate, then stops at the
            # missing capability boundary without any broker/network I/O.
            protection_adapter = None
            account_adapter = None
            broker_config = {
                "broker_id": "hyperliquid",
                "environment": "testnet",
                "account_id": "offline-validation",
                "release_sha": "offline",
                "ledger_namespace": "offline-validation",
            }

        with tempfile.TemporaryDirectory(prefix="grid-plan-validation-") as root:
            state = GridTestnetLifecycle(Path(root), _OfflineBroker()).start(
                dict(plan),
                timestamp=str(plan.get("locked_at") or datetime.now(timezone.utc).isoformat()),
            )
        if state.get("status") != "blocked_protection":
            raise GridTestnetLifecycleError("offline_validation_reached_unexpected_state")
        checks.extend([
            {"name": "grid_geometry", "passed": True, "source": "preview.grid/orders"},
            {"name": "full_depth_risk", "passed": True, "source": "lifecycle"},
            venue_check,
            {"name": "start_grid_session_prewrite", "passed": True, "source": "fake_broker"},
        ])
    except ProofDriverError:
        raise
    except (GridTestnetLifecycleError, ValueError) as exc:
        raise ProofDriverError("lifecycle_preflight_blocked", checks=checks, detail=str(exc)) from exc
    return {"status": "passed", "checks": checks, "rung_count": len(rungs)}


def _validate_grid_instrument_constraints(
    plan: Mapping[str, Any], rungs: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Validate effective rungs against the selected Instrument Catalog facts."""

    context = plan.get("execution_context") if isinstance(plan.get("execution_context"), Mapping) else {}
    facts = {
        key: context.get(key)
        for key in ("minimum_notional", "quantity_step", "price_tick", "minimum_quantity")
    }
    missing = [key for key in ("minimum_notional", "quantity_step", "price_tick") if facts[key] in (None, "")]
    if missing:
        raise ProofDriverError("instrument_constraints_missing", fields=missing)
    try:
        minimum_notional = Decimal(str(facts["minimum_notional"]))
        quantity_step = Decimal(str(facts["quantity_step"]))
        price_tick = Decimal(str(facts["price_tick"]))
        minimum_quantity = Decimal(str(facts["minimum_quantity"])) if facts["minimum_quantity"] not in (None, "") else quantity_step
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProofDriverError("instrument_constraints_invalid") from exc
    if min(minimum_notional, quantity_step, price_tick, minimum_quantity) <= 0:
        raise ProofDriverError("instrument_constraints_invalid")

    violations: list[dict[str, Any]] = []
    for index, rung in enumerate(rungs, start=1):
        price = Decimal(str(rung["price"]))
        quantity = Decimal(str(rung["quantity"]))
        notional = price * quantity
        reasons = []
        if price % price_tick != 0:
            reasons.append(f"price={price} not_on_tick={price_tick}")
        if quantity % quantity_step != 0 or quantity < minimum_quantity:
            reasons.append(f"quantity={quantity} step={quantity_step} minimum={minimum_quantity}")
        if notional < minimum_notional:
            reasons.append(f"notional={notional} minimum_notional={minimum_notional}")
        if reasons:
            violations.append({"rung": index, "price": str(price), "quantity": str(quantity), "notional": str(notional), "reasons": reasons})
    if violations:
        raise ProofDriverError(
            "instrument_constraints_blocked",
            violations=violations,
            suggestion="reduce grid count or increase the Portfolio Gate upper limit",
        )
    return {"name": "venue_constraints", "passed": True, "source": "instrument_catalog"}


def map_confirmation(
    output_root: Path,
    dashboard: Mapping[str, Any],
    *,
    approval_id: str,
    approved_by: str,
) -> dict[str, Any]:
    """Map Dashboard identity to the matching durable Park decision.

    The approval arguments are an explicit operator gate; they never turn a
    Dashboard boolean into authorization.  Authorization and receipt data are
    copied only from the durable Park ledger, which Coordinator verification
    checks again.
    """
    if not approval_id.strip() or approved_by.strip().lower() != "park":
        raise ProofDriverError("park_approval_required")
    digest = str(dashboard.get("plan_digest") or dashboard.get("preview_digest") or "")
    dashboard_path = output_root / DASHBOARD_CONFIRMATIONS
    if dashboard_path.exists() and dashboard.get("status") != "confirmed":
        raise ProofDriverError("dashboard_confirmation_not_confirmed")
    historical_acknowledged = (
        dashboard.get("acknowledged") is None
        and is_dashboard_confirm_and_run_record(dashboard)
        and dashboard.get("status") == "confirmed"
        and str(dashboard.get("operator_id") or "").strip().lower() == "park"
    )
    if dashboard_path.exists() and dashboard.get("acknowledged") is not True and not historical_acknowledged:
        raise ProofDriverError("dashboard_confirmation_not_acknowledged")
    if dashboard_path.exists() and str(dashboard.get("operator_id") or "").strip().lower() != "park":
        raise ProofDriverError("dashboard_operator_invalid")
    dashboard_confirmation = {
        "event": "confirmed", "execution_authorized": True, "execution_environment": "testnet",
        "plan_digest": digest, "confirmation_id": str(dashboard.get("confirmation_id") or ""),
        "proposal_id": digest, "receipt_digest": str(dashboard.get("confirmation_digest") or ""),
        "confirmed_at": dashboard.get("confirmed_at"), "operator_id": "park",
        "activation_id": dashboard.get("activation_id"), "confirmation_source": "dashboard",
    }
    if dashboard_path.exists():
        try:
            return parse_durable_confirmation(
                output_root, plan={"plan_digest": digest}, confirmation=dashboard_confirmation,
            )
        except DurableParkConfirmationError as exc:
            if exc.reason_code.startswith("dashboard_") or exc.reason_code == "activation_identity_mismatch":
                raise ProofDriverError(exc.reason_code) from exc
    rows = _rows(output_root / PARK_CONFIRMATIONS)
    decisions = [row for row in rows if row.get("event") in {"confirmed", "rejected"} and row.get("plan_digest") == digest]
    if not decisions or decisions[-1].get("event") != "confirmed":
        raise ProofDriverError("durable_park_confirmation_missing")
    decision = decisions[-1]
    proposals = [row for row in rows if row.get("event") == "proposal" and row.get("proposal_id") == decision.get("proposal_id")]
    if not proposals:
        raise ProofDriverError("durable_park_proposal_missing")
    return {
        "event": "confirmed", "execution_authorized": True, "execution_environment": "testnet",
        "plan_digest": digest, "confirmation_id": str(dashboard.get("confirmation_id") or decision.get("proposal_id")),
        "proposal_id": decision.get("proposal_id"), "receipt_digest": decision.get("receipt_digest"),
        "confirmed_at": decision.get("confirmed_at"), "operator_id": "park",
        "activation_id": dashboard.get("activation_id"),
    }


def _copy_evidence(source: Path, target: Path) -> None:
    for relative in (PREVIEWS, DASHBOARD_CONFIRMATIONS, PARK_CONFIRMATIONS, Path("testnet_automation/current.json")):
        origin = source / relative
        if origin.exists():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, destination)


def _reset_paper_capability_in_dry_run(work_root: Path) -> None:
    """Project a prior local-Paper capability back to the proof baseline.

    #1164 can leave the shared Paper output at ``paper_execution_ready``.
    Dry-run must still exercise activation and candidate selection, but only
    in its temporary copy; the online state is never rewritten.
    """
    current_path = work_root / "testnet_automation/current.json"
    try:
        rows = load_json(current_path)
    except Exception:  # noqa: BLE001 - let the normal proof gates report state errors.
        return
    if not isinstance(rows, list) or not rows or not isinstance(rows[-1], Mapping):
        return
    current = dict(rows[-1])
    if current.get("status") != "paper_execution_ready":
        return
    current.update(
        {
            "event": "activated",
            "action": "activate",
            "status": "activated",
            "execution_enabled": False,
            "execution_ready": False,
            "execution_blocker": "capability_gap:execution",
            "execution_profile": None,
            "next_action": "await_execution_capability",
        }
    )
    write_json(current_path, [current])


def _write_input(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, dict(value))


def run(
    *, output_root: Path, activation_id: str, approval_id: str, approved_by: str,
    secret_file: Path, account_address: str, runtime_id: str, release_sha: str,
    dry_run: bool, receipt: Path,
) -> dict[str, Any]:
    preview, dashboard = _latest_activation(output_root, activation_id)
    try:
        catalog_rows = tuple(public_catalog_loader(
            str(preview.get("venue_profile_id") or "hyperliquid.testnet")
        ))
    except Exception as exc:  # noqa: BLE001 - catalog is a fail-closed input.
        raise ProofDriverError("instrument_catalog_unavailable") from exc
    plan = build_plan(preview, dashboard, catalog_rows=catalog_rows)
    lifecycle_validation = validate_grid_plan(plan) if plan["strategy_type"] == "grid" else None
    confirmation = map_confirmation(output_root, dashboard, approval_id=approval_id, approved_by=approved_by)
    work_root = output_root
    with ExitStack() as stack:
        if dry_run:
            work_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="testnet-proof-driver-")))
            _copy_evidence(output_root, work_root)
            _reset_paper_capability_in_dry_run(work_root)
        plan_path = work_root / "driver-inputs/plan.json"
        confirmation_path = work_root / "driver-inputs/confirmation.json"
        market_path = work_root / "driver-inputs/market.json"
        _write_input(plan_path, plan)
        _write_input(confirmation_path, confirmation)
        args = argparse.Namespace(
            action="start", output_root=work_root, strategy_plan=plan_path, market=market_path,
            confirmation=confirmation_path, policy=None, strategy_family=plan["strategy_type"],
            instrument_id=plan["instrument_id"], account_address=account_address, runtime_id=runtime_id,
            release_sha=release_sha, standard_broker_release_sha=proof.STANDARD_BROKER_RELEASE_SHA,
            capability_revision=proof.PROTECTED_CAPABILITY_REVISION, approval_id=approval_id,
            approved_by=approved_by, secret_file=secret_file, credential_reference="file-secret://hyperliquid-testnet",
            execute_testnet=not dry_run, acknowledge=proof.ACKNOWLEDGEMENT if not dry_run else "",
        )
        # The market document is obtained from the bound Broker and, when its
        # public ticker is sparse, one coherent credential-free source read.
        broker = proof.build_broker_execution_port(proof._context(args, family=plan["strategy_type"]))
        try:
            market, market_checks = read_coherent_market(
                preview, broker, instrument_id=plan["instrument_id"]
            )
            _write_input(market_path, market)
        finally:
            close = getattr(broker, "close", None)
            if callable(close):
                close()
        if dry_run:
            # Run the real preflight/account/market/candidate path in the
            # temporary copy, replacing only the first exposure-changing call.
            # This preserves the Coordinator's candidate gate and makes the
            # stop point observable without submitting an order.
            safe_result = {"status": "dry_run_candidate_selected", "execution_mutation": False,
                           "network_operation_invoked": False, "next_action": "dry_run_stop_before_start"}
            with patch.object(TestnetAutomationCoordinator, "start_dca_session", return_value=safe_result), \
                 patch.object(TestnetAutomationCoordinator, "start_grid_session", return_value=safe_result):
                for proof_attempt in range(1, _MARKET_READ_ATTEMPTS + 1):
                    try:
                        result = proof._start(args)
                        break
                    except StrategyControlMachineError as exc:
                        result = {
                            "status": "BLOCKED",
                            "reason_code": exc.code,
                            "detail": dict(exc.evidence),
                            "execution_mutation": False,
                            "network_operation_invoked": False,
                            "next_action": "notify_park_and_wait",
                        }
                        break
                    except proof.TestnetAutomationProofError as exc:
                        if exc.reason_code != "market_price_mismatch" or proof_attempt == _MARKET_READ_ATTEMPTS:
                            raise ProofDriverError(exc.reason_code) from exc
                        time.sleep(1.0)
                        market, retry_checks = read_coherent_market(
                            preview, broker, instrument_id=plan["instrument_id"]
                        )
                        market_checks.extend(retry_checks)
                        _write_input(market_path, market)
        else:
            try:
                result = proof._start(args)
            except StrategyControlMachineError as exc:
                result = {
                    "status": "BLOCKED",
                    "reason_code": exc.code,
                    "detail": dict(exc.evidence),
                    "execution_mutation": False,
                    "network_operation_invoked": False,
                    "next_action": "notify_park_and_wait",
                }
            except proof.TestnetAutomationProofError as exc:
                details = dict(exc.result)
                details.setdefault(
                    "execution_blocker",
                    TestnetAutomationCoordinator(work_root).status().get("execution_blocker"),
                )
                raise ProofDriverError(exc.reason_code, **details) from exc
        output = {
            "schema_version": "testnet-proof-driver-receipt-v1", "status": result.get("status"),
            "dry_run": dry_run, "activation_id": activation_id, "plan_digest": plan["plan_digest"],
            "preview_digest": preview["preview_digest"], "confirmation_id": confirmation["confirmation_id"],
            "reason_code": result.get("reason_code"),
            "execution_blocker": result.get("execution_blocker"),
            "detail": result.get("detail"),
            "steps": {"preview_loaded": True, "plan_built": True,
                       "lifecycle_preflight": lifecycle_validation or {"status": "not_applicable"},
                       "market_bound": True,
                       "confirmation_mapped": True, "candidate_selected": result.get("status") in {"candidate_selected", "dry_run_candidate_selected"}},
            "market_self_check": market_checks,
            "result": {key: result.get(key) for key in ("status", "reason_code", "detail", "lifecycle_status", "execution_blocker", "execution_mutation", "network_operation_invoked", "next_action")},
            "secret_material_present": False,
        }
    receipt.parent.mkdir(parents=True, exist_ok=True)
    write_json(receipt, output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drive an attended Testnet proof from Dashboard evidence")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--activation-id")
    parser.add_argument("--approval-id")
    parser.add_argument("--approved-by")
    parser.add_argument("--secret-file", type=Path)
    parser.add_argument("--account-address")
    parser.add_argument("--runtime-id")
    parser.add_argument("--release-sha")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--validate-plan", action="store_true",
        help="Validate a supplied plan through all Grid lifecycle pre-write checks without broker or network I/O.",
    )
    parser.add_argument("--strategy-plan", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.validate_plan:
            if args.strategy_plan is None:
                raise ProofDriverError("strategy_plan_required")
            plan = _load_mapping(args.strategy_plan, "plan")
            result = validate_grid_plan(plan)
            print(json.dumps(result, sort_keys=True, ensure_ascii=False))
            return 0
        required = ("output_root", "activation_id", "approval_id", "approved_by", "secret_file", "account_address", "runtime_id", "release_sha", "receipt")
        missing = [name for name in required if getattr(args, name) in (None, "")]
        if missing:
            raise ProofDriverError("driver_argument_required", fields=missing)
        result = run(
            output_root=args.output_root,
            activation_id=args.activation_id,
            approval_id=args.approval_id,
            approved_by=args.approved_by,
            secret_file=args.secret_file,
            account_address=args.account_address,
            runtime_id=args.runtime_id,
            release_sha=args.release_sha,
            dry_run=args.dry_run,
            receipt=args.receipt,
        )
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except ProofDriverError as exc:
        blocked = {"schema_version": "testnet-proof-driver-receipt-v1", "status": "BLOCKED",
                   "reason_code": exc.reason_code, "activation_id": args.activation_id,
                   "dry_run": args.dry_run, "secret_material_present": False, **exc.details}
        if args.receipt is not None:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            write_json(args.receipt, blocked)
        print(json.dumps(blocked, sort_keys=True, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ProofDriverError",
    "build_market_document",
    "read_coherent_market",
    "build_plan",
    "validate_grid_plan",
    "map_confirmation",
    "run",
    "main",
]
