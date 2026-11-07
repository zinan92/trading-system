"""Assets Park judges and reviews with no venue at all: futures overview bars only (silver and crude until #1387)."""
from __future__ import annotations

from typing import Any, Mapping

from .base import VenueModule


class WatchOnly(VenueModule):
    kind = "watch"
    capabilities = frozenset({"bars", "futures_overview"})

    def venue_text(self, sources: Any, asset: Mapping[str, Any]) -> tuple[str, str]:
        return "期货行情", "只记判断"

    def grid_and_price(self, sources: Any, asset: Mapping[str, Any]) -> tuple[dict[str, Any], float | None]:
        return {"ok": False, "none": True, "reason": f"{asset['label']}只记判断和复盘，还没有可以下单的交易所。"}, None

    def bars(self, sources: Any, asset: Mapping[str, Any], timeframe: str, limit: int) -> dict[str, Any]:
        return sources.overview_bars(asset, timeframe, limit)
