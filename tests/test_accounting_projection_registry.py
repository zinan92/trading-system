from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from schemas.accounting import AccountingSnapshot, build_accounting_snapshot
from services.accounting_projection_composition import (
    BROKER_ACCOUNTING_PLUGINS,
    build_default_broker_accounting_registry,
    project_broker_accounting,
    project_broker_accounting_fail_honest,
)
from services.accounting_projection_port import BrokerAccountingPluginDescriptor
from services.accounting_projection_registry import BrokerAccountingPluginRegistry
from services.dualtrack_nautilus_parity_contract import PLATFORM_CODE_PATHS
from tests.test_broker_accounting_projection import _binance_report, _tiger_report


class CustomAccountingAdapter:
    name = "custom_accounting"
    source_names = ("custom_broker",)

    def project(self, source: Mapping[str, Any]) -> AccountingSnapshot:
        provider = str(source.get("provider") or "")
        return _snapshot(provider=provider, source_schema_version="custom-receipt-v1")


class WrongSourceAccountingAdapter:
    name = "wrong_source_accounting"
    source_names = ("wrong_source",)

    def project(self, source: Mapping[str, Any]) -> AccountingSnapshot:
        return _snapshot(provider="forged_source", source_schema_version="wrong-source-v1")


class InvalidSnapshotAccountingAdapter:
    name = "invalid_snapshot_accounting"
    source_names = ("invalid_snapshot",)

    def project(self, source: Mapping[str, Any]):
        return {"source_name": source.get("provider")}


def _snapshot(*, provider: str, source_schema_version: str) -> AccountingSnapshot:
    return build_accounting_snapshot(
        source_type="broker_reconciliation",
        source_name=provider,
        source_schema_version=source_schema_version,
        scope={"run_date": "2026-07-18", "provider": provider},
        currency="USD",
        orders=[],
        fills=[],
        positions=[],
        trades=[],
        counts={
            "order_count": None,
            "open_order_count": None,
            "fill_count": None,
            "entry_fill_count": None,
            "exit_fill_count": None,
            "trade_count": None,
            "open_trade_count": None,
            "completed_trade_count": None,
            "position_count": None,
            "open_position_count": None,
        },
        pnl={
            "gross_realized_pnl": None,
            "fees": None,
            "funding": None,
            "net_realized_pnl": None,
            "unrealized_pnl": None,
            "net_pnl": None,
            "slippage": None,
        },
        account={
            "starting_balance": None,
            "ending_cash": None,
            "equity": None,
            "available_balance": None,
            "margin": None,
            "exposure": None,
            "leverage": None,
        },
        completeness={"status": "partial", "observed": {}, "limitations": [], "unknown_is_not_zero": True},
        reconciliation={"status": "pass", "issues": [], "identity_policy": "custom"},
    )


def _custom_registry() -> BrokerAccountingPluginRegistry:
    return (
        BrokerAccountingPluginRegistry()
        .register(
            BrokerAccountingPluginDescriptor(
                name="custom_accounting",
                implementation=f"{__name__}.CustomAccountingAdapter",
                source_names=("custom_broker",),
                source_schema_version="custom-receipt-v1",
                default_currency="USD",
            ),
            CustomAccountingAdapter,
        )
        .freeze()
    )


def test_default_registry_is_explicit_frozen_and_content_hashed() -> None:
    descriptors = {row.name: row for row in BROKER_ACCOUNTING_PLUGINS.descriptors()}

    assert set(descriptors) == {"binance_usdm_accounting", "tiger_openapi_accounting"}
    assert descriptors["binance_usdm_accounting"].source_names == (
        "binance_usdm",
        "binance_usdm_futures",
    )
    assert descriptors["binance_usdm_accounting"].read_only is True
    assert descriptors["tiger_openapi_accounting"].source_schema_version == "tiger-account-sync-v1"
    assert BROKER_ACCOUNTING_PLUGINS.fingerprint.startswith("sha256:")
    assert (
        build_default_broker_accounting_registry().fingerprint
        == BROKER_ACCOUNTING_PLUGINS.fingerprint
    )

    with pytest.raises(RuntimeError, match="frozen"):
        BROKER_ACCOUNTING_PLUGINS.register(
            BrokerAccountingPluginDescriptor(
                name="late_accounting",
                implementation="tests.LateAccounting",
                source_names=("late_broker",),
                source_schema_version="late-v1",
                default_currency="USD",
            ),
            CustomAccountingAdapter,
        )


