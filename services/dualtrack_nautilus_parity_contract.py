"""Frozen platform-level Legacy-to-Nautilus parity fixture contract."""

from __future__ import annotations

import hashlib
from pathlib import Path


PLATFORM_PARITY_SCHEMA = "dualtrack-nautilus-parity-fixture-gate-v1"
PLATFORM_PARITY_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
EXPECTED_NAUTILUS_VERSION = "1.230.0"

FIXTURE_CLASSES: dict[str, tuple[str, ...]] = {
    "market_entry_long_short": ("long_stop", "short_stop"),
    "limit_entry_waits_for_touch": ("limit_entry_waits_for_touch",),
    "scale_in_weighted_average": ("scale_in_weighted_average",),
    "partial_reduction_then_close": ("partial_reduction_then_close",),
    "stop_loss_and_take_profit": ("long_stop", "long_target", "short_stop", "short_target"),
    "same_bar_conservative_priority": ("long_same_bar_stop_first",),
    "fees_slippage_margin_exposure_pnl": ("scale_in_weighted_average", "partial_reduction_then_close"),
    "duplicate_command_event_replay": ("duplicate_command_event_replay",),
    "restart_and_reconciliation": ("restart_replay_and_reconciliation",),
    "historical_machine_residual_units": (),
}

PLATFORM_CODE_PATHS = (
    "pipelines/dualtrack_machine_residual_check.py",
    "pipelines/dualtrack_nautilus_parity_gate.py",
    "pipelines/dualtrack_shadow_cutover_status.py",
    "schemas/accounting.py",
    "services/accounting_projection.py",
    "services/dualtrack_config.py",
    "services/dualtrack_execution_adapter.py",
    "services/dualtrack_execution_contract.py",
    "services/dualtrack_nautilus_execution_adapter.py",
    "services/dualtrack_nautilus_instrument.py",
    "services/dualtrack_nautilus_parity_contract.py",
    "services/dualtrack_scoring.py",
    "services/dualtrack_shadow_execution_adapter.py",
    "services/dualtrack_shadow_input.py",
    "services/dualtrack_shadow_reconciliation.py",
    "services/execution_engine_plugin_registry.py",
    "services/execution_engine_port.py",
    "services/execution_conformance.py",
    "services/execution_plugin_composition.py",
    "services/journal_store.py",
    "services/lab_execution_conformance.py",
    "services/lab_promotion.py",
    "services/legacy_paper_execution_adapter.py",
    "services/strategy_plan_execution.py",
    "services/strategy_shadow.py",
    "services/strategy_shadow_nautilus.py",
    "spikes/dualtrack_nautilus_gold_parity_fixture.py",
    "spikes/dualtrack_nautilus_shadow_replay.py",
)


def platform_parity_code_hash(root: Path | None = None) -> str:
    """Fingerprint every code path whose semantics can admit Lab promotion."""

    repository = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for relative in PLATFORM_CODE_PATHS:
        path = repository / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"
