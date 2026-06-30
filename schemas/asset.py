from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    symbol: str
    name: str
    asset_class: str
    priority: str
    timezone: str
    role: str = "tradable"
    trade_symbol: str = ""
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "Asset":
        return cls(
            symbol=str(data["symbol"]),
            name=str(data.get("name", data["symbol"])),
            asset_class=str(data["asset_class"]),
            priority=str(data.get("priority", "medium")),
            timezone=str(data.get("timezone", "UTC")),
            role=str(data.get("role", "tradable")),
            trade_symbol=str(data.get("trade_symbol", data.get("symbol", ""))),
            enabled=bool(data.get("enabled", True)),
        )

    @property
    def tradable(self) -> bool:
        return self.role == "tradable"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "asset_class": self.asset_class,
            "priority": self.priority,
            "timezone": self.timezone,
            "role": self.role,
            "trade_symbol": self.trade_symbol,
            "enabled": self.enabled,
        }
