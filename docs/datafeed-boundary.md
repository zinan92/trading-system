# Market-data boundary

`/Users/wendy/datafeed` is the sole production owner of market-data upstreams,
normalization, source identity, quality, storage, refresh, and market sessions.

Trading code consumes `DatafeedMarketClient` or `DatafeedMarketRepository` only.
Execution adapters remain independent and may contact broker order/account APIs,
but they must not become candle providers.

Legacy SQLite/feed classes are retained only for temporary tests and one-time
migration rehearsals. `market_data_repository()` rejects non-temporary legacy
paths, and `tests/test_market_data_boundary.py` freezes the remaining seams so
new runtime bypasses fail CI.