def test_custom_source_projects_through_registry_without_provider_branch() -> None:
    snapshot = project_broker_accounting(
        {"provider": "custom_broker", "run_date": "2026-07-18"},
        registry=_custom_registry(),
    )

    assert snapshot.source_name == "custom_broker"
    assert snapshot.source_schema_version == "custom-receipt-v1"
    assert snapshot.completeness["unknown_is_not_zero"] is True


def test_unknown_source_fails_before_constructing_any_adapter() -> None:
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return CustomAccountingAdapter()

    registry = (
        BrokerAccountingPluginRegistry()
        .register(
            BrokerAccountingPluginDescriptor(
                name="custom_accounting",
                implementation=f"{__name__}.CustomAccountingAdapter",
                source_names=("custom_broker",),
                source_schema_version="custom-receipt-v1",
                default_currency="USD",
            ),
            factory,
        )
        .freeze()
    )

    with pytest.raises(ValueError, match="unsupported broker accounting provider"):
        project_broker_accounting(
            {"provider": "unknown_broker", "run_date": "2026-07-18"},
            registry=registry,
        )
    assert calls == 0


def test_registry_rejects_empty_duplicate_source_and_non_read_only_plugins() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        BrokerAccountingPluginRegistry().freeze()

    registry = BrokerAccountingPluginRegistry().register(
        BrokerAccountingPluginDescriptor(
            name="custom_accounting",
            implementation=f"{__name__}.CustomAccountingAdapter",
            source_names=("custom_broker",),
            source_schema_version="custom-receipt-v1",
            default_currency="USD",
        ),
        CustomAccountingAdapter,
    )
    with pytest.raises(ValueError, match="duplicate broker accounting source"):
        registry.register(
            BrokerAccountingPluginDescriptor(
                name="second_accounting",
                implementation="tests.SecondAccounting",
                source_names=("custom_broker",),
                source_schema_version="second-v1",
                default_currency="USD",
            ),
            CustomAccountingAdapter,
        )
    with pytest.raises(ValueError, match="duplicate broker accounting plugin"):
        registry.register(
            BrokerAccountingPluginDescriptor(
                name="custom_accounting",
                implementation="tests.SecondAccounting",
                source_names=("second_broker",),
                source_schema_version="second-v1",
                default_currency="USD",
            ),
            CustomAccountingAdapter,
        )
    with pytest.raises(ValueError, match="must be read-only"):
        BrokerAccountingPluginRegistry().register(
            BrokerAccountingPluginDescriptor(
                name="mutable_accounting",
                implementation="tests.MutableAccounting",
                source_names=("mutable_broker",),
                source_schema_version="mutable-v1",
                default_currency="USD",
                read_only=False,
            ),
            CustomAccountingAdapter,
        )
    with pytest.raises(ValueError, match="capabilities are invalid"):
        BrokerAccountingPluginRegistry().register(
            BrokerAccountingPluginDescriptor(
                name="overpowered_accounting",
                implementation="tests.OverpoweredAccounting",
                source_names=("overpowered_broker",),
                source_schema_version="overpowered-v1",
                default_currency="USD",
                capabilities=("project", "submit_order"),
            ),
            CustomAccountingAdapter,
        )


