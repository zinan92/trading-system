"""Frozen platform-level Legacy-to-Nautilus parity fixture contract."""

from __future__ import annotations


PLATFORM_PARITY_SCHEMA = "dualtrack-nautilus-parity-fixture-gate-v1"

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
