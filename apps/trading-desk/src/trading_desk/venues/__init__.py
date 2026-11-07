"""Desk Venue Modules (ADR 0007): one module per desk kind, in a frozen, code-defined registry.

Business code asks the registry for an asset's module and checks a declared capability; it never
tests a kind's name. A module holds desk-side behaviour only (labels, plan template, how price,
grid and bars are read, which flows apply). Venue identity keeps coming from the Dashboard catalog.
A capability switches a desk flow on; it never authorizes an order.
"""
from __future__ import annotations

from .base import CAPABILITIES, Registry, VenueModule, build
from .hyperliquid_testnet import HyperliquidTestnet
from .okx_public import OkxPublic
from .paper_gold import PaperGold
from .watch_only import WatchOnly

DEFAULT = build(HyperliquidTestnet(), PaperGold(), WatchOnly(), OkxPublic())

__all__ = ["CAPABILITIES", "DEFAULT", "Registry", "VenueModule", "build"]
