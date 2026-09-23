from dataclasses import replace
from dataclasses import dataclass

from standard_broker import EvidenceKind, run_external_conformance
from standard_broker.adapters.hyperliquid.profile import HYPERLIQUID_TESTNET_PROFILE, build_hyperliquid_testnet_host
from standard_broker.conformance import _safe_canonical
from standard_broker.external_host import ExternalBrokerHost
from standard_broker.protection import ProtectionCapabilityMatrix

from test_external_profile import _profile_context, _profile_runtime, _profile_session
from test_external_read_facts import NOW, _adapter as read_adapter, _provenance
from test_external_reconciliation import _assemble


@dataclass(frozen=True)
class ProviderPayloadFixture:
    signature: str | None = None
    raw_payload: object | None = None


def test_external_conformance_passes_without_transport_invocation() -> None:
    session = _profile_session()
    runtime = _profile_runtime(session)
    host = build_hyperliquid_testnet_host(context=_profile_context(session), runtime=runtime)
    adapter = read_adapter()
    ticker = adapter.map_ticker(
        request_id="conformance-ticker",
        broker_symbol="SOL",
        raw={
            "mid": "100",
            "bbo": {"coin": "SOL", "bbo": [{"px": "99"}, {"px": "101"}], "time": 1787461200000},
            "provenance": _provenance(),
        },
        now=NOW,
    )
    reconciliation = _assemble()

    report = run_external_conformance(
        host=host,
        read_facts=(ticker,),
        order_receipts=reconciliation.open_orders.fact.data,
        reconciliation=reconciliation,
    )

    assert report.passed is True
    assert report.evidence_kind is EvidenceKind.EXTERNAL_STATIC_FIXTURE
    assert report.network_io is False
    assert report.credential_required is False
    assert report.write_credential_required is True
    assert report.real_money_eligible is False
    assert report.external_network_bound is True
    assert runtime.invoke_calls == []
    assert "trading_system_handoff" in report.checks


def test_external_conformance_rejects_non_pass_reconciliation() -> None:
    session = _profile_session()
    runtime = _profile_runtime(session)
    host = build_hyperliquid_testnet_host(context=_profile_context(session), runtime=runtime)
    adapter = read_adapter()
    ticker = adapter.map_ticker(
        request_id="conformance-ticker-incomplete",
        broker_symbol="SOL",
        raw={
            "mid": "100",
            "bbo": {"coin": "SOL", "bbo": [{"px": "99"}, {"px": "101"}], "time": 1787461200000},
            "provenance": _provenance(),
        },
        now=NOW,
    )
    incomplete = _assemble(funding=None, funding_applicable=True)

    report = run_external_conformance(
        host=host,
        read_facts=(ticker,),
        order_receipts=(),
        reconciliation=incomplete,
    )

    assert report.passed is False
    assert "canonical_order_receipts" in report.failures
    assert "reconciliation_evidence" in report.failures


def test_security_recursion_rejects_bytes_mappings_and_provider_objects() -> None:
    assert _safe_canonical(b"signed-bytes") is False
    assert _safe_canonical({"native": "provider"}) is False
    assert _safe_canonical(ProviderPayloadFixture()) is False


def test_external_conformance_rejects_forged_fact_and_receipt_identity() -> None:
    session = _profile_session()
    runtime = _profile_runtime(session)
    host = build_hyperliquid_testnet_host(context=_profile_context(session), runtime=runtime)
    adapter = read_adapter()
    ticker = adapter.map_ticker(
        request_id="conformance-ticker-forged",
        broker_symbol="SOL",
        raw={
            "mid": "100",
            "bbo": {"coin": "SOL", "bbo": [{"px": "99"}, {"px": "101"}], "time": 1787461200000},
            "provenance": _provenance(),
        },
        now=NOW,
    )
    reconciliation = _assemble()
    forged_receipt = replace(reconciliation.open_orders.fact.data[0], account_address="0x" + "22" * 20)

    report = run_external_conformance(
        host=host,
        read_facts=(ticker,),
        order_receipts=(forged_receipt,),
        reconciliation=reconciliation,
    )

    assert report.passed is False
    assert "canonical_order_receipts" in report.failures


def test_external_conformance_rejects_forged_fact_digest() -> None:
    session = _profile_session()
    runtime = _profile_runtime(session)
    host = build_hyperliquid_testnet_host(context=_profile_context(session), runtime=runtime)
    adapter = read_adapter()
    ticker = adapter.map_ticker(
        request_id="conformance-ticker-digest",
        broker_symbol="SOL",
        raw={
            "mid": "100",
            "bbo": {"coin": "SOL", "bbo": [{"px": "99"}, {"px": "101"}], "time": 1787461200000},
            "provenance": _provenance(),
        },
        now=NOW,
    )
    reconciliation = _assemble()
    forged_fact = replace(ticker, fact_digest="sha256:" + "0" * 64)

    report = run_external_conformance(
        host=host,
        read_facts=(forged_fact,),
        order_receipts=reconciliation.open_orders.fact.data,
        reconciliation=reconciliation,
    )

    assert report.passed is False
    assert "canonical_read_facts" in report.failures


def test_external_conformance_rejects_modified_protection_matrix() -> None:
    session = _profile_session()
    runtime = _profile_runtime(session)
    values = dict(HYPERLIQUID_TESTNET_PROFILE.protection_capabilities.values)
    values["stop_loss_market"] = True
    modified_profile = replace(
        HYPERLIQUID_TESTNET_PROFILE,
        protection_capabilities=ProtectionCapabilityMatrix(
            profile_id=HYPERLIQUID_TESTNET_PROFILE.protection_capabilities.profile_id,
            values=values,
        ),
    )
    host = ExternalBrokerHost(
        context=_profile_context(session),
        runtime=runtime,
        profile=modified_profile,
    )

    report = run_external_conformance(
        host=host,
        read_facts=(),
        order_receipts=(),
        reconciliation=None,
    )

    assert report.passed is False
    assert "protection_capability_gaps" in report.failures
