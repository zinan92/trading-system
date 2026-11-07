"""Silver and crude grids on Paper (ADR 0009, #1407): OKX public data, no account.

Paper runs one instrument at a time. A module sees the running grid only when the Paper session runs its
instrument. Bars and the price for a new plan are OKX's own swap candles; a stale or missing feed gives no
price, so no plan and no preview. Fees are the Paper rates, not observed on OKX: shown 「手续费未核实」.
"""
from __future__ import annotations

from typing import Any, Mapping

from .base import VenueModule


class PaperOkxSwap(VenueModule):
    instrument: str = ""
    label: str = ""
    capabilities = frozenset({"live_price", "bars", "bars_health", "grid_plan", "preview", "execute"})
    bars_health_label = "OKX 公开行情（白银、原油 · datafeed 8100）"
    reading_note = "K 线日报看的是期货合约；这里的价格和 K 线是 OKX 永续，两者有价差。"

    def venue_text(self, sources: Any, asset: Mapping[str, Any]) -> tuple[str, str]:
        return f"OKX {asset['instrument_id']} · 纸面盘", "纸面模拟 · 手续费未核实"

    def asset_row(self, sources: Any, row: dict[str, Any]) -> dict[str, Any]:
        # GridMind's catalog names the Paper instrument id; gm-desk matches a Paper selection on it.
        paper = sources.paper_instrument(self.instrument)
        row["venue_profile_id"] = (paper or {}).get("venue_profile_id") or ""
        row["paper_instrument_id"] = (paper or {}).get("instrument_id") or ""
        return row

    def grid_and_price(self, sources: Any, asset: Mapping[str, Any]) -> tuple[dict[str, Any], float | None]:
        paper = sources.paper_grid(self.instrument)
        if not paper.get("ok"):
            return {"ok": False, "reason": paper.get("reason")}, None
        price = paper.get("price")
        if price is None and not paper["grid"].get("ok"):
            bars = sources.okx_bars(dict(asset), "1h", 2)  # fresh or nothing; never another instrument's mark
            price = bars["bars"][-1][4] if bars.get("ok") and bars.get("bars") else None
        return paper["grid"], price

    def bars(self, sources: Any, asset: Mapping[str, Any], timeframe: str, limit: int) -> dict[str, Any]:
        return sources.okx_bars(dict(asset), timeframe, limit)

    def preview(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
        return executor.preview_paper(plan, instrument=self.instrument, label=self.label)

    def execute(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any], *, shown_max_loss: float | None,
                judgment_id: int) -> dict[str, Any]:
        return executor.execute_paper(plan, instrument=self.instrument, label=self.label, shown_max_loss=shown_max_loss)


class PaperSilver(PaperOkxSwap):
    kind = "xag_paper"
    instrument = "silver"
    label = "白银"
    # About gold's dollars per rung (10 x 0.2 oz is ~$820): 15 oz of silver is ~$780. Silver moves more than gold,
    # so the range and the stop sit a little wider. Ticks are OKX's 0.01; 15 is on the 0.01 size grid.
    plan_template = {"rungs": 10, "units": 15, "size_unit": "盎司", "range_pct": 0.06, "stop_pct": 0.015, "unit": "USDT",
                     "beyond_price": 0.1, "tick": 0.01}
    started_text = "计划已确认，白银纸面盘约 1 分钟内挂单，系统接管盯盘。"


class PaperCrude(PaperOkxSwap):
    kind = "cl_paper"
    instrument = "crude"
    label = "原油"
    # 9 barrels is ~$810 a rung; 9 is on OKX's 0.1 size grid.
    plan_template = {"rungs": 10, "units": 9, "size_unit": "桶", "range_pct": 0.06, "stop_pct": 0.015, "unit": "USDT",
                     "beyond_price": 0.1, "tick": 0.01}
    started_text = "计划已确认，原油纸面盘约 1 分钟内挂单，系统接管盯盘。"
