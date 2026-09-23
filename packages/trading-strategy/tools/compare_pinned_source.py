"""Compare extracted pure modules with the pinned source Git object."""

from __future__ import annotations

import subprocess
from pathlib import Path


SOURCE_ROOT = Path("/Users/wendy/work/trading-system-testnet")
SOURCE_REF = "b841800ee03fd98107063c0cbbf5144096a5c4c0"
MODULES = {
    "dca_plan.py": "services/dca_plan.py",
    "grid_sizing.py": "services/grid_sizing.py",
    "grid_core.py": "services/dualtrack_grid_core.py",
    "costs.py": "services/dualtrack_costs.py",
    "venue_costs.py": "services/venue_costs.py",
    "grid_marketability.py": "services/grid_marketability.py",
    "grid_range_adjustment.py": "services/grid_range_adjustment.py",
}
IMPORT_REPLACEMENTS = {
    "dca_plan.py": {
        "from .precision import": "from services.dualtrack_execution_contract import",
        "from .grid_sizing import": "from services.grid_sizing import",
    },
    "grid_sizing.py": {
        "from .precision import": "from services.dualtrack_execution_contract import",
        "from .grid_marketability import": "from services.grid_marketability import",
    },
    "grid_core.py": {
        "from .market import": "from schemas.market_data import",
        "from .costs import": "from services.dualtrack_costs import",
    },
    "costs.py": {
        "from .venue_costs import": "from services.venue_costs import",
    },
    "grid_range_adjustment.py": {
        "from .grid_sizing import": "from services.grid_sizing import",
    },
}


def source_text(relative: str) -> str:
    return subprocess.run(
        ["git", "-C", str(SOURCE_ROOT), "show", f"{SOURCE_REF}:{relative}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def main() -> None:
    for target_name, source_name in MODULES.items():
        expected = source_text(source_name)
        actual = (Path("trading_strategy") / target_name).read_text(encoding="utf-8")
        for old, new in IMPORT_REPLACEMENTS.get(target_name, {}).items():
            actual = actual.replace(old, new)
        if actual != expected:
            raise SystemExit(f"PINNED_COPY_COMPARISON_FAIL:{target_name}")
    print("PINNED_COPY_COMPARISON_PASS")
    print(f"modules_checked={len(MODULES)}")
    print(f"source_ref={SOURCE_REF}")


if __name__ == "__main__":
    main()
