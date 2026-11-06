"""Pre-start backtest for one Grid plan: how did this shape do over past windows?

The shape is kept in percent of the plan's market price and re-laid at each window's
first price. Each window replays through the same isolated Nautilus replay Paper uses,
with Paper's own grid re-arm rule and Paper's own close at a boundary, and only sees bars
from after its start.

The replay is driven directly rather than through ``NautilusExecutionAdapter``: that
adapter refuses a ``park-paper-preflight-v1`` artifact without the runtime's mutation
gate, which is right for Paper and has no meaning here. Nothing in this module reaches a
Paper ledger, a venue or a gate. The results are history, shown beside a plan; they never
allow or block a start.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
from typing import Any

from services.config_loader import ROOT
from services.datafeed_market_client import DatafeedUnavailable
from services.dualtrack_execution_contract import execution_contract_evidence, normalize_execution_command
from services.dualtrack_nautilus_execution_adapter import _canonical_command, next_grid_rearm_commands
from services.dualtrack_shadow_input import build_shadow_input

SCHEMA = "grid-backtest-v1"
WINDOW = timedelta(days=7)
# One start every 12 hours (347 overlapping runs over 180 days) took 393 s on this Mac,
# over the spec's 5-minute limit, so the spec's fallback applies: non-overlapping weeks.
# Same median on the 9/8 plan (+0.25 %), 40 s.
STEP = WINDOW
MAX_DATA_AGE = timedelta(days=3)
PLAN_VERSION = "park-strategy-plan-v1"

Replay = Callable[[dict[str, Any]], dict[str, Any]]


class BacktestUnavailable(Exception):
    """The history cannot answer this; the page says so and the reason why."""


@dataclass(frozen=True)
class Bar:
    opened_at: datetime
    open: float
    close: float


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def shape_from_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The parts of a Grid plan that survive moving it to another price."""

    normalized = plan.get("normalized_input") if isinstance(plan.get("normalized_input"), Mapping) else {}
    risk = plan.get("risk") if isinstance(plan.get("risk"), Mapping) else {}
    market = plan.get("market") if isinstance(plan.get("market"), Mapping) else {}
    if normalized.get("strategy_type") != "grid":
        raise BacktestUnavailable("只有网格计划能回测")
    rungs = risk.get("grid_rungs")
    if not isinstance(rungs, list) or not rungs:
        raise BacktestUnavailable("计划没有网格档位")
    return {
        "direction": str(normalized.get("direction") or ""),
        "market_price": float(market["price"]),
        "lower": float(normalized["lower_price_boundary"]),
        "upper": float(normalized["upper_price_boundary"]),
        "stop": float(normalized["stop_price"]) if normalized.get("stop_price") not in (None, "") else None,
        "target": float(normalized["take_profit_price"]) if normalized.get("take_profit_price") not in (None, "") else None,
        "quantity": float(risk["per_order_quantity"]),
        "account_equity": float(risk["account_equity"]),
        "rungs": [{"rung": int(row["rung"]), "side": str(row["side"]), "price": float(row["price"]), "take_profit": float(row["take_profit"])} for row in rungs],
    }


def scaled(shape: Mapping[str, Any], price: float, *, price_step: str, quantity_step: str) -> dict[str, Any]:
    """Lay the shape at ``price``: same percent distances, same notional per rung."""

    factor = price / float(shape["market_price"])
    move = lambda value: float(Decimal(str(value * factor)).quantize(Decimal(price_step)))
    quantity = Decimal(str(float(shape["quantity"]) / factor)).quantize(Decimal(quantity_step), rounding=ROUND_DOWN)
    if quantity <= 0:
        raise BacktestUnavailable("这个价位下单量小于最小单位")
    return {
        **shape,
        "market_price": price,
        "lower": move(shape["lower"]),
        "upper": move(shape["upper"]),
        "stop": move(shape["stop"]) if shape.get("stop") is not None else None,
        "target": move(shape["target"]) if shape.get("target") is not None else None,
        "quantity": float(quantity),
        "rungs": [{**row, "price": move(row["price"]), "take_profit": move(row["take_profit"])} for row in shape["rungs"]],
    }


