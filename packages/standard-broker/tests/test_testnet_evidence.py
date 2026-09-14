from datetime import UTC, datetime, timedelta
import unittest

from standard_broker.evidence import (
    EvidenceClass,
    EvidenceIdentity,
    ReconciliationEvidence,
    TESTNET_LIFECYCLE_STEPS,
    ExternalTestnetLifecycleEvidence,
)
from standard_broker.models import AccountScope, BrokerEnvironment, Provenance
from standard_broker.market_data import FreshnessPolicy, FreshnessState


class TestnetEvidenceTests(unittest.TestCase):
    def evidence(self, *, steps=TESTNET_LIFECYCLE_STEPS) -> ExternalTestnetLifecycleEvidence:
        identity = EvidenceIdentity(
            evidence_class=EvidenceClass.TESTNET,
            broker_id="hyperliquid",
            environment=BrokerEnvironment.TESTNET,
            account_scope=AccountScope.MASTER,
            account_address="0x" + "11" * 20,
            execution_scope="hypercore:default",
            lifecycle_id="testnet-proof-1",
            release_sha="a" * 40,
            order_ids=("9001",),
            fill_ids=("7001",),
            fee_ids=("fee-7001",),
            position_ids=("position-1",),
            reconciliation_ids=("recon-1",),
        )
        return ExternalTestnetLifecycleEvidence(
            identity=identity,
            completed_steps=tuple(steps),
            final_reconciliation_id="recon-1",
            reconciliation=ReconciliationEvidence(
                reconciliation_id="recon-1",
                broker_id="hyperliquid",
                environment=BrokerEnvironment.TESTNET,
                account_scope=AccountScope.MASTER,
                account_address="0x" + "11" * 20,
                execution_scope="hypercore:default",
                lifecycle_id="testnet-proof-1",
                watermark=1_800_000_000_000,
                provenance=Provenance(
                    source="nautilus-hyperliquid.testnet",
                    execution_scope="hypercore:default",
                    transport_state="external_testnet",
                    mapping_revision="hyperliquid-testnet-runtime-v1",
                    received_at=datetime.now(UTC),
                ),
            ),
            provenance=Provenance(
                source="nautilus-hyperliquid.testnet",
                execution_scope="hypercore:default",
                transport_state="external_testnet",
                mapping_revision="hyperliquid-testnet-runtime-v1",
                received_at=datetime.now(UTC),
            ),
        )

    def test_requires_all_external_lifecycle_steps(self) -> None:
        evidence = self.evidence()

        self.assertEqual(set(evidence.completed_steps), set(TESTNET_LIFECYCLE_STEPS))
        self.assertEqual(evidence.identity.evidence_class, EvidenceClass.TESTNET)

    def test_rejects_partial_proof(self) -> None:
        with self.assertRaises(ValueError):
            self.evidence(steps=TESTNET_LIFECYCLE_STEPS[:-1])

    def test_rejects_paper_provenance(self) -> None:
        with self.assertRaises(ValueError):
            ExternalTestnetLifecycleEvidence(
                identity=self.evidence().identity,
                completed_steps=TESTNET_LIFECYCLE_STEPS,
                final_reconciliation_id="recon-1",
                reconciliation=self.evidence().reconciliation,
                provenance=Provenance(
                    source="fixture",
                    execution_scope="hypercore:default",
                    transport_state="local_fixture",
                    mapping_revision="hyperliquid-testnet-runtime-v1",
                ),
            )

    def test_external_testnet_transport_can_be_fresh_only_within_the_age_window(self) -> None:
        now = datetime.now(UTC)
        policy = FreshnessPolicy(timedelta(seconds=5))

        self.assertEqual(
            policy.classify(now, now, transport_state="external_testnet"),
            FreshnessState.FRESH,
        )
        self.assertEqual(
            policy.classify(now - timedelta(seconds=6), now, transport_state="external_testnet"),
            FreshnessState.STALE,
        )


if __name__ == "__main__":
    unittest.main()
