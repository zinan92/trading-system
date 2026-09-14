"""The only data the desk owns: Park's judgments and notes."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
create table if not exists judgments (
  id integer primary key autoincrement,
  created_at text not null,
  asset text not null,
  direction text not null check (direction in ('long','short','flat')),
  confidence integer not null check (confidence between 1 and 5),
  reason text not null default '',
  cited text not null default '[]',
  price_at real,
  plan text,
  action text not null check (action in ('recorded','approved')),
  resolved_at text,
  price_after real,
  move_pct real,
  outcome text
);
create table if not exists assets (
  key text primary key,
  label text not null,
  kind text not null check (kind in ('hl_testnet','xau_paper')),
  venue_profile_id text not null,
  instrument_id text not null,
  coin text not null,
  news_queries text not null default '[]',
  primary_words text not null default '[]',
  kline_key text,
  position integer not null default 100,
  created_at text not null
);
create table if not exists executions (
  id integer primary key autoincrement,
  created_at text not null,
  judgment_id integer,
  asset text not null,
  stage text not null,
  preview_digest text,
  detail text not null default '{}'
);
create table if not exists notes (
  id integer primary key autoincrement,
  created_at text not null,
  asset text not null,
  body text not null
);
"""


DEFAULT_ASSETS = [
    {"key": "BTC", "label": "BTC", "kind": "hl_testnet", "venue_profile_id": "hyperliquid.testnet", "instrument_id": "BTC-USD-PERP", "coin": "BTC",
     "news_queries": ["比特币", "BTC", "加密", "以太坊", "美联储", "ETF", "稳定币", "美元"],
     "primary": ["比特币", "BTC", "加密", "以太坊", "ETH", "稳定币", "币", "链", "Hyperliquid", "巨鲸"], "kline_key": "bitcoin", "position": 1},
    {"key": "XAU", "label": "黄金", "kind": "xau_paper", "venue_profile_id": "binance.paper", "instrument_id": "XAUUSDT.BINANCE", "coin": "XAU",
     "news_queries": ["黄金", "金价", "美联储", "加息", "降息", "美元", "原油", "避险", "地缘"],
     "primary": ["黄金", "金价", "贵金属", "白银", "避险", "央行购金", "XAU"], "kline_key": "gold", "position": 2},
]
KLINE_KEYS = {"BTC": "bitcoin", "ETH": "ethereum", "HYPE": "hype", "XAU": "gold", "PAXG": "gold", "SILVER": "silver"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            if conn.execute("select count(*) from assets").fetchone()[0] == 0:
                for row in DEFAULT_ASSETS:
                    conn.execute(
                        "insert into assets (key,label,kind,venue_profile_id,instrument_id,coin,news_queries,primary_words,kline_key,position,created_at)"
                        " values (?,?,?,?,?,?,?,?,?,?,?)",
                        (row["key"], row["label"], row["kind"], row["venue_profile_id"], row["instrument_id"], row["coin"],
                         json.dumps(row["news_queries"], ensure_ascii=False), json.dumps(row["primary"], ensure_ascii=False),
                         row["kline_key"], row["position"], now_iso()),
                    )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def add_judgment(self, *, asset: str, direction: str, confidence: int, reason: str,
                     cited: list[dict[str, Any]], price_at: float | None, plan: dict[str, Any] | None,
                     action: str, created_at: str | None = None) -> dict[str, Any]:
        with self._conn() as conn:
            cur = conn.execute(
                "insert into judgments (created_at, asset, direction, confidence, reason, cited, price_at, plan, action)"
                " values (?,?,?,?,?,?,?,?,?)",
                (created_at or now_iso(), asset, direction, int(confidence), reason.strip(),
                 json.dumps(cited, ensure_ascii=False), price_at,
                 json.dumps(plan, ensure_ascii=False) if plan else None, action),
            )
            return self.judgment(cur.lastrowid, conn)

    def judgment(self, judgment_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        own = conn is None
        conn = conn or self._conn()
        try:
            row = conn.execute("select * from judgments where id=?", (judgment_id,)).fetchone()
            return _judgment(row) if row else {}
        finally:
            if own:
                conn.close()

    def judgments(self, asset: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if asset:
                rows = conn.execute("select * from judgments where asset=? order by id desc limit ?", (asset, limit)).fetchall()
            else:
                rows = conn.execute("select * from judgments order by id desc limit ?", (limit,)).fetchall()
        return [_judgment(r) for r in rows]

    def resolve(self, judgment_id: int, *, price_after: float | None, move_pct: float | None, outcome: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "update judgments set resolved_at=?, price_after=?, move_pct=?, outcome=? where id=? and resolved_at is null",
                (now_iso(), price_after, move_pct, outcome, judgment_id),
            )

    # ---- assets ------------------------------------------------------------
    def assets(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("select * from assets order by position, created_at").fetchall()
        return [_asset(r) for r in rows]

    def asset(self, key: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("select * from assets where key=?", (key,)).fetchone()
        return _asset(row) if row else None

    def add_hl_asset(self, *, coin: str, instrument_id: str, label: str | None = None, news_queries: list[str] | None = None) -> dict[str, Any]:
        key = coin.upper()
        if self.asset(key):
            raise ValueError("asset_exists")
        queries = [q for q in (news_queries or [coin, "加密", "美联储"]) if str(q).strip()][:10]
        with self._conn() as conn:
            position = (conn.execute("select coalesce(max(position),0) from assets").fetchone()[0] or 0) + 1
            conn.execute(
                "insert into assets (key,label,kind,venue_profile_id,instrument_id,coin,news_queries,primary_words,kline_key,position,created_at)"
                " values (?,?,?,?,?,?,?,?,?,?,?)",
                (key, label or key, "hl_testnet", "hyperliquid.testnet", instrument_id, coin, json.dumps(queries, ensure_ascii=False),
                 json.dumps([coin], ensure_ascii=False), KLINE_KEYS.get(key), position, now_iso()),
            )
        return self.asset(key)

    def update_asset_news(self, key: str, queries: list[str]) -> dict[str, Any]:
        cleaned = [q.strip() for q in queries if q.strip()][:10]
        if not cleaned:
            raise ValueError("news_queries_empty")
        with self._conn() as conn:
            conn.execute("update assets set news_queries=? where key=?", (json.dumps(cleaned, ensure_ascii=False), key))
        return self.asset(key)

    def remove_asset(self, key: str) -> None:
        with self._conn() as conn:
            if conn.execute("select count(*) from assets").fetchone()[0] <= 1:
                raise ValueError("last_asset")
            conn.execute("delete from assets where key=?", (key,))

    # ---- execution log ----------------------------------------------------
    def log_execution(self, *, asset: str, stage: str, judgment_id: int | None, preview_digest: str | None, detail: dict[str, Any]) -> dict[str, Any]:
        with self._conn() as conn:
            cur = conn.execute(
                "insert into executions (created_at, judgment_id, asset, stage, preview_digest, detail) values (?,?,?,?,?,?)",
                (now_iso(), judgment_id, asset, stage, preview_digest, json.dumps(detail, ensure_ascii=False, default=str)),
            )
            row = conn.execute("select * from executions where id=?", (cur.lastrowid,)).fetchone()
        item = dict(row)
        item["detail"] = json.loads(item["detail"])
        return item

    def executions(self, asset: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("select * from executions where (? is null or asset=?) order by id desc limit ?", (asset, asset, limit)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            out.append(item)
        return out

    def add_note(self, *, asset: str, body: str) -> dict[str, Any]:
        body = body.strip()
        if not body:
            raise ValueError("note_empty")
        with self._conn() as conn:
            cur = conn.execute("insert into notes (created_at, asset, body) values (?,?,?)", (now_iso(), asset, body))
            row = conn.execute("select * from notes where id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def notes(self, asset: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("select * from notes where asset=? order by id desc limit ?", (asset, limit))]


def _judgment(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["cited"] = json.loads(item.get("cited") or "[]")
    item["plan"] = json.loads(item["plan"]) if item.get("plan") else None
    return item


def _asset(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["news_queries"] = json.loads(item.get("news_queries") or "[]")
    item["primary"] = json.loads(item.pop("primary_words") or "[]")
    return item
