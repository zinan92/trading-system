"""Attended external DCA Testnet operator entrypoint.

The command is deliberately small at the boundary.  ``digest`` is the
default local-only action; ``project`` derives an external plan from the
canonical Paper DCA StrategyPlan without credentials or network; ``preflight``
starts the exact opt-in protection
runtime without resolving a signer or invoking a broker operation; ``start``,
``next-entry``, and ``flatten`` are the only exposure-changing actions and
require a durable Park confirmation plus an explicit operator acknowledgement.

This module owns no venue serialization and does not schedule or retry a
strategy.  It composes the public standard-broker canary binding with the
canonical trading-system DCA lifecycle and stops after one attended action.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from services.journal_store import load_json, write_json
from services.park_confirmation import ParkConfirmationLedger
from services.standard_broker_external_dca import (
    ExternalDcaError,
    ExternalDcaLifecycle,
    ExternalDcaPlan,
)
from services.standard_broker_dca_projection import (
    DcaProjectionError,
    project_canonical_dca_plan,
)


ACKNOWLEDGEMENT = "I_UNDERSTAND_ONE_ATTENDED_EXTERNAL_DCA_TESTNET_ACTION"
DEFAULT_CREDENTIAL_REFERENCE = "file-secret://hyperliquid-testnet"
DEFAULT_OUTPUT_ROOT = Path("outputs")
DEFAULT_SECRET_PLACEHOLDER = Path(".standard-broker-testnet-secret")
PROTECTION_CAPABILITY_REVISION = "hyperliquid-testnet-position-protection-runtime-v1"
PROTECTION_PROFILE_ID = "hyperliquid-testnet-position-protection"
PROTECTION_MATRIX_ID = "hyperliquid-testnet-position-protection-v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class ExternalDcaCliError(ValueError):
    """Redacted operator-facing blocker."""

    def __init__(self, reason_code: str, *, result: Mapping[str, Any] | None = None) -> None:
        self.reason_code = _reason_code(reason_code)
        self.result = dict(result or {})
        super().__init__(self.reason_code)


def _reason_code(value: object) -> str:
    text = str(value or "unknown_error").strip().lower()
    match = re.search(r"[a-z][a-z0-9_.-]*", text)
    return match.group(0) if match else "unknown_error"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _timestamp() -> str:
    return _now().isoformat()


def _account_fingerprint(account_address: str) -> str:
    return "sha256:" + hashlib.sha256(account_address.encode("utf-8")).hexdigest()


def _load_document(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ExternalDcaCliError("input_unavailable") from exc
    if not text.strip():
        raise ExternalDcaCliError("input_empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows: list[object] = []
        try:
            for line in text.splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ExternalDcaCliError("input_json_invalid") from exc
        if not rows:
            raise ExternalDcaCliError("input_json_invalid")
        return rows


def _load_plan_mapping(path: Path) -> dict[str, Any]:
    document = _load_document(path)
    if isinstance(document, Mapping) and isinstance(document.get("plan"), Mapping):
        document = document["plan"]
    if not isinstance(document, Mapping):
        raise ExternalDcaCliError("plan_must_be_object")
    return {str(key): value for key, value in document.items()}


def _load_binding_mapping(path: Path) -> dict[str, Any]:
    document = _load_document(path)
    if isinstance(document, Mapping) and isinstance(document.get("binding"), Mapping):
        document = document["binding"]
    if not isinstance(document, Mapping):
        raise ExternalDcaCliError("binding_must_be_object")
    return {str(key): value for key, value in document.items()}


def _require_plan(raw: Mapping[str, Any]) -> ExternalDcaPlan:
    try:
        return ExternalDcaPlan.from_mapping(raw, now=_now())
    except ExternalDcaError as exc:
        raise ExternalDcaCliError(_reason_code(exc)) from exc


def _require_account(plan: ExternalDcaPlan, account_address: str | None) -> str:
    value = str(account_address or "").strip()
    if not value:
        raise ExternalDcaCliError("account_address_required")
    if _account_fingerprint(value) != plan.account_fingerprint:
        raise ExternalDcaCliError("account_fingerprint_mismatch")
    return value


def _load_confirmation_mapping(path: Path, *, plan: ExternalDcaPlan) -> dict[str, Any]:
    document = _load_document(path)
    candidate: Mapping[str, Any] | None = None
    if isinstance(document, Mapping):
        nested = document.get("confirmation")
        if isinstance(nested, Mapping):
            candidate = nested
        elif document.get("event") == "confirmed":
            candidate = document
    elif isinstance(document, list):
        for row in reversed(document):
            if isinstance(row, Mapping) and row.get("event") == "confirmed":
                candidate = row
                break
    if candidate is None:
        raise ExternalDcaCliError("confirmation_projection_missing")
    confirmation = {str(key): value for key, value in candidate.items()}
    operator_id = str(
        confirmation.get("operator_id") or confirmation.get("park_user_id") or ""
    ).strip()
    if (
        confirmation.get("event") != "confirmed"
        or confirmation.get("execution_authorized") is not True
        or confirmation.get("execution_environment") != "testnet"
        or operator_id != "park"
        or confirmation.get("canary_id") != plan.plan_id
        or confirmation.get("plan_digest") != plan.plan_digest
        or not confirmation.get("receipt_digest")
        or not _SHA256.fullmatch(str(confirmation.get("receipt_digest") or "").lower())
    ):
        raise ExternalDcaCliError("confirmation_identity_invalid")
    try:
        expires_at = datetime.fromisoformat(str(confirmation["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ExternalDcaCliError("confirmation_expiry_invalid") from exc
    if expires_at.tzinfo is None:
        raise ExternalDcaCliError("confirmation_timezone_required")
    if expires_at.astimezone(timezone.utc) <= _now():
        raise ExternalDcaCliError("confirmation_expired")
    if not str(confirmation.get("proposal_id") or confirmation.get("confirmation_id") or "").strip():
        raise ExternalDcaCliError("confirmation_id_missing")
    return confirmation


def _verify_durable_confirmation(
    output_root: Path,
    *,
    plan: ExternalDcaPlan,
    confirmation: Mapping[str, Any],
) -> None:
    ledger = ParkConfirmationLedger(output_root, park_user_id="park")
    proposal_id = str(
        confirmation.get("proposal_id") or confirmation.get("confirmation_id") or ""
    ).strip()
    rows = ledger.rows()
    proposal = next(
        (
            row
            for row in rows
            if row.get("event") == "proposal"
            and str(row.get("proposal_id") or "") == proposal_id
        ),
        None,
    )
    decision = next(
        (
            row
            for row in reversed(rows)
            if row.get("event") == "confirmed"
            and str(row.get("proposal_id") or "") == proposal_id
        ),
        None,
    )
    if not proposal or not decision:
        raise ExternalDcaCliError("durable_confirmation_missing")
    try:
        proposal_expires = float(proposal.get("expires_at") or 0)
    except (TypeError, ValueError) as exc:
        raise ExternalDcaCliError("durable_confirmation_expiry_invalid") from exc
    if (
        proposal_expires <= _now().timestamp()
        or proposal.get("execution_environment") != "testnet"
        or decision.get("execution_environment") != "testnet"
        or proposal.get("plan_digest") != plan.plan_digest
        or decision.get("plan_digest") != plan.plan_digest
        or decision.get("execution_authorized") is not True
        or decision.get("park_user_id") != "park"
        or decision.get("receipt_digest") != confirmation.get("receipt_digest")
    ):
        raise ExternalDcaCliError("durable_confirmation_mismatch")


def _require_operator_args(args: argparse.Namespace, *, action: str) -> None:
    if action == "project" and not args.binding:
        raise ExternalDcaCliError("binding_required")
    if action in {"preflight", "start", "reconcile-entry", "next-entry", "flatten"}:
        if not str(args.account_address or "").strip():
            raise ExternalDcaCliError("account_address_required")
        if not str(args.approval_id or "").strip():
            raise ExternalDcaCliError("approval_id_required")
        if not str(args.approved_by or "").strip():
            raise ExternalDcaCliError("approved_by_required")
    if action in {"start", "reconcile-entry", "next-entry", "flatten"}:
        if not args.confirmation:
            raise ExternalDcaCliError("confirmation_required")
        if not args.secret_file:
            raise ExternalDcaCliError("secret_file_required")
        if args.execute_testnet is not True:
            raise ExternalDcaCliError("explicit_execute_flag_required")
        if args.acknowledge != ACKNOWLEDGEMENT:
            raise ExternalDcaCliError("exact_acknowledgement_required")
    elif args.execute_testnet:
        raise ExternalDcaCliError("execute_flag_requires_exposure_action")


def _build_external_protection(
    plan: ExternalDcaPlan,
    args: argparse.Namespace,
    *,
    canary: bool,
) -> tuple[object, object]:
    """Build only the reviewed trading-system composition seam."""

    try:
        from services.standard_broker_external_protection import (
            ExternalProtectionBuildConfig,
            build_external_position_protection_binding,
        )

        account_address = _require_account(plan, args.account_address)
        secret_file = Path(args.secret_file) if args.secret_file else DEFAULT_SECRET_PLACEHOLDER
        config = ExternalProtectionBuildConfig(
            account_address=account_address,
            runtime_id=plan.runtime_id,
            release_sha=plan.release_sha,
            approval_id=str(args.approval_id).strip(),
            approved_by=str(args.approved_by).strip(),
            secret_file=secret_file,
            credential_reference=str(args.credential_reference or DEFAULT_CREDENTIAL_REFERENCE).strip(),
            capability_revision=PROTECTION_CAPABILITY_REVISION,
        )
        return build_external_position_protection_binding(config, canary=canary)
    except ExternalDcaCliError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider details stay at the boundary.
        raise ExternalDcaCliError(_reason_code(getattr(exc, "reason_code", type(exc).__name__))) from exc


def _close_runtime(runtime: object | None) -> None:
    close = getattr(runtime, "close", None)
    if callable(close):
        close()


def _required_protection_capabilities(matrix: object) -> bool:
    supports = getattr(matrix, "supports", None)
    if not callable(supports):
        return False
    required = {
        "submit",
        "cancel",
        "replace",
        "query",
        "reduce_only",
        "mark_price_trigger",
        "grouped_tp_sl",
        "sibling_cancellation",
        "position_following",
        "position_level_tpsl",
        "take_profit_market",
        "stop_loss_market",
        "position_coverage",
        "cancel_replace",
    }
    return all(supports(name) is True for name in required)


def _preflight_action(plan: ExternalDcaPlan, args: argparse.Namespace) -> dict[str, Any]:
    runtime: object | None = None
    try:
        runtime, binding = _build_external_protection(plan, args, canary=False)
        session = getattr(binding, "runtime_session", None)
        matrix = getattr(binding, "protection_capabilities", None)
        if session is None or matrix is None:
            raise ExternalDcaCliError("protected_binding_identity_missing")
        if (
            getattr(getattr(session, "account", None), "address", None) != args.account_address
            or session.lifecycle_id != plan.runtime_id
            or session.capability_revision != plan.capability_revision
        ):
            raise ExternalDcaCliError("protected_binding_identity_mismatch")
        if getattr(matrix, "profile_id", None) != PROTECTION_MATRIX_ID:
            raise ExternalDcaCliError("protected_matrix_mismatch")
        if not _required_protection_capabilities(matrix):
            raise ExternalDcaCliError("protection_capability_gap")
        preflight = getattr(runtime, "preflight", None)
        if not callable(preflight):
            raise ExternalDcaCliError("runtime_preflight_missing")
        receipt = preflight(required_operations={"protection_order": {"submit", "query"}})
        if (
            getattr(receipt, "accepted", None) is not True
            or getattr(receipt, "external_network", None) is not True
            or getattr(receipt, "real_money_eligible", None) is not False
            or getattr(receipt, "account_address", None) != args.account_address
            or getattr(receipt, "lifecycle_id", None) != plan.runtime_id
            or getattr(receipt, "release_sha", None) != plan.release_sha
            or getattr(receipt, "capability_revision", None) != plan.capability_revision
        ):
            raise ExternalDcaCliError("protected_preflight_identity_mismatch")
        health = getattr(runtime, "health", None)
        if health is None or getattr(health, "external_network", None) is not True:
            raise ExternalDcaCliError("runtime_health_ambiguous")
        if getattr(health, "invocation_performed", None) is not False:
            raise ExternalDcaCliError("preflight_backend_invoked")
        return {
            "status": "PREFLIGHT_READY",
            "action": "preflight",
            "plan_id": plan.plan_id,
            "plan_digest": plan.plan_digest,
            "environment": plan.environment,
            "profile_id": PROTECTION_PROFILE_ID,
            "protection_matrix_id": PROTECTION_MATRIX_ID,
            "account_fingerprint": plan.account_fingerprint,
            "runtime_id": plan.runtime_id,
            "release_sha": plan.release_sha,
            "capability_revision": plan.capability_revision,
            "protection_ready": True,
            "external_network": True,
            "invocation_performed": False,
            "secret_resolved": False,
            "next_action": "supply_durable_park_confirmation_for_attended_start_or_flatten",
        }
    finally:
        _close_runtime(runtime)


def _build_lifecycle(
    output_root: Path,
    binding: object,
) -> tuple[ExternalDcaLifecycle, object]:
    try:
        from services.standard_broker_external_canary import StandardBrokerExternalCanaryAdapter

        protection = getattr(binding, "protection", None)
        if protection is None:
            raise ExternalDcaCliError("protected_binding_missing_protection")
        adapter = StandardBrokerExternalCanaryAdapter(binding)
        lifecycle = ExternalDcaLifecycle(
            output_root,
            adapter,
            adapter,
            protection,
            park_user_id="park",
        )
        return lifecycle, adapter
    except ExternalDcaCliError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExternalDcaCliError(_reason_code(type(exc).__name__)) from exc


def _state_result(
    *,
    action: str,
    plan: ExternalDcaPlan,
    state: Mapping[str, Any],
    lifecycle: ExternalDcaLifecycle,
    secret_resolved: bool = False,
) -> dict[str, Any]:
    return {
        "status": str(state.get("status") or "BLOCKED"),
        "action": action,
        "plan_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
        "profile_id": plan.profile_id,
        "network_invoked": True,
        "secret_resolved": secret_resolved,
        "next_action": state.get("next_action", "notify_park_and_wait"),
        "state_path": str(lifecycle.current_path),
        **({"blocker": state["blocker"]} if state.get("blocker") else {}),
    }


def _exposure_operation_observed(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    action: str,
) -> bool:
    """Conservatively report signer use only when this call adds evidence."""

    if action == "start":
        return not before and bool(after.get("entry_order_id"))
    if action == "next-entry":
        return bool(after.get("entry_order_id")) and after.get("entry_order_id") != before.get("entry_order_id")
    if action == "flatten":
        return len(after.get("receipts") or ()) > len(before.get("receipts") or ())
    return False


def _start_action(plan: ExternalDcaPlan, args: argparse.Namespace, confirmation: Mapping[str, Any]) -> dict[str, Any]:
    output_root = Path(args.output_root)
    runtime: object | None = None
    lifecycle: ExternalDcaLifecycle | None = None
    before: Mapping[str, Any] = {}
    try:
        runtime, binding = _build_external_protection(plan, args, canary=True)
        lifecycle, adapter = _build_lifecycle(output_root, binding)
        before = lifecycle.snapshot()
        state = lifecycle.prepare(plan, confirmation=confirmation, timestamp=_timestamp())
        if state.get("status") == "ENTRY_FILLED_PENDING_FACTS":
            order_id = str(state.get("entry_order_id") or "")
            bundle = adapter.read_facts(
                order_id=order_id,
                instrument_id=plan.instrument_id,
                now=_now(),
            )
            state = lifecycle.on_entry_facts(
                plan,
                bundle=bundle,
                confirmation=confirmation,
                timestamp=_timestamp(),
            )
        return _state_result(
            action="start",
            plan=plan,
            state=state,
            lifecycle=lifecycle,
            secret_resolved=_exposure_operation_observed(before, state, action="start"),
        )
    except ExternalDcaError as exc:
        if lifecycle is not None:
            state = lifecycle.snapshot()
            if state:
                return _state_result(
                    action="start",
                    plan=plan,
                    state=state,
                    lifecycle=lifecycle,
                    secret_resolved=_exposure_operation_observed(before, state, action="start"),
                )
        raise ExternalDcaCliError(_reason_code(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - provider details stay redacted.
        if lifecycle is not None:
            state = lifecycle.snapshot()
            if state:
                return _state_result(
                    action="start",
                    plan=plan,
                    state=state,
                    lifecycle=lifecycle,
                    secret_resolved=_exposure_operation_observed(before, state, action="start"),
                )
        raise ExternalDcaCliError(_reason_code(type(exc).__name__)) from exc
    finally:
        _close_runtime(runtime)


def _flatten_action(plan: ExternalDcaPlan, args: argparse.Namespace, confirmation: Mapping[str, Any]) -> dict[str, Any]:
    output_root = Path(args.output_root)
    runtime: object | None = None
    lifecycle: ExternalDcaLifecycle | None = None
    before: Mapping[str, Any] = {}
    try:
        runtime, binding = _build_external_protection(plan, args, canary=True)
        lifecycle, _adapter = _build_lifecycle(output_root, binding)
        before = lifecycle.snapshot()
        state = lifecycle.flatten(
            plan,
            confirmation=confirmation,
            timestamp=_timestamp(),
        )
        return _state_result(
            action="flatten",
            plan=plan,
            state=state,
            lifecycle=lifecycle,
            secret_resolved=_exposure_operation_observed(before, state, action="flatten"),
        )
    except ExternalDcaError as exc:
        if lifecycle is not None:
            state = lifecycle.snapshot()
            if state:
                return _state_result(
                    action="flatten",
                    plan=plan,
                    state=state,
                    lifecycle=lifecycle,
                    secret_resolved=_exposure_operation_observed(before, state, action="flatten"),
                )
        raise ExternalDcaCliError(_reason_code(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - provider details stay redacted.
        if lifecycle is not None:
            state = lifecycle.snapshot()
            if state:
                return _state_result(
                    action="flatten",
                    plan=plan,
                    state=state,
                    lifecycle=lifecycle,
                    secret_resolved=_exposure_operation_observed(before, state, action="flatten"),
                )
        raise ExternalDcaCliError(_reason_code(type(exc).__name__)) from exc
    finally:
        _close_runtime(runtime)


def _next_entry_action(
    plan: ExternalDcaPlan,
    args: argparse.Namespace,
    confirmation: Mapping[str, Any],
) -> dict[str, Any]:
    """Submit exactly one attended next DCA level, then stop."""

    output_root = Path(args.output_root)
    runtime: object | None = None
    lifecycle: ExternalDcaLifecycle | None = None
    before: Mapping[str, Any] = {}
    try:
        runtime, binding = _build_external_protection(plan, args, canary=True)
        lifecycle, adapter = _build_lifecycle(output_root, binding)
        before = lifecycle.snapshot()
        state = lifecycle.submit_next_entry(
            plan,
            confirmation=confirmation,
            timestamp=_timestamp(),
        )
        if state.get("status") == "ENTRY_FILLED_PENDING_FACTS":
            order_id = str(state.get("entry_order_id") or "")
            bundle = adapter.read_facts(
                order_id=order_id,
                instrument_id=plan.instrument_id,
                now=_now(),
            )
            state = lifecycle.on_entry_facts(
                plan,
                bundle=bundle,
                confirmation=confirmation,
                timestamp=_timestamp(),
            )
        return _state_result(
            action="next-entry",
            plan=plan,
            state=state,
            lifecycle=lifecycle,
            secret_resolved=_exposure_operation_observed(before, state, action="next-entry"),
        )
    except ExternalDcaError as exc:
        if lifecycle is not None:
            state = lifecycle.snapshot()
            if state:
                return _state_result(
                    action="next-entry",
                    plan=plan,
                    state=state,
                    lifecycle=lifecycle,
                    secret_resolved=_exposure_operation_observed(before, state, action="next-entry"),
                )
        raise ExternalDcaCliError(_reason_code(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - provider details stay redacted.
        if lifecycle is not None:
            state = lifecycle.snapshot()
            if state:
                return _state_result(
                    action="next-entry",
                    plan=plan,
                    state=state,
                    lifecycle=lifecycle,
                    secret_resolved=_exposure_operation_observed(before, state, action="next-entry"),
                )
        raise ExternalDcaCliError(_reason_code(type(exc).__name__)) from exc
    finally:
        _close_runtime(runtime)


def _reconcile_entry_action(
    plan: ExternalDcaPlan,
    args: argparse.Namespace,
    confirmation: Mapping[str, Any],
) -> dict[str, Any]:
    """Query one current entry and process its facts without submitting."""

    output_root = Path(args.output_root)
    runtime: object | None = None
    lifecycle: ExternalDcaLifecycle | None = None
    try:
        runtime, binding = _build_external_protection(plan, args, canary=True)
        lifecycle, _adapter = _build_lifecycle(output_root, binding)
        state = lifecycle.reconcile_entry(
            plan,
            confirmation=confirmation,
            timestamp=_timestamp(),
        )
        return _state_result(
            action="reconcile-entry",
            plan=plan,
            state=state,
            lifecycle=lifecycle,
        )
    except ExternalDcaError as exc:
        if lifecycle is not None:
            state = lifecycle.snapshot()
            if state:
                return _state_result(
                    action="reconcile-entry",
                    plan=plan,
                    state=state,
                    lifecycle=lifecycle,
                )
        raise ExternalDcaCliError(_reason_code(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - provider details stay redacted.
        if lifecycle is not None:
            state = lifecycle.snapshot()
            if state:
                return _state_result(
                    action="reconcile-entry",
                    plan=plan,
                    state=state,
                    lifecycle=lifecycle,
                )
        raise ExternalDcaCliError(_reason_code(type(exc).__name__)) from exc
    finally:
        _close_runtime(runtime)


def _write_cli_blocker(output_root: Path, plan: ExternalDcaPlan | None, reason_code: str) -> dict[str, Any]:
    path = output_root / "standard_broker_external_dca" / "cli-blockers.json"
    rows = load_json(path)
    row = {
        "schema_version": "standard-broker-external-dca-cli-v1",
        "status": "BLOCKED",
        "reason_code": _reason_code(reason_code),
        "plan_id": plan.plan_id if plan else "unknown",
        "plan_digest": plan.plan_digest if plan else "unknown",
        "network_invoked": False,
        "secret_resolved": False,
        "next_action": "notify_park_and_wait",
        "recorded_at": _timestamp(),
    }
    rows.append(row)
    write_json(path, rows)
    return row


def _digest_action(raw: Mapping[str, Any]) -> dict[str, Any]:
    plan = _require_plan(raw)
    return {
        "status": "DIGEST_ONLY",
        "action": "digest",
        "plan_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
        "profile_id": plan.profile_id,
        "network_invoked": False,
        "secret_resolved": False,
        "next_action": "create_or_load_the_same_digest_in_durable_park_confirmation",
    }


def _project_action(raw: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    try:
        projection = project_canonical_dca_plan(raw, binding=binding)
    except DcaProjectionError as exc:
        raise ExternalDcaCliError(_reason_code(exc)) from exc
    source = projection.execution_market_source
    return {
        "status": "PROJECTED",
        "action": "project",
        "source_strategy_plan_id": projection.source_strategy_plan_id,
        "source_strategy_plan_digest": projection.source_strategy_plan_digest,
        "canonical_semantics": projection.canonical_semantics,
        "source_market": projection.source_market,
        "execution_market_source": {
            "source_id": source.source_id,
            "broker_id": source.broker_id,
            "environment": source.environment,
            "instrument_id": source.instrument_id,
            "execution_venue": source.execution_venue,
        },
        "projected_plan": projection.plan,
        "network_invoked": False,
        "secret_resolved": False,
        "next_action": "review_projection_and_create_confirmation",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Attended external DCA Testnet action; default is local digest only."
    )
    parser.add_argument(
        "--action",
        choices=("digest", "project", "preflight", "start", "reconcile-entry", "next-entry", "flatten"),
        default="digest",
    )
    parser.add_argument("--plan", type=Path, required=True, help="JSON external DCA plan; no credentials")
    parser.add_argument("--binding", type=Path, help="JSON non-secret Broker binding for project")
    parser.add_argument("--confirmation", type=Path, help="confirmed Park projection JSON/JSONL")
    parser.add_argument("--account-address", help="Hyperliquid Testnet account address")
    parser.add_argument("--secret-file", type=Path, help="local protected signer file (start/reconcile-entry/next-entry/flatten only)")
    parser.add_argument("--credential-reference", default=DEFAULT_CREDENTIAL_REFERENCE)
    parser.add_argument("--approval-id", help="human Testnet approval identifier")
    parser.add_argument("--approved-by", help="human approver identity")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--execute-testnet", action="store_true")
    parser.add_argument("--acknowledge")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    plan: ExternalDcaPlan | None = None
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(argv)
        _require_operator_args(args, action=args.action)
        raw = _load_plan_mapping(args.plan)
        if args.action == "digest":
            result = _digest_action(raw)
        elif args.action == "project":
            result = _project_action(raw, _load_binding_mapping(Path(args.binding)))
        else:
            plan = _require_plan(raw)
            if args.action in {"start", "reconcile-entry", "next-entry", "flatten"}:
                _require_account(plan, args.account_address)
                confirmation = _load_confirmation_mapping(Path(args.confirmation), plan=plan)
                _verify_durable_confirmation(Path(args.output_root), plan=plan, confirmation=confirmation)
                if args.action == "start":
                    result = _start_action(plan, args, confirmation)
                elif args.action == "reconcile-entry":
                    result = _reconcile_entry_action(plan, args, confirmation)
                elif args.action == "next-entry":
                    result = _next_entry_action(plan, args, confirmation)
                else:
                    result = _flatten_action(plan, args, confirmation)
            else:
                _require_account(plan, args.account_address)
                result = _preflight_action(plan, args)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except ExternalDcaCliError as exc:
        result = dict(exc.result)
        if not result:
            result = _write_cli_blocker(Path(args.output_root) if args is not None else DEFAULT_OUTPUT_ROOT, plan, exc.reason_code)
        result.setdefault("status", "BLOCKED")
        result.setdefault("reason_code", exc.reason_code)
        result.setdefault("next_action", "notify_park_and_wait")
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 2
    except (OSError, TypeError, ValueError) as exc:
        result = {
            "status": "BLOCKED",
            "reason_code": _reason_code(type(exc).__name__),
            "network_invoked": False,
            "secret_resolved": False,
            "next_action": "notify_park_and_wait",
        }
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 2
    except Exception as exc:  # noqa: BLE001 - no provider/path detail may cross the CLI.
        result = {
            "status": "BLOCKED",
            "reason_code": _reason_code(type(exc).__name__),
            "network_invoked": False,
            "secret_resolved": False,
            "next_action": "notify_park_and_wait",
        }
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
