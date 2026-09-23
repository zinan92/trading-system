"""Print the source-baseline canonical golden fixture to stdout.

This is a provenance tool, not a runtime dependency of ``trading_strategy``.
It imports only the frozen source checkout named in the extraction contract and
prints deterministic JSON. The committed fixture is checked with ``diff``.
"""

from __future__ import annotations

import argparse
import json
import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


SOURCE_ROOT = Path("/Users/wendy/work/trading-system-testnet")
SOURCE_BASELINE_SHA = "b841800ee03fd98107063c0cbbf5144096a5c4c0"
SOURCE_FILES = (
    "services/dca_plan.py",
    "services/grid_sizing.py",
    "services/dualtrack_grid_core.py",
    "services/dualtrack_execution_contract.py",
    "services/dualtrack_costs.py",
    "services/venue_costs.py",
    "services/grid_marketability.py",
    "services/grid_range_adjustment.py",
    "schemas/market_data.py",
)
EXPECTED_SOURCE_FILE_SHA256 = {
    "schemas/market_data.py": "1ba833a4e1119a1f76921b21323f812f1b9ec6468d4a0ac4af5cf74d6b3af2cc",
    "services/dca_plan.py": "e326d5d4d1e2b69cfbc671fc272b04c268d41d7d7a2bcc5e51f0a1740625dc95",
    "services/dualtrack_costs.py": "8383d9da496dfa79b96191b36be4daf72af587664d7b9c37cfb6320ca0047493",
    "services/dualtrack_execution_contract.py": "01a55fad45c749da278eff25e677164f63a6fc9a82c22be7e1022fdfa6f5ae83",
    "services/dualtrack_grid_core.py": "d2b489fba0c43b4d0edd031c6f2a1f0fb5d4efcdf6b231b47857118d10a1b039",
    "services/grid_marketability.py": "e11c299d66b29d808706c0c66f0c5c4a0cdf39e4dda6361f2fa6a6a6282fe1e4",
    "services/grid_range_adjustment.py": "be2dddbe258a5dc3ec2ddac7ac5e4363d7e1dcbd07f362e225e5aa6b040adba0",
    "services/grid_sizing.py": "36394b4a80693d531734492eec333c683c64378cc689f463a051c744ad00cf81",
    "services/venue_costs.py": "f17c76c2d5523269b426489bab3700761d68c10ca01aa28e655540b4c9670776",
}


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(SOURCE_ROOT), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _source_head() -> str:
    return _git("rev-parse", "HEAD")


def _changed_relevant_files() -> list[str]:
    status = _git(
        "status",
        "--short",
        "--untracked-files=all",
        "--",
        *SOURCE_FILES,
    )
    return [line[3:] for line in status.splitlines() if line.strip()]


def validate_source_baseline(
    *,
    actual_head: str,
    changed_relevant_files: list[str],
) -> None:
    if actual_head != SOURCE_BASELINE_SHA:
        raise RuntimeError(
            "source HEAD mismatch: "
            f"expected {SOURCE_BASELINE_SHA}, got {actual_head}"
        )
    if changed_relevant_files:
        raise RuntimeError(
            "relevant source files are dirty: "
            + ", ".join(sorted(changed_relevant_files))
        )


def validate_source_file_hashes(actual_hashes: dict[str, str]) -> None:
    if actual_hashes != EXPECTED_SOURCE_FILE_SHA256:
        changed = sorted(
            relative
            for relative in set(actual_hashes) | set(EXPECTED_SOURCE_FILE_SHA256)
            if actual_hashes.get(relative) != EXPECTED_SOURCE_FILE_SHA256.get(relative)
        )
        raise RuntimeError(
            "pinned source file hash mismatch: " + ", ".join(changed)
        )


def verify_source_baseline() -> dict[str, object]:
    actual_head = _source_head()
    changed_relevant_files = _changed_relevant_files()
    validate_source_baseline(
        actual_head=actual_head,
        changed_relevant_files=changed_relevant_files,
    )
    source_file_sha256 = source_file_hashes(SOURCE_ROOT)
    validate_source_file_hashes(source_file_sha256)
    return {
        "source_baseline_sha": actual_head,
        "changed_relevant_files": changed_relevant_files,
        "source_file_sha256": source_file_sha256,
    }


def source_file_hashes(root: Path) -> dict[str, str]:
    return {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in SOURCE_FILES
    }


def _load_source_modules(root: Path) -> None:
    global Bar
    global GridLineLifecycle
    global GridStop
    global build_dca_entry_commands
    global build_dca_preview
    global build_dca_strategy_plan
    global build_deterministic_dca_candidate_payload_v1
    global build_grid_preview
    global replay_dca_marks
    global simulate_conditional_grid
    global simulate_explicit_grid

    sys.path.insert(0, str(root))
    from services.dca_plan import (
        build_dca_entry_commands,
        build_dca_preview,
        build_dca_strategy_plan,
        build_deterministic_dca_candidate_payload_v1,
        replay_dca_marks,
    )
    from services.dualtrack_grid_core import (
        Bar,
        GridLineLifecycle,
        GridStop,
        simulate_conditional_grid,
        simulate_explicit_grid,
    )
    from services.grid_sizing import (
        build_adaptive_grid_preview,
        build_grid_preview,
    )
    globals()["build_adaptive_grid_preview"] = build_adaptive_grid_preview


