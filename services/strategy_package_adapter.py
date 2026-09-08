"""Opt-in parity adapter for the external ``trading_strategy`` package.

The internal services remain authoritative.  In ``shadow`` mode the package
is evaluated with the same pure inputs and only mismatches are persisted; the
package result is never returned to a caller and never reaches execution.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Callable

MODE_ENV = "TRADING_ORCHESTRATOR_STRATEGY_IMPL"
INTERNAL = "internal"
SHADOW = "shadow"
PACKAGE_ROOT = "/Users/wendy/work/trading-strategy"
RECEIPT_PATH = Path("strategy_adapter") / "mismatch_receipts.json"
ABS_TOLERANCE = 1e-9
REL_TOLERANCE = 1e-9


def strategy_impl() -> str:
    mode = str(os.environ.get(MODE_ENV, INTERNAL)).strip().lower() or INTERNAL
    if mode not in {INTERNAL, SHADOW}:
        raise ValueError(f"{MODE_ENV} must be internal or shadow")
    return mode


def build_dca_preview(
    cycle_id: str,
    payload: dict[str, Any] | None,
    *,
    market: dict[str, Any],
    account: dict[str, Any] | None,
    config: dict[str, Any],
    output_root: Path | None = None,
) -> dict[str, Any]:
    from services.dca_plan import build_dca_preview as internal_builder

    return _run(
        "dca_preview",
        internal_builder,
        _package_builder("build_dca_preview"),
        (cycle_id, payload),
        {"market": market, "account": account, "config": config},
        output_root=output_root,
    )


def build_grid_preview(
    cycle_id: str,
    payload: dict[str, Any] | None = None,
    *,
    market: dict[str, Any],
    account: dict[str, Any] | None = None,
    config: dict[str, Any],
    allow_unsafe_manual_preview: bool = False,
    output_root: Path | None = None,
) -> dict[str, Any]:
    from services.grid_sizing import build_grid_preview as internal_builder

    kwargs = {
        "market": market,
        "account": account,
        "config": config,
        "allow_unsafe_manual_preview": allow_unsafe_manual_preview,
    }
    return _run(
        "grid_preview",
        internal_builder,
        _package_builder("build_grid_preview"),
        (cycle_id, payload),
        kwargs,
        output_root=output_root,
    )


def build_dca_strategy_plan(
    preview: dict[str, Any],
    *,
    strategy_plan_id: str,
    version: int,
    locked_at: str,
    strategy_session_id: str | None = None,
    strategy_revision_id: str | None = None,
    output_root: Path | None = None,
) -> dict[str, Any]:
    from services.dca_plan import build_dca_strategy_plan as internal_builder

    kwargs = {
        "strategy_plan_id": strategy_plan_id,
        "version": version,
        "locked_at": locked_at,
        "strategy_session_id": strategy_session_id,
        "strategy_revision_id": strategy_revision_id,
    }
    return _run(
        "dca_strategy_plan",
        internal_builder,
        _package_builder("build_dca_strategy_plan"),
        (preview,),
        kwargs,
        output_root=output_root,
    )


def _package_builder(name: str) -> Callable[..., Any]:
    def invoke(*args: Any, **kwargs: Any) -> Any:
        if PACKAGE_ROOT not in sys.path:
            sys.path.insert(0, PACKAGE_ROOT)
        from trading_strategy import __dict__ as package

        return package[name](*args, **kwargs)

    return invoke


def _run(
    operation: str,
    internal_builder: Callable[..., Any],
    package_builder: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    output_root: Path | None,
) -> Any:
    internal = internal_builder(*args, **kwargs)
    if strategy_impl() != SHADOW:
        return internal

    try:
        package = package_builder(*args, **kwargs)
        differences = _differences(_normalise(internal), _normalise(package))
    except Exception as exc:  # shadow evidence must not affect authority
        differences = [{
            "path": "$",
            "classification": "package_error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }]
    if differences and output_root is not None:
        _write_mismatch(Path(output_root), operation, args, differences)
    return internal


def _normalise(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _normalise(dataclasses.asdict(value))
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(key): _normalise(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple, set)):
        return [_normalise(item) for item in value]
    return value


def _differences(left: Any, right: Any, path: str = "$") -> list[dict[str, Any]]:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)) and not isinstance(left, bool) and not isinstance(right, bool):
        delta = abs(float(left) - float(right))
        scale = max(abs(float(left)), abs(float(right)), 1.0)
        return [] if delta <= ABS_TOLERANCE + REL_TOLERANCE * scale else [{"path": path, "classification": "precision", "left": left, "right": right, "numeric_delta": delta}]
    if type(left) is not type(right):
        return [{"path": path, "classification": "schema", "left": left, "right": right}]
    if isinstance(left, dict):
        keys = sorted(set(left) | set(right))
        return sum((_differences(left.get(key), right.get(key), f"{path}.{key}") for key in keys if key in left and key in right), []) + [
            {"path": f"{path}.{key}", "classification": "schema", "left": left.get(key), "right": right.get(key)}
            for key in keys if (key in left) != (key in right)
        ]
    if isinstance(left, list):
        differences = []
        for index in range(max(len(left), len(right))):
            if index >= len(left) or index >= len(right):
                differences.append({"path": f"{path}[{index}]", "classification": "schema", "left": left[index] if index < len(left) else None, "right": right[index] if index < len(right) else None})
            else:
                differences.extend(_differences(left[index], right[index], f"{path}[{index}]"))
        return differences
    return [] if left == right else [{"path": path, "classification": "semantic", "left": left, "right": right}]


def _write_mismatch(output_root: Path, operation: str, args: tuple[Any, ...], differences: list[dict[str, Any]]) -> None:
    from services.journal_store import load_json, write_json

    payload = {"operation": operation, "differences": differences, "input_digest": _digest(args)}
    path = output_root / RECEIPT_PATH
    rows = load_json(path)
    rows.append(payload)
    write_json(path, rows)


def _digest(value: Any) -> str:
    raw = json.dumps(_normalise(value), sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()
