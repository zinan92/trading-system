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
    kline_archive: Path = Path(os.getenv("TRADING_DESK_KLINE_ARCHIVE", str(Path.home() / "park-hands/007_kline daily newsletter")))
    morning_latest: Path = Path(os.getenv("TRADING_DESK_MORNING_LATEST", str(Path.home() / "work/park-ai-intel/public/daily/latest.html")))
    morning_archive: Path = Path(os.getenv("TRADING_DESK_MORNING_ARCHIVE", str(Path.home() / "park-hands/009_morning brief")))
    kline_latest_html: Path = Path(os.getenv("TRADING_DESK_KLINE_HTML", str(Path.home() / "Desktop/K线日报/latest.html")))
    weekly_latest_html: Path = Path(os.getenv("TRADING_DESK_WEEKLY_HTML", str(Path.home() / "Desktop/宏观K线周报/latest.html")))
    remote_passcode: Path = Path(os.getenv("TRADING_DESK_REMOTE_PASSCODE", str(Path.home() / "park-data/trading-desk/remote-passcode")))
    paused_manifest: Path = Path(os.getenv("TRADING_DESK_PAUSED", str(Path.home() / "park-data/trading-desk/paused-services.md")))
    # Execution (Hyperliquid Testnet only): the desk reuses the operator's proven preview -> confirm -> driver path.
    trading_system_checkout: Path = Path(os.getenv("TRADING_DESK_TS_CHECKOUT", str(Path.home() / "work/trading-system-park-paper-main")))
    standard_broker_src: Path = Path(os.getenv("TRADING_DESK_SB_SRC", str(Path.home() / "work/standard-broker/src")))
    nautilus_python: Path = Path(os.getenv("TRADING_DESK_NAUTILUS_PY", str(Path.home() / ".local/share/trading-orchestrator/nautilus-1.230.0/bin/python")))
    dashboard_plist: Path = Path(os.getenv("TRADING_DESK_DASHBOARD_PLIST", str(Path.home() / "Library/LaunchAgents/com.wendy.trading-orchestrator.dashboard.plist")))
    control_script: Path = Path(os.getenv("TRADING_DESK_CONTROL_SCRIPT", str(Path.home() / ".config/trading-system/run-park-paper-control.sh")))