@contextmanager
def _source_snapshot(source_ref: str):
    archive = subprocess.run(
        ["git", "-C", str(SOURCE_ROOT), "archive", source_ref],
        check=True,
        capture_output=True,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="trading-strategy-source-") as root:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            bundle.extractall(root, filter="data")
        yield Path(root)


DCA_CONFIG = {
    "capital_per_track_usd": 10000,
    "max_leverage": 10,
    "cost_per_side_bp": 0.5,
    "execution_contract": {"price_increment": "0.01", "quantity_increment": "0.001"},
}


def dca_market(price: float = 4010.0) -> dict:
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "fixture-provider",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": price,
        "latest_timestamp": "2026-07-22T00:19:00+00:00",
        "bars": [
            {
                "timestamp": f"2026-07-22T00:{index:02d}:00+00:00",
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
            }
            for index in range(20)
        ],
    }


def dca_payload() -> dict:
    return {
        "direction": "long",
        "dca": {
            "entry_levels": [4004.0, 3996.0, 3988.0],
            "target_price": 4050.0,
            "stop_price": 3970.0,
            "notional_per_addition": 2000.0,
            "max_additions": 3,
            "loop_enabled": False,
        },
        "risk_budget": {"leverage": 10},
    }


def grid_bar(index: int, close: float, previous: float) -> Bar:
    timestamp = (
        datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc)
        + timedelta(minutes=index)
    ).isoformat()
    return Bar(
        symbol="GOLD",
        timeframe="1m",
        timestamp=timestamp,
        open=previous,
        high=max(previous, close),
        low=min(previous, close),
        close=close,
        volume=1,
        provider="fixture",
    )


def grid_bars(closes: list[float]) -> list[Bar]:
    rows: list[Bar] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        rows.append(grid_bar(index, close, previous))
        previous = close
    return rows


GRID_CONFIG = {
    "capital_per_track_usd": 10000,
    "max_leverage": 10,
    "cost_per_side_bp": 0.5,
    "execution_contract": {
        "price_increment": "0.01",
        "quantity_increment": "0.001",
    },
    "strategy_grid": {
        "range_timeframe": "1d",
        "range_atr_period": 14,
        "spacing_timeframe": "4h",
        "spacing_atr_period": 14,
        "execution_timeframe": "1m",
        "min_grid_count": 2,
        "max_grid_count": 4,
        "cost_spacing_multiple": 5.0,
        "default_mode": "arithmetic",
        "capital_utilization_cap": 1.0,
        "required_leverage": 10.0,
        "min_net_profit_per_grid_usd": 1.0,
        "styles": {
            "steady": {
                "range_atr_multiple": 2.0,
                "spacing_atr_multiple": 0.25,
            },
            "aggressive": {
                "range_atr_multiple": 1.0,
                "spacing_atr_multiple": 0.125,
            },
        },
    },
}


def grid_market(close: float = 110.0) -> dict:
    def context(span: float) -> list[dict]:
        return [
            {
                "timestamp": f"2026-06-{index + 1:02d}T00:00:00+00:00",
                "open": close - 1 + index * 0.05 - 0.1,
                "high": close - 1 + index * 0.05 + span / 2,
                "low": close - 1 + index * 0.05 - span / 2,
                "close": close - 1 + index * 0.05,
            }
            for index in range(20)
        ]

    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "fixture",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": close,
        "latest_timestamp": "2026-07-05T01:39:00+00:00",
        "bars": context(1.0),
        "strategy_timeframes": {
            "1d": {
                "provider": "fixture",
                "is_synthetic": False,
                "bars": context(10.0),
            },
            "4h": {
                "provider": "fixture",
                "is_synthetic": False,
                "bars": context(4.0),
            },
        },
    }


def grid_preview_summary(preview: dict) -> dict:
    return {
        "schema_version": preview["schema_version"],
        "cycle_id": preview["cycle_id"],
        "preview_id": preview["preview_id"],
        "direction": preview["direction"],
        "style": preview["style"],
        "range": preview["range"],
        "grid": preview["grid"],
        "orders": preview["orders"],
        "risk": preview["risk"],
    }


