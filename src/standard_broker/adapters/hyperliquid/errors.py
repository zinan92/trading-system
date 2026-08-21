"""Hyperliquid adapter mapping errors."""

from ...errors import BrokerError


class UnsupportedProductError(BrokerError):
    """Raised when a fixture belongs to a deferred Hyperliquid product scope."""
