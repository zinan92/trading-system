from services.config_loader import load_pipeline_config
from services.dualtrack_config import dualtrack_config


def test_production_dualtrack_provider_matches_datafeed_gold_route() -> None:
    pipeline = load_pipeline_config()
    route = pipeline["datafeed"]["instrument_routes"]["GOLD"]

    assert dualtrack_config()["market_data"]["provider"] == route["source"]
    assert route["source"] == "binance_usdm_futures"
    assert route["require_execution_venue"] is True
    assert route["historical_cache_policy"] == "allow"
    assert route["historical_quality_policy"] == "standard"
    assert route["live_cache_policy"] == "bypass"
    assert route["live_quality_policy"] == "strict"
