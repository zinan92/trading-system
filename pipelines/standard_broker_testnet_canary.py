"""Attended Hyperliquid Testnet canary operator entrypoint.

The command is deliberately boring at the boundary:

* ``digest`` is the default and is completely local/read-only;
* ``preflight`` binds a release-bound Testnet runtime but does not invoke the
  external backend (and therefore does not resolve a signer secret);
* ``run`` is the only action that may invoke the public standard-broker
  binding, and it requires both an explicit flag and an exact acknowledgement.

The external adapter and all provider-native payload mapping remain inside
``standard-broker``.  This module only owns operator input, durable Park
confirmation loading, and the already-reviewed trading-system canary state
machine.
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
from services.standard_broker_testnet_canary import (
    TestnetCanary,
    TestnetCanaryError,
    TestnetCanaryPlan,
    canary_plan_digest,
)
from services.standard_broker_testnet_canary_facts import TestnetCanaryFillFlat


ACKNOWLEDGEMENT = "I_UNDERSTAND_ONE_ATTENDED_HYPERLIQUID_TESTNET_ORDER"
DEFAULT_CREDENTIAL_REFERENCE = "file-secret://hyperliquid-testnet"
DEFAULT_OUTPUT_ROOT = Path("outputs")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class CanaryCliError(ValueError):
    """Operator-input or runtime blocker safe to summarize without details."""

    def __init__(self, reason_code: str, *, result: Mapping[str, Any] | None = None) -> None:
        self.reason_code = _reason_code(reason_code)
        self.result = dict(result or {})
        super().__init__(self.reason_code)


def _reason_code(value: object) -> str:
    """Keep provider/path/error text out of the operator output."""

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
    """Load one JSON object/list, with JSONL support for Park journals."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CanaryCliError("input_unavailable") from exc
    if not text.strip():
        raise CanaryCliError("input_empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows: list[object] = []
        try:
            for line in text.splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise CanaryCliError("input_json_invalid") from exc
        if not rows:
            raise CanaryCliError("input_json_invalid")
        return rows


def _load_plan_mapping(path: Path) -> dict[str, Any]:
    document = _load_document(path)
    if isinstance(document, Mapping) and isinstance(document.get("plan"), Mapping):
        document = document["plan"]
    if not isinstance(document, Mapping):
        raise CanaryCliError("plan_must_be_object")
    return {str(key): value for key, value in document.items()}


def _load_confirmation_mapping(path: Path, *, plan: TestnetCanaryPlan) -> dict[str, Any]:
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
        raise CanaryCliError("confirmation_projection_missing")
    confirmation = {str(key): value for key, value in candidate.items()}
    if confirmation.get("event") != "confirmed":
        raise CanaryCliError("confirmation_not_confirmed")
    if confirmation.get("execution_authorized") is not True:
        raise CanaryCliError("confirmation_not_authorized")
    if confirmation.get("execution_environment") != "testnet":
        raise CanaryCliError("confirmation_environment_mismatch")
    operator_id = str(
        confirmation.get("operator_id")
        or confirmation.get("park_user_id")
        or ""
    ).strip()
    if operator_id != "park":
        raise CanaryCliError("confirmation_operator_mismatch")
    if confirmation.get("canary_id") != plan.canary_id:
        raise CanaryCliError("confirmation_canary_mismatch")
    if confirmation.get("plan_digest") != plan.plan_digest:
        raise CanaryCliError("confirmation_digest_mismatch")
    if not confirmation.get("proposal_id") and not confirmation.get("confirmation_id"):
        raise CanaryCliError("confirmation_id_missing")
    if not confirmation.get("receipt_digest"):
        raise CanaryCliError("confirmation_receipt_missing")
    if not _SHA256.fullmatch(str(confirmation["receipt_digest"]).lower()):
        raise CanaryCliError("confirmation_receipt_invalid")
    try:
        expires_at = datetime.fromisoformat(str(confirmation["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise CanaryCliError("confirmation_expiry_invalid") from exc
    if expires_at.tzinfo is None or expires_at.astimezone(timezone.utc) <= _now():
        raise CanaryCliError("confirmation_expired")
    return confirmation


def _verify_durable_confirmation(
    output_root: Path,
    *,
    plan: TestnetCanaryPlan,
    confirmation: Mapping[str, Any],
) -> None:
    """Verify Park's durable ledger before constructing any network-capable binding."""

    ledger = ParkConfirmationLedger(output_root, park_user_id="park")
    rows = ledger.rows()
    proposal_id = str(
        confirmation.get("proposal_id")
        or confirmation.get("confirmation_id")
        or ""
    ).strip()
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
        raise CanaryCliError("durable_confirmation_missing")
    try:
        proposal_expires = float(proposal.get("expires_at") or 0)
    except (TypeError, ValueError) as exc:
        raise CanaryCliError("durable_confirmation_expiry_invalid") from exc
    if proposal_expires <= _now().timestamp():
        raise CanaryCliError("durable_confirmation_expired")
    if (
        proposal.get("execution_environment") != "testnet"
        or decision.get("execution_environment") != "testnet"
        or proposal.get("plan_digest") != plan.plan_digest
        or decision.get("plan_digest") != plan.plan_digest
        or decision.get("execution_authorized") is not True
        or decision.get("receipt_digest") != confirmation.get("receipt_digest")
        or decision.get("park_user_id") != "park"
        or str(confirmation.get("operator_id") or confirmation.get("park_user_id") or "").strip() != "park"
    ):
        raise CanaryCliError("durable_confirmation_mismatch")


def _require_plan(raw: Mapping[str, Any]) -> TestnetCanaryPlan:
    try:
        return TestnetCanaryPlan.from_mapping(raw, now=_now())
    except TestnetCanaryError as exc:
        raise CanaryCliError(_reason_code(exc)) from exc


def _require_account(plan: TestnetCanaryPlan, account_address: str | None) -> str:
    value = str(account_address or "").strip()
    if not value:
        raise CanaryCliError("account_address_required")
    if _account_fingerprint(value) != plan.account_fingerprint:
        raise CanaryCliError("account_fingerprint_mismatch")
    return value


def _require_operator_args(args: argparse.Namespace, *, action: str) -> None:
    if action in {"preflight", "run"}:
        if not str(args.account_address or "").strip():
            raise CanaryCliError("account_address_required")
        if not str(args.approval_id or "").strip():
            raise CanaryCliError("approval_id_required")
        if not str(args.approved_by or "").strip():
            raise CanaryCliError("approved_by_required")
    if action == "run":
        if not args.confirmation:
            raise CanaryCliError("confirmation_required")
        if not args.secret_file:
            raise CanaryCliError("secret_file_required")
        if args.execute_testnet is not True:
            raise CanaryCliError("explicit_execute_flag_required")
        if args.acknowledge != ACKNOWLEDGEMENT:
            raise CanaryCliError("exact_acknowledgement_required")
    elif args.execute_testnet:
        raise CanaryCliError("execute_flag_requires_run")


def _build_runtime(
    plan: TestnetCanaryPlan,
    args: argparse.Namespace,
) -> tuple[object, object, object]:
    """Build/start the exact Testnet runtime without invoking an operation."""

    try:
        from standard_broker import (
            AccountReference,
            AccountScope,
            BrokerEnvironment,
            BrokerRuntimeSession,
            ExternalBrokerBuildContext,
            ExternalEnvironmentApproval,
            ExternalRuntimeIdentity,
            RuntimeActivationPolicy,
            RuntimeFactLedger,
            SignerKind,
            SignerReference,
        )
        from standard_broker.adapters.hyperliquid import (
            HyperliquidTestnetBackendConfig,
            LocalFileSecretProvider,
            NautilusHyperliquidRuntime,
            NautilusHyperliquidTestnetBackend,
            NautilusRuntimeConfig,
            default_testnet_capabilities,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise CanaryCliError("standard_broker_dependency_unavailable") from exc

    account_address = _require_account(plan, args.account_address)
    credential_reference = str(args.credential_reference or DEFAULT_CREDENTIAL_REFERENCE).strip()
    secret_file = Path(args.secret_file) if args.secret_file else Path(".standard-broker-testnet-secret")
    capabilities = default_testnet_capabilities(plan.capability_revision)
    signer = SignerReference(SignerKind.API_AGENT, "local-file", credential_reference)
    session = BrokerRuntimeSession(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        account=AccountReference(AccountScope.MASTER, account_address),
        signer=signer,
        signer_provider=LocalFileSecretProvider({credential_reference: secret_file}),
        capabilities=capabilities,
        execution_scope="hypercore:default",
        lifecycle_id=plan.runtime_id,
    )
    approval = ExternalEnvironmentApproval(
        environment=BrokerEnvironment.TESTNET,
        approval_id=str(args.approval_id).strip(),
        release_sha=plan.release_sha,
        approved_by=str(args.approved_by).strip(),
        approved_at=_now(),
        account_address=account_address,
        lifecycle_id=plan.runtime_id,
    )
    backend_config = HyperliquidTestnetBackendConfig(
        account_address=account_address,
        capability_revision=plan.capability_revision,
        capabilities=capabilities,
    )
    backend = NautilusHyperliquidTestnetBackend(
        session=session,
        config=backend_config,
        secrets=session.signer_provider,  # type: ignore[arg-type]
    )
    runtime = NautilusHyperliquidRuntime(
        session=session,
        backend=backend,
        config=NautilusRuntimeConfig(
            expected_version=backend_config.expected_version,
            expected_commit=backend_config.expected_commit,
            policy=RuntimeActivationPolicy(testnet_approval=approval),
            expected_release_sha=plan.release_sha,
        ),
    )
    try:
        runtime.start()
    except Exception as exc:  # noqa: BLE001 - CLI emits only a redacted reason code.
        raise CanaryCliError(_reason_code(getattr(exc, "reason_code", type(exc).__name__))) from exc
    runtime_identity = ExternalRuntimeIdentity(
        adapter_id=backend.metadata.package,
        version=backend.metadata.version,
        commit=backend.metadata.commit,
        mapping_revision=plan.capability_revision,
        transport_state="external_testnet",
    )
    context = ExternalBrokerBuildContext(
        session=session,
        runtime_identity=runtime_identity,
        release_sha=plan.release_sha,
        approval=approval,
    )
    ledger = RuntimeFactLedger()
    return runtime, context, ledger


def _build_external_port(plan: TestnetCanaryPlan, args: argparse.Namespace) -> tuple[object, object]:
    """Build the production public binding; the only operation-capable path."""

    runtime: object | None = None
    try:
        from standard_broker import build_hyperliquid_testnet_canary_binding_from_runtime
        from services.standard_broker_external_canary import StandardBrokerExternalCanaryAdapter
        runtime, context, ledger = _build_runtime(plan, args)
        binding = build_hyperliquid_testnet_canary_binding_from_runtime(
            context=context,
            runtime=runtime,
            ledger=ledger,
        )
        return runtime, StandardBrokerExternalCanaryAdapter(binding)
    except CanaryCliError:
        if runtime is not None:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()
        raise
    except Exception as exc:  # noqa: BLE001 - no native detail crosses CLI.
        if runtime is not None:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()
        raise CanaryCliError(_reason_code(getattr(exc, "reason_code", type(exc).__name__))) from exc


def _runtime_preflight_result(runtime: object, plan: TestnetCanaryPlan) -> dict[str, Any]:
    health = getattr(runtime, "health", None)
    if health is None:
        raise CanaryCliError("runtime_health_missing")
    external_network = getattr(health, "external_network", None)
    invocation_performed = getattr(health, "invocation_performed", None)
    if external_network is not True or invocation_performed is not False:
        raise CanaryCliError("runtime_health_ambiguous")
    return {
        "status": "PREFLIGHT_READY",
        "canary_id": plan.canary_id,
        "plan_digest": plan.plan_digest,
        "environment": plan.environment,
        "profile_id": plan.profile_id,
        "runtime_id": plan.runtime_id,
        "release_sha": plan.release_sha,
        "capability_revision": plan.capability_revision,
        "external_network": external_network,
        "invocation_performed": invocation_performed,
        "secret_resolved": False,
        "next_action": "run_requires_attended_confirmation_and_explicit_acknowledgement",
    }


def _write_cli_blocker(output_root: Path, plan: TestnetCanaryPlan | None, reason_code: str) -> dict[str, Any]:
    path = output_root / "standard_broker_testnet_canary" / "cli-blockers.json"
    rows = load_json(path)
    row = {
        "schema_version": "standard-broker-testnet-canary-cli-v1",
        "status": "BLOCKED",
        "reason_code": _reason_code(reason_code),
        "canary_id": plan.canary_id if plan else "unknown",
        "plan_digest": plan.plan_digest if plan else "unknown",
        "next_action": "notify_park_and_wait",
        "recorded_at": _timestamp(),
    }
    rows.append(row)
    write_json(path, rows)
    return row


def _digest_action(raw: Mapping[str, Any]) -> dict[str, Any]:
    digest = canary_plan_digest(raw)
    supplied = raw.get("plan_digest")
    if supplied is not None and (
        not _SHA256.fullmatch(str(supplied).lower())
        or str(supplied) != digest
    ):
        raise CanaryCliError(
            "plan_digest_mismatch",
            result={
                "status": "BLOCKED",
                "canary_id": str(raw.get("canary_id") or "unknown"),
                "expected_plan_digest": digest,
                "supplied_digest_present": True,
                "network_invoked": False,
                "secret_resolved": False,
                "next_action": "repair_the_plan_before_park_confirmation",
            },
        )
    return {
        "status": "DIGEST_ONLY",
        "canary_id": str(raw.get("canary_id") or "unknown"),
        "plan_digest": digest,
        "supplied_digest_matches": supplied is None or str(supplied) == digest,
        "network_invoked": False,
        "secret_resolved": False,
        "next_action": "supply_the_same_digest_to_the_durable_park_confirmation",
    }


def _preflight_action(raw: Mapping[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    plan = _require_plan(raw)
    _require_account(plan, args.account_address)
    runtime, _context, _ledger = _build_runtime(plan, args)
    try:
        return _runtime_preflight_result(runtime, plan)
    finally:
        close = getattr(runtime, "close", None)
        if callable(close):
            close()


def _run_action(raw: Mapping[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    plan = _require_plan(raw)
    output_root = Path(args.output_root)
    try:
        confirmation = _load_confirmation_mapping(Path(args.confirmation), plan=plan)
        _verify_durable_confirmation(output_root, plan=plan, confirmation=confirmation)
    except CanaryCliError as exc:
        row = _write_cli_blocker(output_root, plan, exc.reason_code)
        raise CanaryCliError(row["reason_code"], result=row) from exc
    runtime: object | None = None
    canary: TestnetCanary | None = None
    try:
        runtime, broker = _build_external_port(plan, args)
        canary = TestnetCanary(
            output_root,
            broker,
            park_user_id="park",
            confirmation_ledger=ParkConfirmationLedger(output_root, park_user_id="park"),
            approved_market_sources={"nautilus-hyperliquid.testnet"},
        )
        prepared = canary.prepare(plan, confirmation=confirmation, timestamp=_timestamp())
        submitted = canary.submit_entry(plan, confirmation=confirmation, timestamp=_timestamp())
        status = str(submitted.get("status") or "")
        if status == "ENTRY_FILLED":
            final = TestnetCanaryFillFlat(canary, broker).complete(
                plan,
                confirmation=confirmation,
                timestamp=_timestamp(),
            )
        elif status == "ENTRY_RECONCILED":
            # A resting limit is not a fill-to-flat success.  Cancel it through
            # the reviewed ordinary CANARY-01 path so no unattended order is
            # left behind.
            final = canary.cancel_entry(
                plan,
                confirmation=confirmation,
                timestamp=_timestamp(),
            )
        else:
            final = submitted
        return {
            "status": str(final.get("status") or "BLOCKED"),
            "canary_id": plan.canary_id,
            "plan_digest": plan.plan_digest,
            "network_invoked": True,
            "secret_resolved": True,
            "next_action": final.get("next_action"),
            "state_path": str(canary.current_path),
        }
    except (CanaryCliError, TestnetCanaryError) as exc:
        state = canary.snapshot(plan) if canary is not None else None
        if state and state.get("status") == "BLOCKED":
            row = {
                "status": "BLOCKED",
                "canary_id": plan.canary_id,
                "plan_digest": plan.plan_digest,
                "reason_code": _reason_code(state.get("blocker")),
                "next_action": state.get("next_action", "notify_park_and_wait"),
                "state_path": str(canary.current_path),
            }
        else:
            row = _write_cli_blocker(output_root, plan, getattr(exc, "reason_code", type(exc).__name__))
        raise CanaryCliError(row["reason_code"], result=row) from exc
    finally:
        if runtime is not None:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Attended Hyperliquid Testnet canary; default action is local digest only."
    )
    parser.add_argument("--action", choices=("digest", "preflight", "run"), default="digest")
    parser.add_argument("--plan", type=Path, required=True, help="JSON canary plan; no credentials")
    parser.add_argument("--confirmation", type=Path, help="confirmed Park projection JSON/JSONL (run only)")
    parser.add_argument("--account-address", help="Hyperliquid Testnet account address")
    parser.add_argument("--secret-file", type=Path, help="protected local signer file (run only)")
    parser.add_argument("--credential-reference", default=DEFAULT_CREDENTIAL_REFERENCE)
    parser.add_argument("--approval-id", help="human Testnet approval identifier")
    parser.add_argument("--approved-by", help="human approver identity")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--execute-testnet", action="store_true")
    parser.add_argument("--acknowledge")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        _require_operator_args(args, action=args.action)
        raw = _load_plan_mapping(args.plan)
        if args.action == "digest":
            result = _digest_action(raw)
        elif args.action == "preflight":
            result = _preflight_action(raw, args)
        else:
            result = _run_action(raw, args)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except CanaryCliError as exc:
        result = dict(exc.result)
        result.setdefault("status", "BLOCKED")
        result.setdefault("reason_code", exc.reason_code)
        result.setdefault("next_action", "notify_park_and_wait")
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 2
    except (OSError, ValueError, TypeError) as exc:
        result = {
            "status": "BLOCKED",
            "reason_code": _reason_code(type(exc).__name__),
            "next_action": "notify_park_and_wait",
        }
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
