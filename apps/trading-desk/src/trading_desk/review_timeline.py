"""One timeline per past run, for the 交易回看 tab (#1306).

Pure read: nothing here writes, and it never resolves a judgment. Missing records are named in
`missing`; nothing is guessed. These are Paper or Testnet records, never proof of a venue action.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "trade-review-timeline-v1"
FLAT_TOLERANCE = 1e-9
BAR_SECONDS = 3600
REASONS = {
    "lower_boundary_invalidated": "价格跌破区间下沿",
    "upper_boundary_invalidated": "价格涨破区间上沿",
    "exit_fill_slippage_exceeded": "平仓滑点超限",
    "hard_stop": "触发硬止损",
}
TESTNET_EVENTS = {
    "lifecycle_started": "启动",
    "ladder_activated": "挂出网格",
    "hard_stop_triggered": "触发硬止损",
    "emergency_flatten_submitted": "提交紧急平仓",
    "exchange_exposure_closed": "交易所持仓已清零",
    "lifecycle_blocked": "被门禁拦下",
    "revision_sealed": "这一轮结束",
}
TESTNET_STATUS = {"terminal": "已结束", "stopped_by_operator": "人工停止", "blocked_reconciliation": "对账拦下", "blocked_local_validation": "本地校验拦下"}


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _jsonl(path: Path) -> list[dict[str, Any]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _latest(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        value = value[-1] if value else None
    return value if isinstance(value, dict) else {}


def _at(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _second(value: Any) -> str:
    return str(value or "")[:19]


def _result(fills: list[dict[str, Any]]) -> dict[str, Any]:
    """Net result only when the fills net to flat; an open position has no result yet."""
    cash = sum((f["price"] * f["quantity"]) * (1 if f["side"] == "sell" else -1) for f in fills)
    fees = [f["cost"] for f in fills if f.get("cost") is not None]
    open_quantity = sum(f["quantity"] * (1 if f["side"] == "buy" else -1) for f in fills)
    flat = abs(open_quantity) < FLAT_TOLERANCE
    fee_total = round(sum(fees), 2) if len(fees) == len(fills) else None
    return {
        "fill_count": len(fills),
        "entries": sum(f["event"] == "entry" for f in fills),
        "exits": sum(f["event"] != "entry" for f in fills),
        "flat": flat,
        "open_quantity": round(open_quantity, 8),
        "gross": round(cash, 2) if flat else None,
        "fees": fee_total,
        "net": round(cash - fee_total, 2) if flat and fee_total is not None else None,
        "excludes_funding": True,
    }


def _hourly(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hourly bars from the run's own recorded market events."""
    buckets: dict[int, dict[str, Any]] = {}
    for event in events:
        at = _at(event.get("event_started_at") or event.get("ts_event"))
        price = event.get("price")
        if at is None or price is None:
            continue
        price = float(price)
        high = float(event.get("high") or price)
        low = float(event.get("low") or price)
        key = int(at.timestamp()) // BAR_SECONDS * BAR_SECONDS
        bar = buckets.get(key)
        if bar is None:
            buckets[key] = {"timestamp": datetime.fromtimestamp(key, timezone.utc).isoformat(), "open": float(event.get("open") or price),
                            "high": high, "low": low, "close": price, "volume": 0}
        else:
            bar.update(high=max(bar["high"], high), low=min(bar["low"], low), close=price)
    return [buckets[key] for key in sorted(buckets)]


def _paper_records(root: Path) -> tuple[dict[str, list[dict[str, Any]] | None], list[str]]:
    names = {"identity": "identity.jsonl", "lifecycle": "lifecycle.jsonl", "executions": "executions.jsonl", "plans": "plans.jsonl"}
    records = {key: _jsonl(root / "park_strategy" / name) for key, name in names.items()}
    return records, [f"park_strategy/{names[key]}" for key, rows in records.items() if rows is None]


def _session_of(path: Path) -> str:
    return path.stem.removeprefix("park-")


def _paper_fills(rows: Any) -> list[dict[str, Any]]:
    fills = []
    for row in rows if isinstance(rows, list) else []:
        fills.append({"ts": row.get("ts"), "side": str(row.get("side") or "").lower(), "event": row.get("event"),
                      "price": float(row["price"]), "quantity": float(row["quantity"]),
                      "cost": float(row["cost"]) if row.get("cost") is not None else None, "order_id": row.get("order_id")})
    return fills


def _testnet_side(row: dict[str, Any], direction: str) -> str:
    entry = row.get("event") == "entry"
    return "buy" if entry == (direction != "short") else "sell"


