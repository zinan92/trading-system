"""Local endpoints and paths. Every value can be overridden by environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# The desk lives inside the trading platform repo:
#   <repo>/apps/trading-desk/src/trading_desk/config.py -> <repo>
PLATFORM_ROOT = Path(__file__).resolve().parents[4]


def _env(name: str, default: str) -> str:
    """Empty or unset environment values fall back to the default."""
    return os.getenv(name) or default


def _default_paper_output() -> str:
    """Share the Dashboard's output root so a fresh clone needs one setting, not two."""
    return os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT") or str(PLATFORM_ROOT / "outputs")


@dataclass(frozen=True)
class Config:
    intel_url: str = _env("TRADING_DESK_INTEL_URL", "http://127.0.0.1:8001")
    dashboard_url: str = _env("TRADING_DESK_DASHBOARD_URL", "http://127.0.0.1:8765")
    kline_review_url: str = _env("TRADING_DESK_KLINE_REVIEW_URL", "http://127.0.0.1:8932")
    hyperliquid_info_url: str = _env("TRADING_DESK_HL_INFO_URL", "https://api.hyperliquid-testnet.xyz/info")
    hyperliquid_account: str = _env("TRADING_DESK_HL_ACCOUNT", "0x7b9d494f79217246b0C80576c26ceDdf2c315f04")
    paper_output: Path = Path(_env("TRADING_DESK_PAPER_OUTPUT", _default_paper_output()))
    db_path: Path = Path(_env("TRADING_DESK_DB", str(Path.home() / "park-data/trading-desk/desk.db")))
    port: int = int(_env("TRADING_DESK_PORT", "8790"))
    review_hours: int = int(_env("TRADING_DESK_REVIEW_HOURS", "72"))
    kline_archive: Path = Path(_env("TRADING_DESK_KLINE_ARCHIVE", str(Path.home() / "park-hands/007_kline daily newsletter")))
    morning_latest: Path = Path(_env("TRADING_DESK_MORNING_LATEST", str(Path.home() / "work/park-ai-intel/public/daily/latest.html")))
    morning_archive: Path = Path(_env("TRADING_DESK_MORNING_ARCHIVE", str(Path.home() / "park-hands/009_morning brief")))
    kline_latest_html: Path = Path(_env("TRADING_DESK_KLINE_HTML", str(Path.home() / "Desktop/K线日报/latest.html")))
    weekly_latest_html: Path = Path(_env("TRADING_DESK_WEEKLY_HTML", str(Path.home() / "Desktop/宏观K线周报/latest.html")))
    remote_passcode: Path = Path(_env("TRADING_DESK_REMOTE_PASSCODE", str(Path.home() / "park-data/trading-desk/remote-passcode")))
    watch_folder: Path = Path(_env("TRADING_DESK_WATCH", str(Path.home() / "park-data/trading-desk/watch")))
    paused_manifest: Path = Path(_env("TRADING_DESK_PAUSED", str(Path.home() / "park-data/trading-desk/paused-services.md")))
    # Execution (Hyperliquid Testnet only): the desk reuses the operator's proven preview -> confirm -> driver path.
    trading_system_checkout: Path = Path(_env("TRADING_DESK_TS_CHECKOUT", str(PLATFORM_ROOT)))
    standard_broker_src: Path = Path(_env("TRADING_DESK_SB_SRC", str(PLATFORM_ROOT / "packages" / "standard-broker" / "src")))
    nautilus_python: Path = Path(_env("TRADING_DESK_NAUTILUS_PY", str(Path.home() / ".local/share/trading-orchestrator/nautilus-1.230.0/bin/python")))
    dashboard_plist: Path = Path(_env("TRADING_DESK_DASHBOARD_PLIST", str(Path.home() / "Library/LaunchAgents/com.wendy.trading-orchestrator.dashboard.plist")))
    # Hyperliquid Mainnet (read-only dry run, trading-system#1277): Park types the key into the desk; it lives only in this 0600 file.
    mainnet_config: Path = Path(_env("TRADING_DESK_MAINNET_CONFIG", str(Path.home() / ".config/trading-system/hyperliquid-mainnet.json")))
    mainnet_key: Path = Path(_env("TRADING_DESK_MAINNET_KEY", str(Path.home() / ".config/trading-system/hyperliquid-mainnet.pk")))
    standard_broker_mainnet: Path = Path(_env("TRADING_DESK_SB_MAINNET", str(PLATFORM_ROOT)))
    control_script: Path = Path(_env("TRADING_DESK_CONTROL_SCRIPT", str(Path.home() / ".config/trading-system/run-park-paper-control.sh")))