def terminal_reason(shape: Mapping[str, Any], price: float) -> str | None:
    """Paper's order: the boundaries, then the stop, then the target."""

    if price >= float(shape["upper"]):
        return "upper"
    if price <= float(shape["lower"]):
        return "lower"
    long = shape["direction"] == "long"
    if shape.get("stop") is not None and shape["direction"] != "neutral":
        if (long and price <= shape["stop"]) or (not long and price >= shape["stop"]):
            return "stop"
    if shape.get("target") is not None and shape["direction"] != "neutral":
        if (long and price >= shape["target"]) or (not long and price <= shape["target"]):
            return "target"
    return None


def market_events(bars: Sequence[Bar], *, cycle_id: str, instrument: str, source: str) -> list[dict[str, Any]]:
    """One event per minute, the way Paper observed them: the first bar's open, then each close.

    A close is stamped when the bar ended, so a replay never sees a price early.
    """

    points = [(bars[0].opened_at, bars[0].open)] + [(bar.opened_at + timedelta(minutes=1), bar.close) for bar in bars]
    return [_event(moment, price, cycle_id=cycle_id, instrument=instrument, source=source) for moment, price in points]


def _event(moment: datetime, price: float, *, cycle_id: str, instrument: str, source: str) -> dict[str, Any]:
    stamp = moment.isoformat()
    row = {
        "schema_version": "dualtrack-market-event-v1",
        "cycle_id": cycle_id,
        "ts_event": stamp,
        "event_started_at": stamp,
        "price": price,
        "open": price,
        "high": price,
        "low": price,
        "fresh": True,
        "is_synthetic": False,
        "source": source,
        "provider": source,
        "instrument_id": instrument,
    }
    return {**row, "event_id": _digest(row)}


def _identity(cycle_id: str) -> dict[str, Any]:
    return {
        "cycle_id": cycle_id,
        "strategy_plan_id": cycle_id,
        "strategy_plan_version": PLAN_VERSION,
        "strategy_session_id": cycle_id,
        "strategy_revision_id": cycle_id,
        "plan_digest": cycle_id,
        "source": "park_telegram",
    }


