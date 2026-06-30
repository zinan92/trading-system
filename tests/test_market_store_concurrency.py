import sqlite3
from pathlib import Path

from schemas.market_data import Bar
from services.market_store import MarketStore


def _bar(ts: str) -> Bar:
    return Bar("GOLD", "5m", ts, 1, 1, 1, 1, 1, "binance_usdm", [])


def test_marketstore_enables_wal_and_busy_timeout(tmp_path: Path):
    db = tmp_path / "m.db"
    store = MarketStore(db)
    store.upsert_bars([_bar("2026-05-31T00:00:00+00:00")])  # triggers a connect

    # WAL persists on the DB file once set — protects concurrent readers/writer.
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    con.close()

    # Each store connection waits on a lock instead of erroring immediately.
    c = store._connect()
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    c.close()


def test_marketstore_concurrent_writers_do_not_error(tmp_path: Path):
    # Two MarketStore instances writing the same DB interleaved must not raise
    # "database is locked" — the dual-writer (runner + strategies) cadence.
    db = tmp_path / "m.db"
    a = MarketStore(db)
    b = MarketStore(db)
    for i in range(25):
        a.upsert_bars([_bar(f"2026-05-31T00:{i:02d}:00+00:00")])
        b.upsert_quote(_bar(f"2026-05-31T01:{i:02d}:00+00:00"))
    assert len(a.load_bars("GOLD", "5m", 50)) == 25
