"""The Paper gold grid GridMind runs. Which Paper venue it is comes from GridMind's catalog, never from here."""
from __future__ import annotations

from typing import Any, Mapping

from .base import VenueModule


class PaperGold(VenueModule):
    kind = "xau_paper"
    capabilities = frozenset({"live_price", "bars", "grid_plan", "preview", "execute"})
    # Matches the running paper grid: 10 rungs x 0.2 oz, 200-point range, stop 50 beyond. The paper engine only
    # accepts a range that contains the price, so 10% of the width sits on the other side of it.
    plan_template = {"rungs": 10, "units": 0.2, "range_pct": 0.046, "stop_pct": 0.0116, "unit": "USDT", "beyond_price": 0.1}
    started_text = "计划已确认，黄金纸面盘约 1 分钟内挂单，系统接管盯盘。"

    def venue_text(self, sources: Any, asset: Mapping[str, Any]) -> tuple[str, str]:
        known = (sources.paper_gold() or {}).get("venue_label")
        return (f"{known} · 纸面盘" if known else "纸面盘（场所读不到）"), "纸面模拟"

    def asset_row(self, sources: Any, row: dict[str, Any]) -> dict[str, Any]:
        # The seeded ids are a placeholder; GridMind's catalog decides the venue.
        paper = sources.paper_gold()
        row["venue_profile_id"] = (paper or {}).get("venue_profile_id") or ""
        row["instrument_id"] = (paper or {}).get("instrument_id") or ""
        return row

    def grid_and_price(self, sources: Any, asset: Mapping[str, Any]) -> tuple[dict[str, Any], float | None]:
        paper = sources.xau_paper()
        if not paper.get("ok"):
            return {"ok": False, "reason": paper.get("reason")}, None
        return paper["grid"], paper.get("price")

    def bars(self, sources: Any, asset: Mapping[str, Any], timeframe: str, limit: int) -> dict[str, Any]:
        return sources.paper_gold_bars(timeframe, limit)

    def preview(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
        return executor.preview_gold(plan)

    def execute(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any], *, shown_max_loss: float | None,
                judgment_id: int) -> dict[str, Any]:
        return executor.execute_gold(plan, shown_max_loss=shown_max_loss)