def entry_commands(
    shape: Mapping[str, Any], *, cycle_id: str, at: datetime, instrument: str, source: str, config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    long_only = {"long": "buy", "short": "sell"}.get(str(shape["direction"]))
    stamp = at.isoformat()
    rows = []
    for rung in shape["rungs"]:
        line = f"{cycle_id}:grid:{rung['rung']}"
        command = normalize_execution_command({
            **_identity(cycle_id),
            "ts": stamp,
            "event": "entry",
            "side": long_only or rung["side"],
            "order_type": "limit",
            "price": rung["price"],
            "market_price": shape["market_price"],
            "quantity": shape["quantity"],
            "notional": round(rung["price"] * shape["quantity"], 12),
            "source_fill_id": line,
            "symbol": instrument,
            "instrument_id": instrument,
            "market_timestamp": stamp,
            "market_source": source,
            "market_fresh": True,
            "tp": rung["take_profit"],
            **({"sl": shape["stop"]} if shape.get("stop") is not None else {}),
            "grid_line_id": line,
            "grid_generation": 1,
            "grid_rearm_enabled": True,
        }, dict(config))
        rows.append(_row(command))
    return rows


def _row(command: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _canonical_command(dict(command))
    return {
        "schema_version": "dualtrack-shadow-command-v1",
        "command_id": normalized["command_id"],
        "cycle_id": normalized["cycle_id"],
        "command": normalized["command"],
    }


def close_commands(
    positions: Sequence[Mapping[str, Any]], *, cycle_id: str, at: datetime, price: float, instrument: str, source: str, config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Paper's close at a boundary: one market order per open position."""

    stamp = at.isoformat()
    rows = []
    for position in positions:
        position_id = str(position.get("position_id") or "")
        quantity = float(position.get("remaining_units") or position.get("quantity") or 0.0)
        side = str(position.get("side") or "").lower()
        target_side = "long" if side in {"buy", "long"} else "short"
        rows.append(_row({
            **normalize_execution_command({
                **_identity(cycle_id),
                "ts": stamp,
                "event": "stop",
                "side": "sell" if target_side == "long" else "buy",
                "order_type": "market",
                "price": price,
                "market_price": price,
                "quantity": quantity,
                "source_fill_id": f"{cycle_id}:close:{position_id}",
                "position_id": position_id,
                "trade_id": str(position.get("trade_id") or ""),
                "target_position_id": position_id,
                "target_position_side": target_side,
                "target_command_id": str(position.get("trade_id") or ""),
                "target_remaining_before": quantity,
                "target_remaining_after": 0.0,
                "symbol": instrument,
                "instrument_id": instrument,
                "market_timestamp": stamp,
                "market_source": source,
                "market_fresh": True,
            }, dict(config)),
        }))
    return rows


def _settings(shape: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, float]:
    return {"starting_cash": float(config["capital_per_track_usd"]), "max_leverage": float(config["max_leverage"])}


def _replay_all(
    *, cycle_id: str, events: list[dict[str, Any]], commands: list[dict[str, Any]], replay: Replay, settings: Mapping[str, float]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replay until no grid line has a new generation to place, as Paper does."""

    current = sorted(commands, key=lambda row: str(row["command"].get("ts") or ""))
    for _ in range(len(events) + len(commands) + 1):
        snapshot = replay(build_shadow_input(
            cycle_id=cycle_id,
            authoritative_snapshot={"cycle_id": cycle_id, "engine": "nautilus_paper", "fills": []},
            market_events=events,
            commands=current,
            execution_settings=dict(settings),
        ))
        generated = next_grid_rearm_commands(cycle_id, current, snapshot)
        if not generated:
            return snapshot, current
        current = sorted([*current, *generated], key=lambda row: str(row["command"].get("ts") or ""))
    raise BacktestUnavailable("回放没有收敛")


def _signed_quantity(fills: Sequence[Mapping[str, Any]], moment: str) -> float:
    return sum(
        (1 if str(fill.get("side")) == "buy" else -1) * float(fill.get("quantity") or 0.0)
        for fill in fills
        if str(fill.get("ts") or "") <= moment
    )


def funding_cost(fills: Sequence[Mapping[str, Any]], funding: Sequence[Mapping[str, Any]], prices: Mapping[str, float]) -> float:
    """What the open position paid (positive) or received (negative) at each settlement."""

    return sum(
        _signed_quantity(fills, str(row["funding_time"])) * prices[str(row["funding_time"])] * float(row["realized_rate"] if row.get("realized_rate") is not None else row["funding_rate"])
        for row in funding
        if str(row["funding_time"]) in prices
    )


def run_window(
    shape: Mapping[str, Any],
    bars: Sequence[Bar],
    *,
    replay: Replay,
    config: Mapping[str, Any],
    instrument: str,
    source: str,
    price_step: str,
    quantity_step: str,
    funding: Sequence[Mapping[str, Any]] = (),
    funding_from: str | None = None,
) -> dict[str, Any]:
    started = bars[0].opened_at
    cycle_id = f"backtest-{started.strftime('%Y%m%d%H%M')}"
    laid = scaled(shape, bars[0].open, price_step=price_step, quantity_step=quantity_step)
    events = market_events(bars, cycle_id=cycle_id, instrument=instrument, source=source)
    ended = "time"
    for index, event in enumerate(events):
        reason = terminal_reason(laid, float(event["price"]))
        if reason:
            ended, events = reason, events[: index + 1]
            break
    settings = _settings(shape, config)
    commands = entry_commands(laid, cycle_id=cycle_id, at=started + timedelta(seconds=1), instrument=instrument, source=source, config=config)
    snapshot, commands = _replay_all(cycle_id=cycle_id, events=events, commands=commands, replay=replay, settings=settings)
    last = events[-1]
    open_positions = [row for row in snapshot.get("positions") or [] if str(row.get("status") or "").lower() == "open"]
    if open_positions:
        closed_at = datetime.fromisoformat(str(last["ts_event"])) + timedelta(seconds=1)
        events = [*events, _event(closed_at, float(last["price"]), cycle_id=cycle_id, instrument=instrument, source=source)]
        exits = close_commands(open_positions, cycle_id=cycle_id, at=closed_at, price=float(last["price"]), instrument=instrument, source=source, config=config)
        snapshot, commands = _replay_all(cycle_id=cycle_id, events=events, commands=[*commands, *exits], replay=replay, settings=settings)
    fills = [dict(row) for row in snapshot.get("fills") or []]
    gross = sum((1 if str(fill.get("side")) == "sell" else -1) * float(fill["price"]) * float(fill["quantity"]) for fill in fills)
    fees = sum(float(fill.get("cost") or 0.0) for fill in fills)
    window_end = str(last["ts_event"])
    settlements = [row for row in funding if started.isoformat() <= str(row["funding_time"]) <= window_end]
    prices = {str(event["ts_event"]): float(event["price"]) for event in events}
    funded = funding_cost(fills, settlements, prices)
    funding_complete = funding_from is not None and started.isoformat() >= funding_from
    net = gross - fees - (funded if funding_complete else 0.0)
    return {
        "started_at": started.isoformat(),
        "ended_at": window_end,
        "ended": ended,
        "fill_count": len(fills),
        "gross": round(gross, 4),
        "fees": round(fees, 4),
        "funding": round(funded, 4) if funding_complete else None,
        "net": round(net, 4),
        "return_pct": round(net / float(shape["account_equity"]) * 100, 4),
        "open_after_close": sum(1 for row in snapshot.get("positions") or [] if str(row.get("status") or "").lower() == "open"),
    }


def windows(bars: Sequence[Bar], *, step: timedelta = STEP, length: timedelta = WINDOW) -> list[list[Bar]]:
    """Every ``step`` from the first bar, each with a full ``length`` of bars after it."""

    if not bars:
        return []
    out: list[list[Bar]] = []
    start = bars[0].opened_at
    last = bars[-1].opened_at
    cursor = 0
    while start + length <= last + timedelta(minutes=1):
        while bars[cursor].opened_at < start:
            cursor += 1
        stop = cursor
        while stop < len(bars) and bars[stop].opened_at < start + length:
            stop += 1
        out.append(list(bars[cursor:stop]))
        start += step
    return out


def summarize(results: Sequence[Mapping[str, Any]], *, days: float) -> dict[str, Any]:
    returns = sorted(float(row["return_pct"]) for row in results)
    if not returns:
        raise BacktestUnavailable("没有可用的窗口")
    width = max((returns[-1] - returns[0]) / 12, 1e-9)
    counts = [0] * 12
    for value in returns:
        counts[min(11, int((value - returns[0]) / width))] += 1
    return {
        "runs": len(results),
        "independent_windows": int(days // 7),
        "median_return_pct": round(statistics.median(returns), 2),
        "worst_return_pct": round(returns[0], 2),
        "best_return_pct": round(returns[-1], 2),
        "stopped_pct": round(sum(1 for row in results if row["ended"] in {"lower", "stop"}) / len(results) * 100, 1),
        "mean_fills": round(statistics.mean(int(row["fill_count"]) for row in results), 1),
        "funded_runs": sum(1 for row in results if row["funding"] is not None),
        "histogram": {"low": round(returns[0], 2), "width": round(width, 4), "counts": counts},
    }


def load_history(client: Any, *, source: str, instrument: str, asset_class: str = "commodity", now: datetime | None = None) -> dict[str, Any]:
    """Bars and funding, or the reason there are none. A gap is never stepped over."""

    moment = now or datetime.now(timezone.utc)
    try:
        report = client.history_report(source=source, instrument=instrument)
    except DatafeedUnavailable as error:
        raise BacktestUnavailable(f"读不到历史数据：{error}") from error
    if report.get("gaps") or report.get("fetch_failures"):
        raise BacktestUnavailable(f"历史数据有缺口：{report.get('missing_minutes')} 分钟")
    start, end = str(report.get("requested_start") or ""), str(report.get("requested_end") or "")
    if not start or not end or moment - datetime.fromisoformat(end) > MAX_DATA_AGE:
        raise BacktestUnavailable(f"历史数据太旧：截至 {end or '未知'}")
    if datetime.fromisoformat(end) - datetime.fromisoformat(start) < WINDOW:
        raise BacktestUnavailable("历史不够一个窗口")
    bars: list[Bar] = []
    cursor = datetime.fromisoformat(start)
    stop = datetime.fromisoformat(end)
    try:
        while cursor < stop:
            upper = min(cursor + WINDOW, stop)
            payload = client.history_candles(
                asset_class=asset_class, ticker=instrument, source=source,
                start=cursor.isoformat(), end=(upper - timedelta(seconds=1)).isoformat(),
            )
            for row in payload.get("candles") or []:
                opened = datetime.fromisoformat(str(row["timestamp"]))
                bars.append(Bar(opened if opened.tzinfo else opened.replace(tzinfo=timezone.utc), float(row["open"]), float(row["close"])))
            cursor = upper
        funding = client.history_funding(source=source, instrument=instrument).get("funding") or []
    except DatafeedUnavailable as error:
        raise BacktestUnavailable(f"读不到历史数据：{error}") from error
    expected = int(report.get("expected_bars") or 0)
    if len({bar.opened_at for bar in bars}) != expected:
        raise BacktestUnavailable(f"历史数据不完整：{len(bars)} / {expected} 根")
    return {"bars": bars, "funding": funding, "funding_from": funding[0]["funding_time"] if funding else None}


def subprocess_replay(python: str | Path, preflight: str | Path) -> Replay:
    """The isolated replay Paper runs, with Paper's stripped environment."""

    return _replay(python, lambda folder: (Path(preflight), []))


def backtest_replay(python: str | Path, contract: Mapping[str, Any]) -> Replay:
    """The same isolated replay, given a ``backtest_only`` contract instead of Paper's preflight (#1337)."""

    if contract.get("status") != "backtest_only":
        raise BacktestUnavailable("回测合约说明的状态必须是 backtest_only")

    def write(folder: Path) -> tuple[Path, list[str]]:
        path = folder / "contract.json"
        path.write_text(json.dumps([dict(contract)]), encoding="utf-8")
        return path, ["--backtest-contract"]

    return _replay(python, write)


def _replay(python: str | Path, contract_for: Callable[[Path], tuple[Path, list[str]]]) -> Replay:
    def replay(bundle: dict[str, Any]) -> dict[str, Any]:
        environment = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ") if os.environ.get(key) is not None}
        environment["PYTHONPATH"] = str(ROOT)
        with tempfile.TemporaryDirectory(prefix="grid-backtest-") as folder:
            source, target = Path(folder) / "input.json", Path(folder) / "output.json"
            source.write_text(json.dumps([bundle]), encoding="utf-8")
            contract, flags = contract_for(Path(folder))
            result = subprocess.run(
                [str(python), str(ROOT / "spikes" / "dualtrack_nautilus_shadow_replay.py"), "--preflight", str(contract), *flags,
                 "--input", str(source), "--output", str(target)],
                cwd=ROOT, env=environment, capture_output=True, text=True, check=False,
            )
            if result.returncode:
                raise RuntimeError(f"Nautilus replay failed: {result.stderr[-500:]}")
            return json.loads(target.read_text(encoding="utf-8"))[-1]

    return replay


def run_backtest(
    shape: Mapping[str, Any],
    bars: Sequence[Bar],
    *,
    replay: Replay,
    config: Mapping[str, Any],
    instrument: str,
    source: str,
    funding: Sequence[Mapping[str, Any]] = (),
    funding_from: str | None = None,
    step: timedelta = STEP,
    workers: int = 1,
    now: datetime | None = None,
) -> dict[str, Any]:
    spans = windows(bars, step=step)
    if not spans:
        raise BacktestUnavailable("历史不够一个窗口")

    contract = execution_contract_evidence(dict(config))

    def one(span: list[Bar]) -> dict[str, Any]:
        return run_window(
            shape, span, replay=replay, config=config, instrument=instrument, source=source,
            price_step=str(contract["price_increment"]), quantity_step=str(contract["quantity_increment"]),
            funding=funding, funding_from=funding_from,
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(one, spans))
    days = (bars[-1].opened_at - bars[0].opened_at).total_seconds() / 86400
    return {
        "schema_version": SCHEMA,
        "status": "ready",
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "source": source,
        "instrument": instrument,
        "history_start": bars[0].opened_at.isoformat(),
        "history_end": bars[-1].opened_at.isoformat(),
        "summary": summarize(results, days=days),
        "windows": results,
        "paper_only": True,
    }
