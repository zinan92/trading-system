"""System-state and market-view intake response builders for the dashboard API."""

from __future__ import annotations

import os

from datetime import datetime
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.market_view_intake import MarketViewIntake
from services.run_date import utc_run_date
from services.system_state import build_system_state
from services.contracts.common import _DATE_PATTERN, _truthy


def dashboard_output_root() -> Path:
    cfg = load_pipeline_config()
    return ROOT / str(cfg.get("output_root", "outputs"))


def build_system_state_response(output_root: Path | None = None, *, as_of: str | datetime | None = None) -> dict:
    return build_system_state(output_root=output_root, as_of=as_of)


def build_market_view_intake_response(payload: dict, *, output_root: Path | None = None) -> dict:
    run_date = str(payload.get("date") or payload.get("run_date") or utc_run_date())
    if not _DATE_PATTERN.match(run_date):
        raise ValueError("expected date as YYYY-MM-DD")
    raw_text = str(payload.get("raw_text") or payload.get("text") or "").strip()
    if not raw_text:
        raise ValueError("raw_text is required")
    if len(raw_text) > 20_000:
        raise ValueError("raw_text exceeds 20000 characters")
    output = Path(output_root) if output_root else dashboard_output_root()
    draft_only = _truthy(payload.get("draft_only", False))
    write_obsidian = _truthy(payload.get("write_obsidian", False))
    obsidian_env = os.environ.get("TRADING_ORCHESTRATOR_OBSIDIAN_ROOT", "").strip()
    obsidian_root = Path(obsidian_env) if obsidian_env else None
    if write_obsidian and obsidian_root is None:
        raise ValueError("TRADING_ORCHESTRATOR_OBSIDIAN_ROOT is required when write_obsidian=true")
    intake = MarketViewIntake(output, obsidian_root=obsidian_root)
    if draft_only:
        draft = intake.draft(run_date, raw_text)
        return {
            "status": "draft",
            "draft_only": True,
            "run_date": draft.run_date,
            "draft": {
                "score": draft.score,
                "summary": draft.summary,
                "key_levels": draft.key_levels,
                "timeframes": draft.timeframes,
                "trade_plan": draft.trade_plan,
                "valid_for_hours": draft.valid_for_hours,
                "expires_at": draft.expires_at,
                "expires_if_price_moves_pct": draft.expires_if_price_moves_pct,
                "expiry_target_price": draft.expiry_target_price,
                "expire_above": draft.expire_above,
                "expire_below": draft.expire_below,
            },
        }
    market_view = intake.record(run_date, raw_text, write_obsidian=write_obsidian)
    return {
        "status": "recorded",
        "draft_only": False,
        "run_date": run_date,
        "market_view": market_view,
        "source_artifacts": {
            "json": str(output / "market_views" / f"{run_date}.json"),
            "current": str(output / "market_views" / "current.json"),
            "markdown": str(output / "market_views" / f"{run_date}.md"),
        },
    }
