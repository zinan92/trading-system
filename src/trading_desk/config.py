"""Local endpoints and paths. Every value can be overridden by environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    intel_url: str = os.getenv("TRADING_DESK_INTEL_URL", "http://127.0.0.1:8001")
    dashboard_url: str = os.getenv("TRADING_DESK_DASHBOARD_URL", "http://127.0.0.1:8765")
    hyperliquid_info_url: str = os.getenv("TRADING_DESK_HL_INFO_URL", "https://api.hyperliquid-testnet.xyz/info")
    hyperliquid_account: str = os.getenv("TRADING_DESK_HL_ACCOUNT", "0x7b9d494f79217246b0C80576c26ceDdf2c315f04")
    paper_output: Path = Path(os.getenv("TRADING_DESK_PAPER_OUTPUT", str(Path.home() / "work/park-paper-output")))
    db_path: Path = Path(os.getenv("TRADING_DESK_DB", str(Path.home() / "park-data/trading-desk/desk.db")))
    port: int = int(os.getenv("TRADING_DESK_PORT", "8790"))
    review_hours: int = int(os.getenv("TRADING_DESK_REVIEW_HOURS", "72"))
