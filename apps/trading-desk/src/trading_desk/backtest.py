"""开仓前回测 (#1309): start the backtest for a previewed gold plan and read its result.

The backtest runs in its own process (``pipelines.grid_backtest``) and writes one JSON file
under the desk's own folder. It never touches the Paper ledger, and nothing here can allow
or block a start: the page shows the numbers next to the start button and that is all.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

KEY = re.compile(r"^[0-9a-f]{24}$")
STALE_RUN = timedelta(minutes=15)
KEEP = 60

Launch = Callable[..., Any]


def shape_key(plan: dict[str, Any], *, day: str) -> str:
    """Same shape in percent, same data day -> same result. The price level does not matter."""

    normalized = plan.get("normalized_input") or {}
    risk = plan.get("risk") or {}
    price = float((plan.get("market") or {})["price"])
    rel = lambda value: None if value in (None, "") else round(float(value) / price, 5)
    shape = {
        "direction": normalized.get("direction"),
        "lower": rel(normalized.get("lower_price_boundary")),
        "upper": rel(normalized.get("upper_price_boundary")),
        "stop": rel(normalized.get("stop_price")),
        "target": rel(normalized.get("take_profit_price")),
        "notional": round(float(risk.get("per_order_quantity") or 0) * price, 2),
        "rungs": [[row.get("side"), rel(row.get("price")), rel(row.get("take_profit"))] for row in risk.get("grid_rungs") or []],
        "day": day,
    }
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:24]


class Backtests:
    def __init__(self, config: Any, *, launch: Launch = subprocess.Popen, now: Callable[[], datetime] | None = None) -> None:
        self.folder = Path(config.db_path).parent / "backtests"
        self.checkout = Path(config.trading_system_checkout)
        self.python = Path(config.nautilus_python)
        self.preflight = Path(config.paper_output) / "park_strategy" / "paper_preflight_current.json"
        self.datafeed_url = str(getattr(config, "datafeed_url", "http://127.0.0.1:8100"))
        self.launch = launch
        self.now = now or (lambda: datetime.now(timezone.utc))

    def start(self, plan: dict[str, Any]) -> str | None:
        """Start (or reuse) the backtest for this plan. Returns its key, or None for a non-grid plan."""

        if (plan.get("normalized_input") or {}).get("strategy_type") != "grid" or not (plan.get("risk") or {}).get("grid_rungs"):
            return None
        key = shape_key(plan, day=self.now().date().isoformat())
        current = self.read(key)
        if current and (current.get("status") != "running" or self.now() - datetime.fromisoformat(current["started_at"]) < STALE_RUN):
            return key
        self.folder.mkdir(parents=True, exist_ok=True)
        plan_path = self.folder / f"{key}.plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        environment = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "LANG", "TZ") if os.environ.get(name)}
        environment["PYTHONPATH"] = str(self.checkout)
        self.launch(
            [str(self.python), "-m", "pipelines.grid_backtest", "--plan-path", str(plan_path), "--output", str(self.path(key)),
             "--preflight", str(self.preflight), "--nautilus-python", str(self.python), "--datafeed-url", self.datafeed_url],
            cwd=str(self.checkout), env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._prune()
        return key

    def path(self, key: str) -> Path:
        return self.folder / f"{key}.json"

    def read(self, key: str) -> dict[str, Any] | None:
        if not KEY.match(key):
            return None
        try:
            return json.loads(self.path(key).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _prune(self) -> None:
        results = sorted(self.folder.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in [p for p in results if not p.name.endswith(".plan.json")][KEEP:]:
            path.unlink(missing_ok=True)
            (self.folder / f"{path.stem}.plan.json").unlink(missing_ok=True)


class Busy(Exception):
    """Another comparison is still running; the machine also runs Paper, so only one at a time."""


class Comparisons:
    """回测工作台 (#1335): one comparison at a time, each in its own process, one result file each.

    The same request on the same data day reuses its result. Nothing here can allow or block a
    start: the page shows numbers, and the only thing this starts is ``pipelines.backtest_compare``.
    """

    def __init__(self, config: Any, *, launch: Launch = subprocess.Popen, now: Callable[[], datetime] | None = None) -> None:
        self.folder = Path(config.db_path).parent / "comparisons"
        self.checkout = Path(config.trading_system_checkout)
        self.python = Path(config.nautilus_python)
        self.datafeed_url = str(getattr(config, "datafeed_url", "http://127.0.0.1:8100"))
        self.launch = launch
        self.now = now or (lambda: datetime.now(timezone.utc))

    def key(self, request: dict[str, Any]) -> str:
        day = self.now().date().isoformat()
        return hashlib.sha256(json.dumps({"request": request, "day": day}, sort_keys=True).encode()).hexdigest()[:24]

    def _running(self) -> list[str]:
        out = []
        for path in self.folder.glob("*.json"):
            if path.name.endswith(".request.json"):
                continue
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if result.get("status") == "running" and self.now() - datetime.fromisoformat(result["started_at"]) < STALE_RUN:
                out.append(path.stem)
        return out

    def start(self, request: dict[str, Any]) -> str:
        """`request` must already be checked (pipelines.backtest_compare.check). Raises Busy."""

        key = self.key(request)
        current = self.read(key)
        # A ready result is today's answer; an unavailable one (gap, datafeed blip, no contract)
        # is retried, so pressing the button again is a real retry.
        if current and (current.get("status") == "ready" or key in self._running()):
            return key
        self.folder.mkdir(parents=True, exist_ok=True)
        if self._running():
            raise Busy("已有一个回测在跑，等它跑完再开始")
        request_path = self.folder / f"{key}.request.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        # Written before launch so a second click in the same second already sees it running.
        self.path(key).write_text(json.dumps({"status": "running", "started_at": self.now().isoformat()}), encoding="utf-8")
        environment = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "LANG", "TZ") if os.environ.get(name)}
        environment["PYTHONPATH"] = str(self.checkout)
        self.launch(
            [str(self.python), "-m", "pipelines.backtest_compare", "--request", str(request_path), "--output", str(self.path(key)),
             "--datafeed-url", self.datafeed_url, "--nautilus-python", str(self.python)],
            cwd=str(self.checkout), env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._prune()
        return key

    def path(self, key: str) -> Path:
        return self.folder / f"{key}.json"

    def read(self, key: str) -> dict[str, Any] | None:
        if not KEY.match(key):
            return None
        try:
            return json.loads(self.path(key).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _prune(self) -> None:
        results = sorted((p for p in self.folder.glob("*.json") if not p.name.endswith(".request.json")), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in results[KEEP:]:
            path.unlink(missing_ok=True)
            (self.folder / f"{path.stem}.request.json").unlink(missing_ok=True)
