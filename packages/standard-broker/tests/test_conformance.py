import unittest
from types import SimpleNamespace

from standard_broker.capabilities import PORT_NAMES, CapabilityDescriptor
from standard_broker.conformance import EvidenceKind, run_paper_conformance
from standard_broker.models import BrokerEnvironment, BrokerIdentity, SignerKind
from standard_broker.paper import PaperBrokerAdapter
from standard_broker.security import find_secret_like_literals


class ConformanceTests(unittest.TestCase):
    def make_adapter(self) -> PaperBrokerAdapter:
        return PaperBrokerAdapter(
            identity=BrokerIdentity(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                signer_kind=SignerKind.NONE,
            ),
            capabilities=CapabilityDescriptor(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                operations={
                    "preflight": frozenset({"read"}),
                    **{port: frozenset({"read"}) for port in PORT_NAMES},
                },
                revision="conformance-v1",
            ),
        )

    def test_paper_conformance_passes_with_static_fixture_evidence(self) -> None:
        report = run_paper_conformance(self.make_adapter())

        self.assertTrue(report.passed)
        self.assertEqual(report.evidence_kind, EvidenceKind.STATIC_FIXTURE)
        self.assertFalse(report.network_io)
        self.assertFalse(report.real_money_eligible)
        self.assertEqual(report.failures, ())
        self.assertIn("paper_boundary", report.checks)

    def test_conformance_fails_if_preflight_claims_network(self) -> None:
        adapter = self.make_adapter()
        unsafe = SimpleNamespace(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.PAPER,
            network_io=True,
            real_money_eligible=False,
            credential_required=False,
            ports=PORT_NAMES,
        )
        adapter.preflight = lambda: unsafe  # type: ignore[method-assign]

        report = run_paper_conformance(adapter)

        self.assertFalse(report.passed)
        self.assertIn("paper_network_io", report.failures)

    def test_conformance_fails_if_required_port_is_missing(self) -> None:
        complete = self.make_adapter()
        adapter = SimpleNamespace(
            identity=complete.identity,
            capabilities=complete.capabilities,
            ports={name: complete.ports[name] for name in PORT_NAMES[:-1]},
            preflight=complete.preflight,
        )

        report = run_paper_conformance(adapter)

        self.assertFalse(report.passed)
        self.assertIn("canonical_ports", report.failures)

    def test_secret_scanner_detects_key_shaped_text_without_storing_a_key(self) -> None:
        key_shaped = "0x" + "a" * 64

        self.assertTrue(find_secret_like_literals(f"PRIVATE={key_shaped}"))
        self.assertEqual(find_secret_like_literals("PRIVATE=0x..."), ())


if __name__ == "__main__":
    unittest.main()
