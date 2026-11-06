"""回测证据包 (#1369): what produced a backtest number, and whether the run could have seen the future.

Every finished backtest result carries one ``evidence`` object:

- ``code``: the checkout's commit and whether it had uncommitted changes;
- ``data``: a sha256 over the bars exactly as used and over the funding rows, with counts;
- ``inputs``: digests of files or contracts that change results but are not parameters;
- ``params``: the parameters and the settings that change results, with one digest;
- ``checks``: future-data checks run on this run's own data, each ``ok`` / ``failed`` / ``未核实``;
- ``tested``: properties covered by unit tests rather than checked on this run;
- ``verdict``: ``ok`` only if every check ran and passed.

A check that cannot run says ``未核实`` and why. Nothing here reads credentials, account ids or
runtime ids: files are hashed, never copied. Nothing here allows or blocks anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Callable

SCHEMA = "backtest-evidence-v1"
OK, FAILED, UNVERIFIED = "ok", "failed", "未核实"
SELECTION_NOTE = "参数是在这段行情已经看得到之后定的，好结果可能只是挑对了参数；这一点回测本身无法核实"
BARS_FORMAT = "每根一行：开盘时间(ISO),开盘价,收盘价"

Run = Callable[..., Any]


def bars_sha256(bars: Iterable[Any]) -> str:
    """sha256 over the bars as used, one line each: ``opened_at,open,close`` (Python repr floats)."""

    digest = hashlib.sha256()
    for bar in bars:
        digest.update(f"{bar.opened_at.isoformat()},{bar.open!r},{bar.close!r}\n".encode())
    return "sha256:" + digest.hexdigest()


def value_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def code_identity(checkout: str | Path, *, run: Run = subprocess.run) -> dict[str, Any]:
    """The commit the backtest ran from. Untracked files do not count as changes; edits to tracked files do."""

    try:
        head = run(["git", "rev-parse", "HEAD"], cwd=str(checkout), capture_output=True, text=True, timeout=10, check=False)
        status = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=str(checkout), capture_output=True, text=True,
                     timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {"commit": None, "dirty": None, "reason": f"git 不可用：{type(error).__name__}"}
    commit = (head.stdout or "").strip()
    if head.returncode or len(commit) != 40:
        return {"commit": None, "dirty": None, "reason": "读不到提交号"}
    if status.returncode:
        return {"commit": commit, "dirty": None, "reason": "读不到有没有未提交的改动"}
    return {"commit": commit, "dirty": bool((status.stdout or "").strip()), "reason": None}


def data_evidence(*, source: str, instrument: str, bars: Sequence[Any], funding: Sequence[Mapping[str, Any]],
                  funding_from: str | None, report: Mapping[str, Any] | None) -> dict[str, Any]:
    report = report or {}
    return {
        "source": source,
        "instrument": instrument,
        "bars": len(bars),
        "expected_bars": report.get("expected_bars"),
        "first_bar": bars[0].opened_at.isoformat() if bars else None,
        "last_bar": bars[-1].opened_at.isoformat() if bars else None,
        "missing_minutes": report.get("missing_minutes"),
        "report_generated_at": report.get("generated_at"),
        "bars_sha256": bars_sha256(bars),
        "bars_format": BARS_FORMAT,
        "funding_rows": len(funding),
        "funding_from": funding_from,
        "funding_sha256": value_sha256(list(funding)),
    }


def params_evidence(values: Mapping[str, Any]) -> dict[str, Any]:
    return {"digest": value_sha256(dict(values)), "values": dict(values)}


def _at(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _check(name: str, label: str, status: str, detail: str) -> dict[str, Any]:
    return {"name": name, "label": label, "status": status, "detail": detail}


def history_check(*, last_bar: datetime | None, run_started: datetime | None) -> dict[str, Any]:
    label = "数据在回测开始前就已收盘"
    if last_bar is None or run_started is None:
        return _check("history_before_run", label, UNVERIFIED, "没有数据或没有开始时间")
    closed = last_bar + timedelta(minutes=1)
    if closed <= run_started:
        return _check("history_before_run", label, OK, f"最后一根 {closed.isoformat()} 收盘，回测 {run_started.isoformat()} 开始")
    return _check("history_before_run", label, FAILED, f"最后一根 {closed.isoformat()} 收盘，晚于回测开始 {run_started.isoformat()}")


def window_checks(results: Sequence[Mapping[str, Any]], *, length: timedelta, fill_word: str = "成交") -> list[dict[str, Any]]:
    """Run on each window's own record. A window without the fields needed makes that check 未核实.

    ``closed_at`` is the time of the last price the window saw (a bar's close is known when the bar ends).
    """

    def run(name: str, label: str, judge: Callable[[Mapping[str, Any]], str | None], needs: tuple[str, ...]) -> dict[str, Any]:
        if not results:
            return _check(name, label, UNVERIFIED, "没有窗口")
        missing = [i for i, row in enumerate(results) if any(key not in row for key in needs)]
        if missing:
            return _check(name, label, UNVERIFIED, f"{len(missing)} / {len(results)} 段没有记录所需时间")
        problems = [(i, problem) for i, row in enumerate(results) if (problem := judge(row))]
        if problems:
            first, problem = problems[0]
            return _check(name, label, FAILED, f"{len(problems)} / {len(results)} 段不符合；第 {first + 1} 段：{problem}")
        return _check(name, label, OK, f"{len(results)} 段全部符合")

    def bars_inside(row: Mapping[str, Any]) -> str | None:
        start, last = _at(row["started_at"]), _at(row["last_bar_at"])
        return None if start and last and start <= last < start + length else f"最后一根 {row['last_bar_at']} 超出 {row['started_at']} 起的 {length.days} 天"

    def fills_inside(row: Mapping[str, Any]) -> str | None:
        if row.get("fill_times_complete") is False:
            return f"有{fill_word}没有时间"
        first, last = _at(row["first_fill_at"]), _at(row["last_fill_at"])
        if first is None and last is None:
            return None
        start, end = _at(row["started_at"]), _at(row["closed_at"])
        # A window's own close is stamped one second after the last price it saw; its fill may follow by microseconds.
        if start and end and first and last and start <= first and last <= end + timedelta(seconds=2):
            return None
        return f"{fill_word}在 {row['first_fill_at']} 到 {row['last_fill_at']}，窗口是 {row['started_at']} 到 {row['closed_at']}"

    def funding_inside(row: Mapping[str, Any]) -> str | None:
        first, last = _at(row["funding_first_at"]), _at(row["funding_last_at"])
        if first is None and last is None:
            return None
        start, end = _at(row["started_at"]), _at(row["closed_at"])
        if start and end and first and last and start <= first and last <= end:
            return None
        return f"计入的资金费在 {row['funding_first_at']} 到 {row['funding_last_at']}，窗口是 {row['started_at']} 到 {row['closed_at']}"

    def funded_after_position(row: Mapping[str, Any]) -> str | None:
        funded, filled = _at(row["first_funded_at"]), _at(row["first_fill_at"])
        if funded is None:
            return None
        if filled is not None and filled < funded:
            return None
        return f"{row['first_funded_at']} 的资金费算在了 {row['first_fill_at'] or '没有'} 才有的仓位上"

    return [
        run("bars_inside_window", "每段只用自己 7 天内的行情", bars_inside, ("started_at", "last_bar_at")),
        run("fills_inside_window", f"每笔{fill_word}都在自己那段之内", fills_inside, ("started_at", "closed_at", "first_fill_at", "last_fill_at")),
        run("funding_inside_window", "计入的资金费都在自己那段之内", funding_inside, ("started_at", "closed_at", "funding_first_at", "funding_last_at")),
        run("funding_after_position", "资金费只算在已经有的仓位上", funded_after_position, ("first_funded_at", "first_fill_at")),
    ]


def verdict(checks: Sequence[Mapping[str, Any]]) -> str:
    statuses = [row["status"] for row in checks]
    if FAILED in statuses:
        return FAILED
    if statuses and all(status == OK for status in statuses):
        return OK
    return UNVERIFIED


TESTED = [
    {"label": "把后面的行情换掉，前面的结果不变", "test": "tests/test_backtest_evidence.py"},
    {"label": "一根 K 线的收盘价要等它收盘才看得到", "test": "tests/test_grid_backtest.py"},
]


def pack(*, code: Mapping[str, Any], data: Mapping[str, Any] | None, params: Mapping[str, Any],
         inputs: Mapping[str, Any] | None = None, checks: Sequence[Mapping[str, Any]] = (), reason: str | None = None) -> dict[str, Any]:
    """``checks`` are the future-data checks of this run; with none (the run never got data) the verdict is 未核实."""

    return {
        "schema_version": SCHEMA,
        "code": dict(code),
        "data": dict(data) if data else None,
        "inputs": dict(inputs or {}),
        "params": params_evidence(params),
        "checks": [dict(row) for row in checks],
        "tested": TESTED,
        "verdict": verdict(checks),
        "reason": reason,
        "notes": [SELECTION_NOTE],
    }
