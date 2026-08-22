#!/usr/bin/env python3
"""Run the human-gated Hyperliquid Testnet preflight or one lifecycle proof.

The script never prints credential material.  It is deliberately Testnet-only;
there is no Mainnet endpoint option.
"""

from argparse import ArgumentParser
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import subprocess

from standard_broker.adapters.hyperliquid import (
    HyperliquidTestnetBackendConfig,
    LocalFileSecretProvider,
    NautilusHyperliquidTestnetBackend,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
    TestnetProofPlan,
    default_testnet_capabilities,
    run_testnet_lifecycle,
)
from standard_broker.models import AccountScope, BrokerEnvironment, SignerKind
from standard_broker.runtime import (
    AccountReference,
    BrokerRuntimeSession,
    ExternalEnvironmentApproval,
    RuntimeActivationPolicy,
    SignerReference,
)


def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--secret-file", type=Path, required=True)
    parser.add_argument("--account-address", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--approved-by", default="park")
    parser.add_argument("--execute-testnet", action="store_true")
    parser.add_argument("--auto-prices", action="store_true")
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--instrument", default="HYPE-USD-PERP")
    parser.add_argument("--quantity", type=Decimal)
    parser.add_argument("--resting-price", type=Decimal)
    parser.add_argument("--aggressive-price", type=Decimal)
    parser.add_argument("--fill-price", type=Decimal)
    parser.add_argument("--close-price", type=Decimal)
    parser.add_argument("--max-slippage-bps", type=Decimal, default=Decimal("100"))
    parser.add_argument("--max-quote-age-seconds", type=float, default=5.0)
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    secret_path = args.secret_file.expanduser().resolve()
    if repo_root == secret_path or repo_root in secret_path.parents:
        parser.error("--secret-file must be outside the standard-broker repository")
    current_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if args.release_sha != current_sha:
        parser.error("--release-sha must equal the checked-out standard-broker commit")
    if args.execute_testnet and args.evidence_output is None:
        parser.error("--execute-testnet requires --evidence-output")

    signer = SignerReference(
        SignerKind.API_AGENT,
        "file",
        "file-secret://hyperliquid-testnet",
    )
    secrets = LocalFileSecretProvider({signer.reference: secret_path})
    capabilities = default_testnet_capabilities()
    session = BrokerRuntimeSession(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        account=AccountReference(AccountScope.MASTER, args.account_address),
        signer=signer,
        signer_provider=secrets,
        capabilities=capabilities,
        execution_scope="hypercore:default",
        lifecycle_id=f"testnet-proof-{args.approval_id}",
    )
    approval = ExternalEnvironmentApproval(
        environment=BrokerEnvironment.TESTNET,
        approval_id=args.approval_id,
        release_sha=args.release_sha,
        approved_by=args.approved_by,
        approved_at=datetime.now(UTC),
        account_address=args.account_address,
        lifecycle_id=session.lifecycle_id,
    )
    backend = NautilusHyperliquidTestnetBackend(
        session=session,
        config=HyperliquidTestnetBackendConfig(
            account_address=args.account_address,
            capabilities=capabilities,
        ),
        secrets=secrets,
    )
    runtime = NautilusHyperliquidRuntime(
        session=session,
        backend=backend,
        config=NautilusRuntimeConfig(
            expected_version=backend.metadata.version,
            expected_commit=backend.metadata.commit,
            policy=RuntimeActivationPolicy(testnet_approval=approval),
            expected_release_sha=args.release_sha,
        ),
    )

    # Validate the file format without exposing the key.  The provider owns
    # this read; the canonical runtime only sees the opaque signer reference.
    secrets.resolve_private_key(signer)
    health = runtime.start()
    result: dict[str, object] = {
        "environment": health.environment.value,
        "broker_id": health.broker_id,
        "account_address": session.account.address,
        "execution_scope": session.execution_scope,
        "lifecycle_id": session.lifecycle_id,
        "release_sha": args.release_sha,
        "preflight": "passed",
        "network_io": bool(args.execute_testnet),
        "write_executed": False,
    }
    if not args.execute_testnet:
        print(json.dumps(result, sort_keys=True))
        return 0
    values = (args.quantity, args.resting_price, args.aggressive_price, args.fill_price, args.close_price)
    if any(value is None for value in values) and not args.auto_prices:
        parser.error("--execute-testnet requires quantity and all three prices, or --auto-prices")
    proof = run_testnet_lifecycle(
        runtime=runtime,
        plan=TestnetProofPlan(
            instrument_id=args.instrument,
            quantity=args.quantity,
            resting_price=args.resting_price,
            aggressive_price=args.aggressive_price,
            fill_price=args.fill_price,
            close_price=args.close_price,
            max_slippage_bps=args.max_slippage_bps,
            max_quote_age_seconds=args.max_quote_age_seconds,
        ),
    )
    evidence_path = args.evidence_output.expanduser().resolve()
    if repo_root == evidence_path or repo_root in evidence_path.parents:
        parser.error("--evidence-output must be outside the standard-broker repository")
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(_proof_payload(proof), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result.update(
        {
            "write_executed": True,
            "entry_state": proof.entry_receipt.state.value,
            "close_state": proof.close_receipt.state.value,
            "evidence_output": str(evidence_path),
            "evidence_class": proof.evidence.identity.evidence_class.value,
            "order_ids": proof.evidence.identity.order_ids,
            "fill_ids": proof.evidence.identity.fill_ids,
            "fee_ids": proof.evidence.identity.fee_ids,
            "position_ids": proof.evidence.identity.position_ids,
            "reconciliation_ids": proof.evidence.identity.reconciliation_ids,
        }
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _proof_payload(proof: object) -> dict[str, object]:
    evidence = proof.evidence
    identity = evidence.identity
    reconciliation = evidence.reconciliation
    return {
        "schema": "standard-broker-testnet-lifecycle-evidence-v1",
        "evidence_class": identity.evidence_class.value,
        "identity": {
            "broker_id": identity.broker_id,
            "environment": identity.environment.value,
            "account_scope": identity.account_scope.value,
            "account_address": identity.account_address,
            "execution_scope": identity.execution_scope,
            "lifecycle_id": identity.lifecycle_id,
            "release_sha": identity.release_sha,
            "order_ids": identity.order_ids,
            "fill_ids": identity.fill_ids,
            "fee_ids": identity.fee_ids,
            "position_ids": identity.position_ids,
            "reconciliation_ids": identity.reconciliation_ids,
        },
        "completed_steps": evidence.completed_steps,
        "reconciliation": {
            "reconciliation_id": reconciliation.reconciliation_id,
            "watermark": reconciliation.watermark,
            "account_address": reconciliation.account_address,
            "lifecycle_id": reconciliation.lifecycle_id,
        },
        "provenance": {
            "source": evidence.provenance.source,
            "execution_scope": evidence.provenance.execution_scope,
            "transport_state": evidence.provenance.transport_state,
            "mapping_revision": evidence.provenance.mapping_revision,
            "received_at": evidence.provenance.received_at.isoformat(),
        },
        "orders": {
            "entry": _receipt_payload(proof.entry_receipt),
            "replacement": _receipt_payload(proof.replacement_receipt),
            "close": _receipt_payload(proof.close_receipt),
        },
    }


def _receipt_payload(receipt: object) -> dict[str, object]:
    return {
        "order_id": receipt.order_id,
        "environment": receipt.environment.value,
        "client_order_id": receipt.client_order_id,
        "state": receipt.state.value,
        "filled_quantity": str(receipt.filled_quantity),
        "remaining_quantity": str(receipt.remaining_quantity),
        "broker_order_id": receipt.broker_order_id,
        "average_fill_price": str(receipt.average_fill_price)
        if receipt.average_fill_price is not None
        else None,
    }


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Keep errors useful without echoing exception payloads that might
        # accidentally include provider material.
        print(json.dumps({"error": type(exc).__name__, "reason": getattr(exc, "reason_code", "runtime_failure")}))
        raise SystemExit(1)
