"""Shared in-memory identity ledger for one local Broker runtime composition."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from .account import AccountSnapshot, LiquidationFact
from .fees import FillFact, FundingPayment
from .orders import OrderFill


@dataclass
class RuntimeFactLedger:
    """Keep lifecycle observations separate from enriched accounting facts."""

    fills: dict[str, FillFact] = field(default_factory=dict)
    funding: dict[str, FundingPayment] = field(default_factory=dict)
    order_fills: dict[str, OrderFill] = field(default_factory=dict)
    order_fill_raw: dict[str, Mapping[str, object]] = field(default_factory=dict)
    accounts: dict[str, AccountSnapshot] = field(default_factory=dict)
    liquidations: dict[str, LiquidationFact] = field(default_factory=dict)
    session_key: str | None = None
    fill_enricher: Callable[[OrderFill, Mapping[str, object]], FillFact | None] | None = None

    def bind_session(self, *, broker_id: str, environment: str, account_address: str) -> None:
        """Bind the ledger to one immutable Broker runtime identity."""

        key = ":".join((broker_id, environment, account_address))
        if self.session_key is None:
            self.session_key = key
            return
        if self.session_key != key:
            raise ValueError("runtime_fact_ledger_session_mismatch")

    def register_fill_enricher(
        self,
        enricher: Callable[[OrderFill, Mapping[str, object]], FillFact | None],
    ) -> None:
        """Register the one FeePort enrichment path for this runtime composition."""

        if self.fill_enricher is not None and self.fill_enricher is not enricher:
            raise ValueError("runtime_fact_ledger_multiple_fill_enrichers")
        self.fill_enricher = enricher
        for key, fill in tuple(self.order_fills.items()):
            raw = self.order_fill_raw.get(key)
            if raw is not None:
                enricher(fill, raw)

    def record_order_fill(self, fill: OrderFill, raw: Mapping[str, object]) -> FillFact | None:
        """Stage an OrderFill and enrich it when fee evidence is available."""

        if self.session_key is None:
            raise ValueError("runtime_fact_ledger_unbound")
        fill_key = f"{self.session_key}:{fill.fill_id}"
        self.order_fills[fill_key] = fill
        self.order_fill_raw[fill_key] = dict(raw)
        if self.fill_enricher is None:
            return None
        return self.fill_enricher(fill, raw)
