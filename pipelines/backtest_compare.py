"""回测工作台 (#1335): run up to three parameter sets of one strategy over one stored history.

Runs apart from every service and writes one JSON file. History is loaded once; the variants run
one after another in this process. A failure is a result too (``status: unavailable``), because the
page shows it. Nothing here reaches a ledger, a venue or a gate.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable

from services import backtest_evidence as evidence
from services.config_loader import ROOT
from services.datafeed_market_client import DatafeedMarketClient
from services.dca_backtest import run_backtest as run_dca
from services.dca_backtest import validate as validate_dca
from services.dualtrack_config import dualtrack_config
from services.dualtrack_nautilus_instrument import BACKTEST_ASSET_CLASSES, validate_instrument_definition
from services.grid_backtest import MAX_DATA_AGE, STEP, WINDOW, BacktestUnavailable, Replay, backtest_replay, load_history
from services.grid_backtest import run_backtest as run_grid

SCHEMA = "backtest-compare-v1"
MAX_VARIANTS = 3
# asset -> (datafeed source, instrument, asset class). One stored series each; no fallback between them.
ASSETS: dict[str, tuple[str, str, str]] = {
    "XAU": ("okx_swap_public", "XAU-USDT-SWAP", "commodity"),
    "BTC": ("okx_swap_crypto_public", "BTC-USDT-SWAP", "crypto"),
    # #1388: silver and WTI, the desk's keys. OKX listed these swaps in 2026, so their history is short.
    "SILVER": ("okx_swap_public", "XAG-USDT-SWAP", "commodity"),
    "WTI": ("okx_swap_public", "CL-USDT-SWAP", "commodity"),
}
STRATEGIES = ("dca", "grid")
# Grids replay through Paper's Nautilus replay against a backtest contract (#1337): the venue's own
# instrument definition for that exact series, plus Paper's fee model, status backtest_only.
GRID_ASSETS = ("XAU", "BTC", "SILVER", "WTI")
CONTRACT_SCHEMA = "backtest-contract-v1"
GRID_WORKERS = 3  # of 10 cores; Paper runs on the same machine


def validate_grid(params: dict[str, Any]) -> dict[str, Any]:
    """A grid laid around the current price, in percent. Raises ValueError naming the field."""

    try:
        direction = str(params["direction"])
        below, above = float(params["below_pct"]), float(params["above_pct"])
        levels = int(params["levels"])
        notional = float(params["notional_per_level"])
        stop = params.get("stop_pct")
        stop = None if stop in (None, "", 0, 0.0) else float(stop)
    except (KeyError, TypeError, ValueError):
        raise ValueError("网格参数要有方向、下沿 %、上沿 %、格数、每格金额") from None
    if direction not in {"long", "short", "neutral"}:
        raise ValueError("方向只能是做多、做空或双向")
    if not (0.5 <= below <= 30 and 0.5 <= above <= 30):
        raise ValueError("上下沿要在现价 0.5%–30% 之内")
    if not 2 <= levels <= 30 or float(params["levels"]) != levels:
        raise ValueError("格数要是 2–30 的整数")
    if not 10 <= notional <= 100_000:
        raise ValueError("每格金额要在 10–100000 USDT 之间")
    if stop is not None:
        if direction == "neutral":
            raise ValueError("双向网格用上下沿止损，不另设止损")
        edge = below if direction == "long" else above
        if not 0 < stop < edge:
            raise ValueError("止损要在现价和区间边沿之间")
    return {"direction": direction, "below_pct": below, "above_pct": above, "levels": levels, "notional_per_level": notional, "stop_pct": stop}


def grid_shape(params: dict[str, Any], *, equity: float) -> dict[str, Any]:
    """The same geometry park_strategy_plan builds, laid at price 1.0; grid_backtest.scaled moves it."""

    lower, upper = 1 - params["below_pct"] / 100, 1 + params["above_pct"] / 100
    spacing = (upper - lower) / (params["levels"] + 1)
    prices = [lower + spacing * (i + 1) for i in range(params["levels"])]
    direction = params["direction"]
    rungs = []
    for i, price in enumerate(prices):
        side = ("buy" if price < 1 else "sell") if direction == "neutral" else ("buy" if direction == "long" else "sell")
        take_profit = (prices[i + 1] if i + 1 < len(prices) else upper) if side == "buy" else (prices[i - 1] if i > 0 else lower)
        rungs.append({"rung": i + 1, "side": side, "price": price, "take_profit": take_profit})
    stop = params["stop_pct"]
    return {
        "direction": direction, "market_price": 1.0, "lower": lower, "upper": upper,
        "stop": None if stop is None else (1 - stop / 100 if direction == "long" else 1 + stop / 100),
        "target": None, "quantity": params["notional_per_level"], "account_equity": equity, "rungs": rungs,
    }


def check(request: dict[str, Any]) -> dict[str, Any]:
    """The request the page may send, normalized. Raises ValueError naming what is wrong."""

    asset = str(request.get("asset") or "").upper()
    if asset not in ASSETS:
        raise ValueError(f"品种只能是 {' / '.join(ASSETS)}")
    strategy = str(request.get("strategy") or "")
    if strategy not in STRATEGIES:
        raise ValueError("策略只能是定投（dca）或网格（grid）")
    if strategy == "grid" and asset not in GRID_ASSETS:
        raise ValueError(f"网格回测只开通了 {' / '.join(GRID_ASSETS)}")
    variants = request.get("variants")
    if not isinstance(variants, list) or not 1 <= len(variants) <= MAX_VARIANTS:
        raise ValueError(f"参数要有 1–{MAX_VARIANTS} 组")
    validate = validate_dca if strategy == "dca" else validate_grid
    return {"asset": asset, "strategy": strategy, "variants": [validate(v if isinstance(v, dict) else {}) for v in variants]}


def write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def backtest_contract(client: Any, asset: str, config: dict[str, Any]) -> dict[str, Any]:
    """The contract a grid backtest replays against (#1337). Raises BacktestUnavailable.

    The instrument is the venue's own definition, read from the same datafeed source as the history
    and checked like Paper checks its preflight. Fees are Paper's fee model. The status is
    ``backtest_only``: the replay accepts it only from the backtest entry, and Paper never does.
    """

    source, instrument, asset_class = ASSETS[asset]
    try:
        definition = client.instrument_definition(asset_class=asset_class, instrument=instrument, source=source)
    except Exception as error:  # noqa: BLE001 - any failure is "no contract", said on the page
        raise BacktestUnavailable(f"读不到 {instrument} 的合约说明（{source}）：{type(error).__name__}") from error
    if not isinstance(definition, dict):
        raise BacktestUnavailable(f"{instrument} 的合约说明不是一个对象")
    try:
        validate_instrument_definition(definition, asset_classes=BACKTEST_ASSET_CLASSES)
    except ValueError as error:
        raise BacktestUnavailable(f"{instrument} 的合约说明不可用：{error}") from error
    if definition.get("symbol") != instrument or definition.get("provider") != source:
        raise BacktestUnavailable(
            f"合约说明是 {definition.get('symbol')}（{definition.get('provider')}），回测历史是 {instrument}（{source}），两者不一致")
    fees = dict(config["paper_fee_model"])
    return {
        "schema_version": CONTRACT_SCHEMA,
        "status": "backtest_only",
        "paper_only": True,
        "real_money_eligible": False,
        "instrument": definition,
        "fee_model": {
            "mode": "backtest_contract",
            "maker_fee_rate": str(fees.get("maker_fee_rate") or "0"),
            "taker_fee_rate": str(fees["taker_fee_rate"]),
            "funding_rate": "0",
            "source": "park_paper_config",
            "environment": "paper",
            "real_money_eligible": False,
        },
    }


def contract_config(config: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Paper's config with the execution precision of the contract's instrument (gold's is unchanged)."""

    definition = contract["instrument"]
    return {**config, "execution_contract": {
        **dict(config.get("execution_contract") or {}),
        "execution_instrument_id": str(definition["symbol"]),
        "price_precision": int(definition["price_precision"]),
        "price_increment": str(definition["price_increment"]),
        "quantity_precision": int(definition["size_precision"]),
        "quantity_increment": str(definition["size_increment"]),
    }}


def settings(**extra: Any) -> dict[str, Any]:
    """The settings besides the request that change a result (#1369)."""
    return {"window_days": WINDOW.days, "step_days": STEP.days, "max_data_age_days": MAX_DATA_AGE.days, **extra}


def compare(request: dict[str, Any], *, client: Any, config: dict[str, Any], replay_for: Callable[[dict[str, Any]], Replay] | None = None,
            now: datetime | None = None, code: dict[str, Any] | None = None, started: datetime | None = None) -> dict[str, Any]:
    checked = check(request)
    source, instrument, asset_class = ASSETS[checked["asset"]]
    contract: dict[str, Any] | None = None
    if checked["strategy"] == "grid":
        if replay_for is None:
            raise BacktestUnavailable("网格回测需要 Paper 的回放环境")
        contract = backtest_contract(client, checked["asset"], config)  # before any history is read
    fee_rate = float(config["paper_fee_model"]["taker_fee_rate"])
    history = load_history(client, source=source, instrument=instrument, asset_class=asset_class, now=now)
    bars, funding, funding_from = history["bars"], history["funding"], history["funding_from"]
    windows: list[dict[str, Any]] = []
    if checked["strategy"] == "dca":
        variants = []
        for params in checked["variants"]:
            result = run_dca(params, bars, fee_rate=fee_rate, funding=funding, funding_from=funding_from)
            windows.extend(result["windows"])
            variants.append({key: value for key, value in result.items() if key != "windows"})
    else:
        assert replay_for is not None and contract is not None
        equity = float(config["capital_per_track_usd"])
        replay, grid_config = replay_for(contract), contract_config(config, contract)
        variants = []
        for params in checked["variants"]:
            result = run_grid(grid_shape(params, equity=equity), bars, replay=replay, config=grid_config, instrument=instrument, source=source,
                              funding=funding, funding_from=funding_from, workers=GRID_WORKERS, now=now)
            windows.extend(result["windows"])
            variants.append({"params": params, "summary": result["summary"], "account_equity": equity})
    contract_note = None if contract is None else {
        "status": contract["status"], "observed_at": contract["instrument"].get("observed_at"),
        "price_increment": contract["instrument"]["price_increment"], "size_increment": contract["instrument"]["size_increment"],
    }
    extra = {"fee_rate": fee_rate}
    if contract is not None:
        extra |= {"account_equity": float(config["capital_per_track_usd"]), "max_leverage": float(config["max_leverage"]),
                  "price_increment": contract["instrument"]["price_increment"], "size_increment": contract["instrument"]["size_increment"]}
    pack = evidence.pack(
        code=code or evidence.code_identity(ROOT),
        data=evidence.data_evidence(source=source, instrument=instrument, bars=bars, funding=funding, funding_from=funding_from,
                                    report=history.get("report")),
        params={"request": checked, **settings(**extra)},
        inputs={"contract_sha256": evidence.value_sha256(contract)} if contract is not None else {},
        checks=[evidence.history_check(last_bar=bars[-1].opened_at, run_started=started or now or datetime.now(timezone.utc)),
                *evidence.window_checks(windows, length=WINDOW, fill_word="成交" if checked["strategy"] == "grid" else "买入")],
    )
    return {
        "schema_version": SCHEMA, "status": "ready", **checked, "variants": variants, "evidence": pack,
        "source": source, "instrument": instrument, "fee_rate": fee_rate,
        "history_start": bars[0].opened_at.isoformat(), "history_end": bars[-1].opened_at.isoformat(),
        "funding_from": history["funding_from"], "contract": contract_note, "paper_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--datafeed-url", default="http://127.0.0.1:8100")
    parser.add_argument("--nautilus-python", help="Python with nautilus_trader (grid only)")
    # Ignored since #1337 (grids build their own backtest contract). Kept for one release so a desk
    # started before the deploy can still launch this CLI during the restart window.
    parser.add_argument("--preflight", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    started = datetime.now(timezone.utc)
    write_result(args.output, {"schema_version": SCHEMA, "status": "running", "started_at": started.isoformat()})
    code = evidence.code_identity(ROOT)
    request: Any = None
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        python = args.nautilus_python
        replay_for = (lambda contract: backtest_replay(python, contract)) if python else None
        result = compare(request, client=DatafeedMarketClient(base_url=args.datafeed_url, timeout_seconds=60.0), config=dualtrack_config(),
                         replay_for=replay_for, code=code, started=started)
    except (BacktestUnavailable, ValueError) as error:
        result = {"schema_version": SCHEMA, "status": "unavailable", "reason": str(error)}
    except Exception as error:  # noqa: BLE001 - the page says it failed; it must not take anything down.
        result = {"schema_version": SCHEMA, "status": "unavailable", "reason": f"回测出错：{type(error).__name__}", "detail": str(error)[-500:]}
    if "evidence" not in result:
        result["evidence"] = evidence.pack(code=code, data=None, params={"request": request, **settings()}, reason=result.get("reason"))
    result["started_at"] = started.isoformat()
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["duration_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    write_result(args.output, result)
    print(json.dumps({key: result.get(key) for key in ("status", "reason", "duration_seconds")}, ensure_ascii=False))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
