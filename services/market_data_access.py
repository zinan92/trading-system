"""Single construction point for market-data reads.

Production paths resolve to the independent datafeed. Temporary databases
remain available only for isolated tests and migration rehearsals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from schemas.market_data import MarketDataEnvelope
from services.config_loader import ROOT, load_pipeline_config
from services.datafeed_market_repository import DatafeedMarketRepository
from services.market_store import MarketStore


class MarketDataReadPort(Protocol):
    def load_bars(self, symbol: str, timeframe: str, limit: int): ...
    def load_bars_between(self, symbol: str, timeframe: str, start_timestamp: str, end_timestamp: str): ...
    def load_latest_bar(self, symbol: str, timeframe: str, providers: list[str] | None = None): ...
    def load_latest_quote(self, symbol: str): ...
    def load_bar_at_or_before(self, symbol: str, timeframe: str, timestamp: str): ...
    def coverage(self): ...


@runtime_checkable
class TrustedMarketDataReadPort(Protocol):
    """Opt-in port for versioned market-data trust envelopes."""

    def load_envelope(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> MarketDataEnvelope: ...


def market_data_repository(market_db: Path | str | None = None) -> MarketDataReadPort:
    config = load_pipeline_config()
    datafeed = config.get("datafeed", {}) or {}
    if bool(datafeed.get("enabled", False)) and _is_production_market_path(market_db, config):
        return DatafeedMarketRepository(config=config)
    if market_db is None:
        raise RuntimeError("Temporary legacy market store requires an explicit path")
    path = Path(market_db)
    if bool(datafeed.get("enabled", False)) and not _is_temporary_market_path(path):
        raise RuntimeError(
            "Legacy market stores are restricted to temporary tests; production reads must use datafeed"
        )
    return MarketStore(path)


def uses_independent_datafeed(market_db: Path | str | None = None) -> bool:
    """Return whether this call resolves to the production datafeed port."""
    config = load_pipeline_config()
    return bool((config.get("datafeed", {}) or {}).get("enabled", False)) and _is_production_market_path(
        market_db, config
    )


def _is_production_market_path(market_db: Path | str | None, config: dict) -> bool:
    if market_db is None:
        return True
    raw = str(market_db)
    if raw == ":memory:":
        return False
    configured = (ROOT / str(config.get("local_market_db", "data/market_data.db"))).expanduser()
    candidate = Path(raw).expanduser()
    # Test and migration callers always pass an explicit sandbox path. An env
    # override outside the repository is likewise treated as an explicit
    # compatibility seam, never as the production read path.
    try:
        return candidate.resolve() == configured.resolve()
    except OSError:
        return candidate.absolute() == configured.absolute()


def _is_temporary_market_path(path: Path) -> bool:
    if str(path) == ":memory:":
        return True
    resolved = path.expanduser().absolute()
    return any(
        str(resolved).startswith(prefix)
        for prefix in ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")
    )
