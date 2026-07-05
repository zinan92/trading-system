"""CLI entry for the R5-B oracle-bracket conditional grid experiment.

Separate from ``pipelines/lab_run.py`` on purpose: R4 work is in flight on
that module and R5 must not collide with it.

Usage:
    python -m pipelines.lab_r5
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.config_loader import load_pipeline_config  # noqa: E402
from services.lab_r5_grid import run_r5_grid  # noqa: E402
from services.lab_registry import LabRegistry  # noqa: E402
from services.lab_walkforward import HoldoutQuarantine, load_gold_1m_bars  # noqa: E402


def main() -> None:
    config = load_pipeline_config()
    market_db = Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))
    output_root = ROOT / "outputs"
    bars = load_gold_1m_bars(market_db)
    research = HoldoutQuarantine(bars).research_bars()
    report = run_r5_grid(output_root, LabRegistry(output_root), research)
    print(f"R5-B done: {report['trial_count']} trials, cycles={report['coverage_note']}")
    print(f"report: {output_root / 'lab' / 'reports' / 'R5_grid_oracle.md'}")


if __name__ == "__main__":
    main()
