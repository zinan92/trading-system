"""#1379: the venue registry decides which asset kinds exist, in code; the database no longer lists them.

`fixtures/desk_schema_2026-11-07.sql` is the production desk database's schema only (`.schema`, read-only), no rows.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trading_desk.store import Store
from trading_desk.venues import DEFAULT

SCHEMA = Path(__file__).with_name("fixtures") / "desk_schema_2026-11-07.sql"
ROWS = [("BTC", "BTC", "hl_testnet", 1), ("XAU", "黄金", "xau_paper", 2), ("ETH", "ETH", "hl_testnet", 3), ("SILVER", "白银", "watch", 4), ("DOGE", "DOGE", "hl_testnet", 9)]


def _table_sql(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return conn.execute("select sql from sqlite_master where type='table' and name='assets'").fetchone()[0]


def _assets(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute("select key, label, kind, position, news_queries from assets order by key").fetchall()


@pytest.fixture
def production_shaped(tmp_path: Path) -> Path:
    path = tmp_path / "desk.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        for key, label, kind, position in ROWS:
            conn.execute("insert into assets (key,label,kind,venue_profile_id,instrument_id,coin,news_queries,primary_words,kline_key,position,created_at)"
                         " values (?,?,?,?,?,?,?,?,?,?,?)", (key, label, kind, "v", f"{key}-I", key, f'["{key}"]', "[]", None, position, "2026-09-15T00:00:00+00:00"))
        conn.execute("insert into meta (key, value) values ('v4_assets', 'x')")
    return path


def test_the_production_schema_still_lists_kinds(production_shaped: Path) -> None:
    assert "check (kind in" in _table_sql(production_shaped)


def test_opening_drops_the_kind_list_and_keeps_every_row(production_shaped: Path) -> None:
    before = _assets(production_shaped)
    Store(production_shaped)
    assert "check (kind in" not in _table_sql(production_shaped)
    assert _assets(production_shaped) == before
    with sqlite3.connect(production_shaped) as conn:
        assert [row[0] for row in conn.execute("select name from sqlite_master where type='table' and name like 'assets%'")] == ["assets"]


def test_opening_twice_does_nothing_the_second_time(production_shaped: Path) -> None:
    Store(production_shaped)
    once = _table_sql(production_shaped)
    Store(production_shaped)
    assert _table_sql(production_shaped) == once


def test_a_database_from_before_watch_assets_migrates_too(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = SCHEMA.read_text(encoding="utf-8").replace("('hl_testnet','xau_paper','watch')", "('hl_testnet','xau_paper')")
    with sqlite3.connect(path) as conn:
        conn.executescript(old)
        conn.execute("insert into assets (key,label,kind,venue_profile_id,instrument_id,coin,created_at) values ('BTC','BTC','hl_testnet','v','i','BTC','t')")
    store = Store(path)
    assert "check (kind in" not in _table_sql(path)
    assert store.asset("BTC")["kind"] == "hl_testnet" and store.asset("SILVER")["kind"] == "xag_paper"  # the v4 seed still lands, on OKX since #1387, Paper since #1407


def test_a_kind_the_registry_does_not_know_is_refused_in_code(tmp_path: Path) -> None:
    store = Store(tmp_path / "d.db")
    with pytest.raises(ValueError, match="unknown_kind"):
        store.add_asset({"key": "X", "kind": "nope"}, kinds=DEFAULT.kinds())
    added = store.add_asset({"key": "x", "kind": "fake_venue", "label": "假交易所"}, kinds=(*DEFAULT.kinds(), "fake_venue"))
    assert added["key"] == "X" and added["kind"] == "fake_venue" and added["position"] == 6
    with pytest.raises(ValueError, match="asset_exists"):
        store.add_asset({"key": "X", "kind": "fake_venue"}, kinds=("fake_venue",))
