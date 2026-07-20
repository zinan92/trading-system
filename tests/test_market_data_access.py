from pathlib import Path

import pytest

from services.datafeed_market_repository import DatafeedMarketRepository
from services.market_data_access import (
    TrustedMarketDataReadPort,
    market_data_repository,
)
from services.market_store import MarketStore


def test_production_market_path_resolves_to_datafeed():
    repo = market_data_repository()
    assert isinstance(repo, DatafeedMarketRepository)
    assert isinstance(repo, TrustedMarketDataReadPort)


def test_temporary_test_path_remains_isolated_legacy_store(tmp_path: Path):
    repo = market_data_repository(tmp_path / "fixture.db")
    assert isinstance(repo, MarketStore)
    assert not isinstance(repo, TrustedMarketDataReadPort)


def test_non_temp_legacy_path_is_rejected():
    with pytest.raises(RuntimeError, match="restricted to temporary tests"):
        market_data_repository(Path("/Users/wendy/private-market.db"))