def _capture(source_receipt: dict[str, object]) -> dict:
    preview = build_dca_preview(
        "golden-dca",
        dca_payload(),
        market=dca_market(),
        account={"equity": 10000},
        config=DCA_CONFIG,
    )
    plan = build_dca_strategy_plan(
        preview,
        strategy_plan_id="strategy-plan-dca-golden",
        version=1,
        locked_at="2026-07-22T16:00:00+00:00",
    )
    commands = build_dca_entry_commands(
        plan,
        timestamp="2026-07-22T16:00:00+00:00",
    )
    explicit = simulate_explicit_grid(
        cycle_id="golden-grid",
        bars=grid_bars([4000.0, 4010.0, 4000.0, 4010.0]),
        direction=1,
        orders=[{"entry": 4000.0, "take_profit": 4010.0, "weight": 1.0}],
        stop=GridStop(side="below", price=3990.0),
        rung_notional=1000.0,
        cost_per_side_bp=0.5,
        finalize=False,
    )
    hard_stop = simulate_explicit_grid(
        cycle_id="golden-grid-stop",
        bars=grid_bars([4000.0, 3990.0, 3980.0]),
        direction=1,
        orders=[{"entry": 4000.0, "take_profit": 4010.0, "weight": 1.0}],
        stop=GridStop(side="below", price=3990.0),
        rung_notional=1000.0,
        cost_per_side_bp=0.5,
        finalize=False,
    )
    line = GridLineLifecycle(
        line_id="golden-line",
        armed_at="2026-07-05T01:00:00+00:00",
        requested_quantity=10.0,
    )
    line.apply_entry_fill(
        fill_id="entry-partial",
        quantity=4.0,
        at="2026-07-05T01:01:00+00:00",
    )
    line.apply_close_fill(
        fill_id="close-partial",
        quantity=4.0,
        at="2026-07-05T01:02:00+00:00",
        rearm=True,
    )
    line.confirm_entry_cancelled(
        at="2026-07-05T01:03:00+00:00",
        reason="cancel remainder",
    )
    common = {
        "direction": 1,
        "spacing_bp": 20.0,
        "range_k": 1.0,
        "rung_notional": 1000.0,
        "max_rungs": 10,
        "cost_per_side_bp": 0.5,
    }
    same_bar = simulate_conditional_grid(
        cycle_id="golden-conditional-hard-stop",
        bars=grid_bars([4000.0, 3940.0]),
        prev_range=100.0,
        re_arm_max=1,
        stop=GridStop(side="below", price=3950.0),
        **common,
    )
    synthetic_rearm = simulate_conditional_grid(
        cycle_id="golden-conditional-rearm",
        bars=grid_bars([4000.0, 3920.0, 4000.0]),
        prev_range=80.0,
        re_arm_max=1,
        stop=None,
        **common,
    )
    terminal_flatten = simulate_conditional_grid(
        cycle_id="golden-conditional-flatten",
        bars=grid_bars([4000.0, 3990.0]),
        prev_range=80.0,
        re_arm_max=0,
        stop=None,
        **common,
    )
    grid_preview = build_grid_preview(
        "golden-grid-preview",
        {
            "direction": "neutral",
            "style": "steady",
            "grid": {
                "count": 4,
                "mode": "arithmetic",
                "notional_per_grid": 1000.0,
                "notional_mode": "manual",
            },
        },
        market=grid_market(),
        account={"equity": 10000},
        config=GRID_CONFIG,
        allow_unsafe_manual_preview=True,
    )
    output = {
        "dca": {
            "preview": preview,
            "plan": plan,
            "commands": commands,
            "target_replay": replay_dca_marks(
                preview,
                [4004.0, 3996.0, 4050.0, 3970.0],
            ),
            "stop_replay": replay_dca_marks(preview, [4004.0, 3970.0]),
        },
        "grid": {
            "preview": grid_preview_summary(grid_preview),
            "explicit_rearm": asdict(explicit),
            "hard_stop": asdict(hard_stop),
            "line_after_partial_close_cancel": line.snapshot(),
        },
        "conditional_grid": {
            "same_bar_hard_stop": asdict(same_bar),
            "synthetic_range_rearm": asdict(synthetic_rearm),
            "terminal_flatten": asdict(terminal_flatten),
        },
        "source_baseline_sha": source_receipt["source_baseline_sha"],
        "source_file_sha256": source_receipt["source_file_sha256"],
    }
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-ref",
        help=(
            "Read the pinned source from a Git object snapshot. Without this "
            "flag, the current source checkout HEAD must equal the pinned SHA."
        ),
    )
    args = parser.parse_args()
    if args.source_ref:
        validate_source_baseline(
            actual_head=str(args.source_ref),
            changed_relevant_files=[],
        )
        with _source_snapshot(str(args.source_ref)) as root:
            source_file_sha256 = source_file_hashes(root)
            validate_source_file_hashes(source_file_sha256)
            _load_source_modules(root)
            receipt = {
                "source_baseline_sha": str(args.source_ref),
                "changed_relevant_files": [],
                "source_file_sha256": source_file_sha256,
            }
            print(json.dumps(_capture(receipt), sort_keys=True, indent=2))
        return

    receipt = verify_source_baseline()
    _load_source_modules(SOURCE_ROOT)
    print(json.dumps(_capture(receipt), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
