from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

CONTRACT_VERSION = "strategy-diff-contract-v1"
FLOAT_ABS_TOLERANCE = 1e-9
FLOAT_REL_TOLERANCE = 1e-9
TRADING_STRATEGY_SHA = "d4daae915c549fe657a98b6cbf0e539886af355c"

_PACKAGE_ROOT = "/Users/wendy/work/trading-strategy"
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)


def _bars(module: Any, n: int = 40) -> list[Any]:
    result = []
    for i in range(n):
        close = 100.0 + (i % 7) * 0.35
        result.append(module.Bar("XAUUSD", "1m", f"2026-01-01T00:{i:02d}:00Z", close, close + 1.2, close - 1.2, close + .1, 1000.0 + i, "fixture-provider"))
    return result


def _market() -> dict[str, Any]:
    def rows(tf: str) -> list[dict[str, Any]]:
        return [{"timestamp": f"2026-01-01T{i:02d}:00:00Z", "open": 100 + i * .1, "high": 101 + i * .1, "low": 99 + i * .1, "close": 100.2 + i * .1, "volume": 10} for i in range(32)]
    return {"status": "ready", "fresh": True, "is_synthetic": False, "provider": "fixture-provider", "symbol": "XAUUSD", "latest_close": 103.2, "bars": rows("1m"), "strategy_timeframes": {"1d": {"provider": "fixture-provider", "is_synthetic": False, "bars": rows("1d")}, "4h": {"provider": "fixture-provider", "is_synthetic": False, "bars": rows("4h")}, "1m": {"provider": "fixture-provider", "is_synthetic": False, "bars": rows("1m")}}}


def _config() -> dict[str, Any]:
    return {"cost_per_side_bp": 0.5, "max_leverage": 10.0, "execution_contract": {"price_decimals": 2, "price_increment": "0.01", "quantity_increment": "0.01"}, "strategy_grid": {"range_timeframe": "1d", "spacing_timeframe": "4h", "execution_timeframe": "1m", "range_atr_period": 14, "spacing_atr_period": 14, "min_grid_count": 2, "max_grid_count": 12, "capital_utilization_cap": 1.0, "min_net_profit_per_grid_usd": 0.01, "styles": {"steady": {"range_atr_multiple": 2, "spacing_atr_multiple": 1}, "aggressive": {"range_atr_multiple": 3, "spacing_atr_multiple": 0.5}}}}


def _dca_payload(i: int) -> dict[str, Any]:
    long = i % 2 == 0
    levels = [102.0 - j * (0.35 + i * .001) for j in range(4)] if long else [102.0 + j * (0.35 + i * .001) for j in range(4)]
    return {"direction": "long" if long else "short", "dca": {"entry_levels": levels, "target_price": 104.0 if long else 100.0, "stop_price": 98.0 if long else 106.0, "notional_per_addition": 100.0 + i, "max_additions": 4, "loop_enabled": False}, "risk_budget": {"leverage": 2.0 + (i % 3)}}


def _grid_payload(i: int) -> dict[str, Any]:
    return {"direction": ("long", "short", "neutral")[i % 3], "style": "aggressive" if i % 2 else "steady", "range": {"low": 90.0, "high": 116.0}, "grid": {"mode": "geometric" if i % 4 == 0 else "arithmetic", "count": 4 + i % 5}, "risk_budget": {"leverage": 10.0}}


def _core_case(i: int) -> dict[str, Any]:
    direction = 1 if i % 2 == 0 else -1
    return {"cycle_id": f"fixture-grid-{i:02d}", "bars": _bars, "direction": direction, "prev_range": 2.0 + i * .01, "spacing_bp": 100.0 + i, "range_k": 3.0, "rung_notional": 100.0 + i, "max_rungs": 3 + i % 3, "cost_per_side_bp": .5, "tp_mult": 1.0 + (i % 2) * .1, "re_arm_max": i % 2, "budget_sizing": bool(i % 3 == 0), "layer": "grid"}


