"""Assets read from OKX public perpetual-swap data through the datafeed (silver, crude; #1387). Read-only.

No account and no order path: plans stay record-only until a Paper venue for these swaps exists (#1361).
The bars are OKX's own (datafeed source ``okx_swap_public``); a stale or missing feed is ``ok=false``,
never another source's candles.
"""
from __future__ import annotations

from typing import Any, Mapping

from .base import VenueModule


class OkxPublic(VenueModule):
    kind = "okx_public"
    capabilities = frozenset({"bars", "bars_health"})
    bars_health_label = "OKX 公开行情（白银、原油 · datafeed 8100）"
    reading_note = "K 线日报看的是期货合约；这里的价格和 K 线是 OKX 永续，两者有价差。"

    def venue_text(self, sources: Any, asset: Mapping[str, Any]) -> tuple[str, str]:
        return f"OKX {asset['instrument_id']} 公开行情", "只记判断"

    def grid_and_price(self, sources: Any, asset: Mapping[str, Any]) -> tuple[dict[str, Any], float | None]:
        return {"ok": False, "none": True, "reason": f"{asset['label']}只有 OKX 公开行情，还没有纸面盘，只记判断和复盘。"}, None

    def bars(self, sources: Any, asset: Mapping[str, Any], timeframe: str, limit: int) -> dict[str, Any]:
        return sources.okx_bars(asset, timeframe, limit)
