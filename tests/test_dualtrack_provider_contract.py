from services.config_loader import load_pipeline_config
from services.dualtrack_config import dualtrack_config


def test_production_dualtrack_provider_matches_datafeed_gold_route() -> None:
    pipeline = load_pipeline_config()
    route = pipeline["datafeed"]["instrument_routes"]["GOLD"]

    assert dualtrack_config()["market_data"]["provider"] == route["source"]
