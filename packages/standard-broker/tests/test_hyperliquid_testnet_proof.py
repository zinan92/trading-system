from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
import unittest

from standard_broker.adapters.hyperliquid import (
    TestnetProofPlan as ProofPlan,
    default_testnet_capabilities,
    run_testnet_lifecycle,
)
from standard_broker.models import AccountScope, BrokerEnvironment, Provenance, SignerKind
from standard_broker.runtime import AccountReference, BrokerRuntimeSession, SignerReference


class FakeExternalRuntime:
    def __init__(self) -> None:
        capabilities = default_testnet_capabilities()
        self.session = BrokerRuntimeSession(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.TESTNET,
            account=AccountReference(AccountScope.MASTER, "0x" + "11" * 20),
            signer=SignerReference(SignerKind.API_AGENT, "fixture", "fixture://testnet"),
            signer_provider=SimpleNamespace(sign=lambda signer, payload: b"fixture"),
            capabilities=capabilities,
            execution_scope="hypercore:default",
            lifecycle_id="testnet-proof-fixture-1",
        )
        self._config = SimpleNamespace(expected_release_sha="a" * 40)
        self._backend = SimpleNamespace(local_only=False, external_network=True)
        self._close_submitted = False
        self._replacement_submitted = False
        self._replacement_canceled = False
        self._fill_submitted = False

    def start(self) -> None:
        return None

    def _provenance(self) -> Provenance:
        return Provenance(
            source="nautilus-hyperliquid.testnet",
            execution_scope=self.session.execution_scope,
            transport_state="external_testnet",
            mapping_revision=self.session.capabilities.revision,
            received_at=datetime.now(UTC),
        )

    def _filled_event(self, *, close: bool, fill: bool = False) -> dict[str, object]:
        return {
            "status": "filled",
            "oid": 202 if close else 303 if fill else 101,
            "cloid": "0xfixture-close" if close else "0xfixture-fill" if fill else "0xfixture-entry-replacement",
            "coin": "HYPE",
            "side": "A" if close else "B",
            "px": "43" if close else "66.666",
            "sz": "0.3",
            "time": 1_800_000_000_000,
            "tid": 702 if close else 701,
            "fee": "0.01",
            "feeToken": "USDC",
            "crossed": True,
            "closedPnl": "0",
        }

    def _invoke_native(self, port: str, operation: str, request: object) -> object:
        if port == "instrument":
            return {
                "meta": {"universe": [{"name": "HYPE", "szDecimals": 2, "maxLeverage": 10}]},
                "provenance": self._provenance(),
            }
        if port == "market_data":
            return {
                "mid": "54.833",
                "bbo": {"coin": "HYPE", "bbo": [{"px": "43"}, {"px": "66.666"}], "time": 1_800_000_000_000},
                "provenance": self._provenance(),
            }
        if port == "order_execution" and operation == "submit":
            if request.get("reduceOnly"):
                self._close_submitted = True
                event = self._filled_event(close=True)
                return {"response": {"data": {"statuses": [{"filled": {
                    "oid": event["oid"],
                    "side": event["side"],
                    "totalSz": event["sz"],
                    "avgPx": event["px"],
                    "tid": event["tid"],
                    "time": event["time"],
                    "coin": event["coin"],
                    "fee": event["fee"],
                    "feeToken": event["feeToken"],
                    "crossed": event["crossed"],
                }}]}}, "provenance": self._provenance()}
            if request.get("tif") == "IOC":
                self._fill_submitted = True
                event = self._filled_event(close=False, fill=True)
                return {"response": {"data": {"statuses": [{"filled": {
                    "oid": event["oid"],
                    "side": event["side"],
                    "totalSz": event["sz"],
                    "avgPx": event["px"],
                    "tid": event["tid"],
                    "time": event["time"],
                    "coin": event["coin"],
                    "fee": event["fee"],
                    "feeToken": event["feeToken"],
                    "crossed": event["crossed"],
                }}]}}, "provenance": self._provenance()}
            return {"response": {"data": {"statuses": [{"resting": {"oid": 101}}]}}, "provenance": self._provenance()}
        if port == "order_execution" and operation == "replace":
            self._replacement_submitted = True
            return {"status": "ok", "provenance": self._provenance()}
        if port == "order_execution" and operation == "cancel":
            self._replacement_canceled = True
            return {"status": "ok", "provenance": self._provenance()}
        if port == "order_execution" and operation == "query":
            event = (
                {"status": "canceled", "oid": 202, "coin": "HYPE", "side": "B", "time": 1_800_000_000_000}
                if self._replacement_canceled
                else {"status": "resting", "oid": 202, "coin": "HYPE", "side": "B", "time": 1_800_000_000_000}
                if self._replacement_submitted and not self._fill_submitted
                else self._filled_event(close=self._close_submitted, fill=self._fill_submitted)
            )
            event["cloid"] = str(request.get("cloid"))
            return event | {"provenance": self._provenance()}
        if port == "order_execution" and operation == "open_orders":
            return {"orders": [], "provenance": self._provenance()}
        if port == "account" and operation == "positions":
            rows = [] if self._close_submitted else [{"report_id": "position-1", "quantity": "0.3"}]
            return {"positions": rows, "provenance": self._provenance()}
        if port == "account" and operation == "read":
            return {"data": {"ts_event": 1_800_000_000_000}, "provenance": self._provenance()}
        raise AssertionError((port, operation, request))


class TestnetProofRunnerTests(unittest.TestCase):
    def test_deterministic_lifecycle_produces_external_evidence(self) -> None:
        result = run_testnet_lifecycle(
            runtime=FakeExternalRuntime(),
            plan=ProofPlan(
                instrument_id="HYPE-USD-PERP",
                quantity=Decimal("0.3"),
                resting_price=Decimal("40"),
                aggressive_price=Decimal("42"),
                fill_price=Decimal("66.666"),
                close_price=Decimal("43"),
                poll_attempts=2,
                poll_interval_seconds=0,
            ),
            sleep=lambda _: None,
        )

        self.assertEqual(result.evidence.identity.evidence_class.value, "testnet")
        self.assertEqual(result.evidence.reconciliation.watermark, 1_800_000_000_000)
        self.assertEqual(result.evidence.provenance.transport_state, "external_testnet")
        self.assertEqual(result.close_receipt.state.value, "filled")


if __name__ == "__main__":
    unittest.main()