def _testnet_fills(state: dict[str, Any]) -> list[dict[str, Any]]:
    direction = str(state.get("direction") or "long")
    return [{"ts": row.get("timestamp"), "side": _testnet_side(row, direction), "event": "entry" if row.get("event") == "entry" else row.get("event"),
             "price": float(row["price"]), "quantity": float(row["quantity"]),
             "cost": float(row["fee"]) if row.get("fee") is not None else None, "order_id": row.get("order_id")}
            for row in state.get("fills") or [] if row.get("price") is not None and row.get("quantity") is not None]


def runs(paper_output: Path) -> list[dict[str, Any]]:
    """Every past run with at least one fill, newest first."""
    root = Path(paper_output)
    items: list[dict[str, Any]] = []
    records, _ = _paper_records(root)
    identity = records["identity"] or []
    for path in sorted((root / "dualtrack" / "nautilus_authoritative" / "fills").glob("*.json")):
        session = _session_of(path)
        fills = _paper_fills(_json(path))
        if not fills:
            continue
        started = next((r.get("observed_at") for r in identity if r.get("event") == "session_started" and r.get("strategy_session_id") == session), fills[0]["ts"])
        items.append({"id": f"paper:{session}", "asset": "XAU", "environment": "paper", "label": "黄金 Paper", "started_at": started, "ended_at": fills[-1]["ts"],
                      "fill_count": len(fills), "status": "ended"})
    for path in sorted((root / "dualtrack" / "grid_testnet_lifecycle").glob("*.json")):
        state = _latest(_json(path))
        fills = _testnet_fills(state)
        if not fills:
            continue
        items.append({"id": f"testnet:{state.get('strategy_plan_id')}", "asset": _coin(state), "environment": "testnet", "label": f"{_coin(state)} Testnet", "started_at": state.get("created_at"),
                      "ended_at": next((f["ts"] for f in reversed(fills) if f["ts"]), state.get("created_at")), "fill_count": len(fills), "status": TESTNET_STATUS.get(str(state.get("status")), "已结束")})
    return sorted(items, key=lambda item: str(item["started_at"]), reverse=True)


def _paper_timeline(root: Path, session: str) -> dict[str, Any] | None:
    stem = f"park-{session}"
    base = root / "dualtrack" / "nautilus_authoritative"
    raw_fills = _json(base / "fills" / f"{stem}.json")
    if raw_fills is None:
        return None
    fills = _paper_fills(raw_fills)
    records, missing = _paper_records(root)
    lifecycle = records["lifecycle"] or []
    executions = records["executions"] or []
    plan = next((r for r in lifecycle if r.get("event") == "plan_activated" and r.get("strategy_session_id") == session), None)
    if plan is None:
        missing.append("计划记录")
    seal = next((r for r in executions if r.get("event") == "terminal_paused" and r.get("strategy_session_id") == session), None)
    done = next((r for r in executions if seal and r.get("event") == "terminal_close_completed" and r.get("terminal_key") == seal.get("terminal_key")), None)
    if seal is None:
        missing.append("结束记录")
    result_at_seal = (seal or {}).get("result") or {}
    open_at_seal = ((result_at_seal.get("reconciliation") or {}).get("counts") or {}).get("open_positions")
    reason = REASONS.get(str((seal or {}).get("terminal_key") or "").rsplit(":", 1)[-1], "原因见原始记录")
    # Exits stamped at the seal but settled later keep their price; the chart places them when they settled (#1311).
    late = bool(seal and done and open_at_seal)
    for fill in fills:
        moved = late and fill["event"] != "entry" and fill["event"] != "target" and _second(fill["ts"]) == _second(seal.get("recorded_at"))
        fill["shown_ts"] = done["recorded_at"] if moved else fill["ts"]
        fill["backdated"] = moved
    proposal = next((r for r in (records["plans"] or []) if plan and r.get("plan_digest") == plan.get("plan_digest")), {})
    events = []
    if proposal:
        events.append({"ts": proposal.get("created_at"), "kind": "plan", "title": "计划生成", "source": "park_strategy/plans.jsonl", "record": {"plan_digest": proposal.get("plan_digest")}})
    if fills:
        first = [f for f in fills if f["ts"] == fills[0]["ts"]]
        events.append({"ts": fills[0]["ts"], "kind": "start", "title": f"启动，开仓 {len(first)} 笔", "source": "nautilus_authoritative/fills", "record": first[0]})
    if seal:
        title = f"{reason}，策略被标记为结束" + (f"，当时还有 {open_at_seal} 笔未平" if open_at_seal else "")
        events.append({"ts": seal.get("recorded_at"), "kind": "seal", "title": title, "source": "park_strategy/executions.jsonl", "record": {"event": "terminal_paused", "open_positions": open_at_seal, "reconciliation": (result_at_seal.get("reconciliation") or {}).get("status")}})
    if done:
        events.append({"ts": done.get("recorded_at"), "kind": "closed", "title": f"{done.get('exits')} 笔持仓全部结算" if done.get("exits") else "已全部平仓", "source": "park_strategy/executions.jsonl", "record": {"event": "terminal_close_completed", "exits": done.get("exits")}})
    recorded = _latest(_json(base / "inputs" / f"{stem}.json"))
    market_events = recorded.get("market_events") or []
    if not market_events:
        missing.append("行情记录")
    first_event = market_events[0] if market_events else {}
    result = _result(fills)
    result["ended_flat"] = (open_at_seal == 0) if open_at_seal is not None else None
    lines = [{"price": plan[key], "title": title} for key, title in (("upper_price_boundary", "区间上沿"), ("lower_price_boundary", "区间下沿"), ("stop_price", "止损")) if plan and plan.get(key) is not None]
    return {"schema_version": SCHEMA, "run": {"id": f"paper:{session}", "asset": "XAU", "environment": "paper", "label": "黄金 Paper", "reason": reason},
            "market": {"source": first_event.get("provider") or first_event.get("source") or "", "instrument": first_event.get("instrument_id") or "", "timeframe": "1h",
                       "synthetic": any(event.get("is_synthetic") for event in market_events), "bars": _hourly(market_events)},
            "lines": lines, "fills": fills, "events": events, "result": result, "missing": missing}


