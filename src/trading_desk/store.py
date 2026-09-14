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
create table if not exists notes (
  id integer primary key autoincrement,
  created_at text not null,
  asset text not null,
  body text not null
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

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