def fixtures() -> list[dict[str, Any]]:
    cases = []
    for i in range(24):
        cases.append({"id": f"grid-sizing-{i:02d}", "family": "grid", "module": "grid_sizing", "args": {"cycle_id": f"fixture-grid-sizing-{i:02d}", "payload": _grid_payload(i), "market": _market(), "account": {"equity": 10000 + i}, "config": _config()}})
        cases.append({"id": f"dca-{i:02d}", "family": "dca", "module": "dca_plan", "args": {"cycle_id": f"fixture-dca-{i:02d}", "payload": _dca_payload(i), "market": _market(), "account": {"equity": 10000 + i}, "config": _config()}})
        cases.append({"id": f"grid-core-{i:02d}", "family": "grid", "module": "grid_core", "args": _core_case(i)})
    invalid_dca = deepcopy(cases[1]["args"])
    invalid_dca["payload"]["dca"]["loop_enabled"] = True
    cases.append({"id": "dca-exception-loop-enabled", "family": "dca", "module": "dca_plan", "args": invalid_dca})
    invalid_dca_direction = deepcopy(cases[1]["args"])
    invalid_dca_direction["payload"]["direction"] = "sideways"
    cases.append({"id": "dca-exception-direction", "family": "dca", "module": "dca_plan", "args": invalid_dca_direction})
    invalid_grid = deepcopy(cases[0]["args"])
    invalid_grid["payload"]["grid"]["mode"] = "unsupported"
    cases.append({"id": "grid-exception-mode", "family": "grid", "module": "grid_sizing", "args": invalid_grid})
    stale_grid = deepcopy(cases[0]["args"])
    stale_grid["market"]["fresh"] = False
    cases.append({"id": "grid-exception-stale-market", "family": "grid", "module": "grid_sizing", "args": stale_grid})
    return cases


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return format(value, ".15g")
    if isinstance(value, dict):
        return {str(k): _jsonable(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(x) for x in value]
    return value


def _invoke(case: dict[str, Any], services: bool) -> Any:
    prefix = "services" if services else "trading_strategy"
    if case["module"] == "grid_sizing":
        from importlib import import_module
        return import_module(f"{prefix}.grid_sizing").build_grid_preview(**case["args"])
    if case["module"] == "dca_plan":
        from importlib import import_module
        return import_module(f"{prefix}.dca_plan").build_dca_preview(**case["args"])
    from importlib import import_module
    mod = import_module(f"{prefix}.dualtrack_grid_core" if services else f"{prefix}.grid_core")
    args = dict(case["args"])
    args["bars"] = [mod.Bar("XAUUSD", "1m", b.timestamp, b.open, b.high, b.low, b.close, b.volume, b.provider) for b in _bars(mod)]
    return mod.simulate_conditional_grid(**args)


def _observed(case: dict[str, Any], services: bool) -> dict[str, Any]:
    try:
        return {"ok": True, "value": _jsonable(_invoke(case, services))}
    except Exception as exc:  # errors are part of the comparison contract
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}


def _number_pair(left: Any, right: Any) -> tuple[bool, float] | None:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)) and not isinstance(left, bool) and not isinstance(right, bool):
        delta = abs(float(left) - float(right))
        scale = max(abs(float(left)), abs(float(right)), 1.0)
        return delta <= FLOAT_ABS_TOLERANCE + FLOAT_REL_TOLERANCE * scale, delta
    return None


def _diff(left: Any, right: Any, path: str = "$") -> list[dict[str, Any]]:
    pair = _number_pair(left, right)
    if pair:
        if not pair[0]: return [{"path": path, "left": left, "right": right, "numeric_delta": pair[1]}]
        return []
    if type(left) is not type(right): return [{"path": path, "left": left, "right": right}]
    if isinstance(left, dict):
        diffs = []
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right: diffs.append({"path": f"{path}.{key}", "left": left.get(key), "right": right.get(key)})
            else: diffs.extend(_diff(left[key], right[key], f"{path}.{key}"))
        return diffs
    if isinstance(left, list):
        diffs = []
        for i in range(max(len(left), len(right))):
            if i >= len(left) or i >= len(right): diffs.append({"path": f"{path}[{i}]", "left": left[i] if i < len(left) else None, "right": right[i] if i < len(right) else None})
            else: diffs.extend(_diff(left[i], right[i], f"{path}[{i}]"))
        return diffs
    return [] if left == right else [{"path": path, "left": left, "right": right}]


def _classify(diff: dict[str, Any]) -> str:
    if "numeric_delta" in diff:
        return "precision"
    if isinstance(diff.get("left"), (dict, list)) or isinstance(diff.get("right"), (dict, list)) or diff["path"].endswith(".ok"):
        return "schema"
    return "semantic"


def _suggestion(kind: str) -> str:
    return {"semantic": "以已批准的 trading_strategy 语义为准，记录兼容性差异后再由 owner 决策", "precision": "以 venue precision/Decimal 规范化结果为准，不在本 story 修改实现", "schema": "以版本化合同字段为准，先确认 schema 兼容性再决定迁移"}[kind]


def build_report() -> dict[str, Any]:
    rows = []
    for case in fixtures():
        left, right = _observed(case, True), _observed(case, False)
        diffs = _diff(left, right)
        classified = [{**d, "classification": _classify(d), "recommendation": _suggestion(_classify(d))} for d in diffs]
        rows.append({"case_id": case["id"], "family": case["family"], "module": case["module"], "match": not bool(diffs), "differences": classified, "services": left, "trading_strategy": right})
    envelope = {"contract_version": CONTRACT_VERSION, "trading_strategy_sha": TRADING_STRATEGY_SHA, "cases": rows}
    digest = hashlib.sha256(json.dumps(_jsonable(envelope), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"contract_version": CONTRACT_VERSION, "trading_strategy_sha": TRADING_STRATEGY_SHA, "float_tolerance": {"absolute": FLOAT_ABS_TOLERANCE, "relative": FLOAT_REL_TOLERANCE}, "case_counts": {"total": len(rows), "dca": sum(r["family"] == "dca" for r in rows), "grid": sum(r["family"] == "grid" for r in rows)}, "envelope_digest": digest, "cases": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare trading_strategy against the internal strategy implementation")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    report = build_report()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"report": str(args.report), "envelope_digest": report["envelope_digest"], "case_counts": report["case_counts"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
