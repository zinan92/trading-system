"""Hyperliquid adapter mapping errors."""

from ...errors import BrokerError


class UnsupportedProductError(BrokerError):
    """Raised when a fixture belongs to a deferred Hyperliquid product scope."""


class UnknownInstrumentError(BrokerError):
    """Raised when an account or fee fact references an unmapped instrument."""

    def __init__(self, broker_symbol: str) -> None:
        self.broker_symbol = broker_symbol
        super().__init__(f"unknown Hyperliquid instrument: {broker_symbol}")
