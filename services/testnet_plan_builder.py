"""Public Dashboard-to-Testnet plan projection seam."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def build_plan(
    preview: Mapping[str, Any],
    confirmation: Mapping[str, Any],
    constraints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the exact plan shape shared by proof and scheduled ticks.

    The implementation remains owned by the proof driver for compatibility
    with its existing validation helpers; this module is the dependency-safe
    public seam used by runtime composition.
    """
    from pipelines.testnet_proof_driver import _build_plan_from_dashboard

    options = dict(constraints or {})
    catalog_rows = options.get("catalog_rows")
    if catalog_rows is not None and not isinstance(catalog_rows, Sequence):
        raise TypeError("catalog_rows must be a sequence")
    return _build_plan_from_dashboard(preview, confirmation, catalog_rows=catalog_rows)


__all__ = ["build_plan"]