def test_registry_rejects_false_identity_invalid_result_and_forged_source() -> None:
    false_identity = (
        BrokerAccountingPluginRegistry()
        .register(
            BrokerAccountingPluginDescriptor(
                name="custom_accounting",
                implementation="tests.NotTheReturnedAdapter",
                source_names=("custom_broker",),
                source_schema_version="custom-receipt-v1",
                default_currency="USD",
            ),
            CustomAccountingAdapter,
        )
        .freeze()
    )
    with pytest.raises(TypeError, match="implementation mismatch"):
        false_identity.project(
            "custom_broker",
            {"provider": "custom_broker", "run_date": "2026-07-18"},
        )

    invalid_snapshot = (
        BrokerAccountingPluginRegistry()
        .register(
            BrokerAccountingPluginDescriptor(
                name="invalid_snapshot_accounting",
                implementation=f"{__name__}.InvalidSnapshotAccountingAdapter",
                source_names=("invalid_snapshot",),
                source_schema_version="invalid-snapshot-v1",
                default_currency="USD",
            ),
            InvalidSnapshotAccountingAdapter,
        )
        .freeze()
    )
    with pytest.raises(TypeError, match="invalid snapshot"):
        invalid_snapshot.project(
            "invalid_snapshot",
            {"provider": "invalid_snapshot", "run_date": "2026-07-18"},
        )

    wrong_source = (
        BrokerAccountingPluginRegistry()
        .register(
            BrokerAccountingPluginDescriptor(
                name="wrong_source_accounting",
                implementation=f"{__name__}.WrongSourceAccountingAdapter",
                source_names=("wrong_source",),
                source_schema_version="wrong-source-v1",
                default_currency="USD",
            ),
            WrongSourceAccountingAdapter,
        )
        .freeze()
    )
    with pytest.raises(ValueError, match="wrong source_name"):
        wrong_source.project(
            "wrong_source",
            {"provider": "wrong_source", "run_date": "2026-07-18"},
        )


def test_fail_honest_unknown_source_preserves_unknown_instead_of_zero() -> None:
    snapshot = project_broker_accounting_fail_honest(
        {"provider": "unknown_broker", "run_date": "2026-07-18"}
    ).to_dict()

    assert snapshot["source_name"] == "unknown_broker"
    assert snapshot["reconciliation"]["status"] == "blocked"
    assert all(value is None for value in snapshot["counts"].values())
    assert all(value is None for value in snapshot["pnl"].values())
    assert snapshot["completeness"]["unknown_is_not_zero"] is True


def test_binance_alias_resolves_to_same_adapter_without_relabeling_source() -> None:
    report = _binance_report()
    report["provider"] = "binance_usdm_futures"

    snapshot = project_broker_accounting(report)

    assert snapshot.source_name == "binance_usdm_futures"
    assert snapshot.source_schema_version == "broker-reconciliation-v1"


def test_recognized_tiger_failure_preserves_registered_schema_and_currency() -> None:
    report = _tiger_report()
    report["exchange_accounting"]["net_realized_pnl_estimate"] = float("nan")

    snapshot = project_broker_accounting_fail_honest(report).to_dict()

    assert snapshot["source_name"] == "tiger_openapi"
    assert snapshot["source_schema_version"] == "tiger-account-sync-v1"
    assert snapshot["currency"] == "USD"
    assert snapshot["reconciliation"]["status"] == "blocked"
    assert snapshot["pnl"]["net_realized_pnl"] is None


def test_a11_binance_and_tiger_snapshot_ids_are_frozen_across_adapter_cutover() -> None:
    assert project_broker_accounting(_binance_report()).snapshot_id == (
        "accounting-07c106a8682cb8be79954801823763d97b0dccff5086e66a0ac9a2a48055b3cb"
    )
    assert project_broker_accounting(_tiger_report()).snapshot_id == (
        "accounting-a8ba1a6cf165aa0fed9cf6f720d09033e512378adb1b86bfabd6d7a0ca201a68"
    )


def test_platform_parity_hash_covers_every_accounting_plugin_semantic_file() -> None:
    assert {
        "services/accounting_projection.py",
        "services/accounting_projection_core.py",
        "services/accounting_projection_port.py",
        "services/accounting_projection_registry.py",
        "services/accounting_projection_composition.py",
        "services/accounting_broker_common.py",
        "services/accounting_binance_adapter.py",
        "services/accounting_tiger_adapter.py",
    }.issubset(PLATFORM_CODE_PATHS)
