"""Mixin modules composing services.dashboard_state.DashboardState."""

from services.dashboard.market_view import MarketViewMixin
from services.dashboard.nav_quality import NavQualityMixin
from services.dashboard.replay import ReplayMixin
from services.dashboard.boards import BoardsMixin
from services.dashboard.frequency import FrequencyMixin
from services.dashboard.provenance import ProvenanceMixin
from services.dashboard.trade_lifecycle import TradeLifecycleMixin
from services.dashboard.vitals import VitalsMixin

__all__ = [
    "MarketViewMixin",
    "NavQualityMixin",
    "ReplayMixin",
    "BoardsMixin",
    "FrequencyMixin",
    "ProvenanceMixin",
    "TradeLifecycleMixin",
    "VitalsMixin",
]