def _coin(state: dict[str, Any]) -> str:
    return str(state.get("instrument_id") or "BTC").split("-")[0]


def _testnet_timeline(root: Path, plan_id: str, bars: Callable[[str, str, str], dict[str, Any]]) -> dict[str, Any] | None:
    path = root / "dualtrack" / "grid_testnet_lifecycle" / f"{plan_id}.json"
    state = _latest(_json(path))
    if not state:
        return None
    fills = _testnet_fills(state)
    for fill in fills:
        fill.update(shown_ts=fill["ts"], backdated=False)
    events: list[dict[str, Any]] = []
    for row in state.get("events") or []:
        title = TESTNET_EVENTS.get(str(row.get("event")))
        if title and not (events and events[-1]["title"] == title):
            events.append({"ts": row.get("timestamp"), "kind": "lifecycle", "title": title, "source": "grid_testnet_lifecycle", "record": {"event": row["event"]}})
    moments = [str(f["ts"]) for f in fills if f["ts"]] + [str(e["ts"]) for e in events if e["ts"]]
    market = bars(_coin(state), str(state.get("created_at") or min(moments)), max(moments)) if moments else {"ok": False}
    missing = [] if market.get("ok") else ["行情记录"]
    result = _result(fills)
    result["ended_flat"] = result["flat"]
    reason = REASONS.get(str(state.get("terminal_reason")), TESTNET_STATUS.get(str(state.get("status")), "原因见原始记录"))
    lines = [{"price": state[key], "title": title} for key, title in (("upper_boundary", "区间上沿"), ("lower_boundary", "区间下沿")) if state.get(key) is not None]
    return {"schema_version": SCHEMA, "run": {"id": f"testnet:{plan_id}", "asset": _coin(state), "environment": "testnet", "label": f"{_coin(state)} Testnet", "reason": reason},
            "market": {"source": market.get("source") or "", "instrument": state.get("instrument_id") or "", "timeframe": "1h", "synthetic": False,
                       "bars": [{"timestamp": row[0], "open": row[1], "high": row[2], "low": row[3], "close": row[4], "volume": 0} for row in market.get("bars") or []]},
            "lines": lines, "fills": fills, "events": events, "result": result, "missing": missing}


def timeline(paper_output: Path, run_id: str, *, bars: Callable[[str, str, str], dict[str, Any]], judgments: list[dict[str, Any]]) -> dict[str, Any] | None:
    root = Path(paper_output)
    kind, _, key = run_id.partition(":")
    if not key or "/" in key or ".." in key:
        return None
    payload = _paper_timeline(root, key) if kind == "paper" else _testnet_timeline(root, key, bars) if kind == "testnet" else None
    if payload is None:
        return None
    moments = [m for m in (_at(f["shown_ts"]) for f in payload["fills"]) if m is not None]
    if moments:
        start, end = min(moments), max(moments)
        inside = [j for j in judgments if (at := _at(j.get("created_at"))) is not None and start <= at <= end]
        payload["judgments"] = [{key: row.get(key) for key in ("id", "created_at", "author", "direction", "outcome", "reason", "price_at")} for row in inside]
    else:
        payload["judgments"] = []
    return payload


def scoreboard(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Judgment record by author, counted the same way the review list counts it."""
    board: dict[str, dict[str, int]] = {}
    for author in ("ai", "park"):
        items = [row for row in rows if row.get("author", "park") == author]
        count = lambda outcome: sum(row.get("outcome") == outcome for row in items)  # noqa: E731
        board[author] = {"total": len(items), "hit": count("hit"), "miss": count("miss"), "even": count("even"), "unverifiable": count("unverifiable"), "pending": count(None)}
    return board
