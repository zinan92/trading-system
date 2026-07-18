"""Compatibility facade for canonical accounting projection boundaries."""

from schemas.accounting import build_accounting_snapshot
from services.accounting_projection_composition import (
    broker_accounting_snapshot_payload,
    project_broker_accounting,
    project_broker_accounting_fail_honest,
)
from services.accounting_projection_core import (
    AccountingContractError,
    project_execution_accounting,
)


__all__ = (
    "AccountingContractError",
    "broker_accounting_snapshot_payload",
    "build_accounting_snapshot",
    "project_broker_accounting",
    "project_broker_accounting_fail_honest",
    "project_execution_accounting",
)
