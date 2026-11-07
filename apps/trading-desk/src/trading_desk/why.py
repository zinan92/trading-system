"""这一笔为什么 (#1359): one chain for a past run or a desk judgment, built from records that already exist.

Pure read. The records join on plan_digest (Paper: plans, confirmations, lifecycle, executions.
Testnet: previews, confirmations, automation events, lifecycle state); the desk's own executions
join to them on preview_digest. A record that should exist and does not is `unknown` and is named;
nothing is inferred or borrowed from today's state. Only named fields are read, so account
fingerprints, runtime ids and user ids stay out. These are Paper or Testnet records, never proof of
a venue action.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .review_timeline import REASONS, TESTNET_STATUS, _at, _coin, _json, _jsonl, _latest, _paper_fills, _testnet_fills

SCHEMA = "why-this-trade-v1"
OK, ATTENTION, FAILED, UNKNOWN, NOT_REACHED = "ok", "attention", "failed", "unknown", "not_reached"
NOTE = "这些是 Paper 或 Testnet 的记录，只说明系统当时记下了什么，不是交易所动作的证明。"
AI_WINDOW = timedelta(hours=36)
SIDE = {"long": "看多", "short": "看空", "flat": "观望", "wait": "观望"}
PLAN_STOPS = {"hold": "观望，不开新单", "keep": "沿用正在跑的网格", "record_only": "这个品种只记判断"}


def step(key: str, label: str, status: str, summary: str, *, at: Any = None, source: str | None = None,
         facts: Iterable[tuple[str, Any]] = ()) -> dict[str, Any]:
    return {"key": key, "label": label, "status": status, "summary": summary, "at": at, "source": source,
            "facts": [{"label": name, "value": str(value)} for name, value in facts if value not in (None, "", [])]}


def _finish(subject: dict[str, Any], *, asset: Any, environment: str, label: str, steps: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    problem = next((row for row in steps if row["status"] in {FAILED, UNKNOWN}), None)
    return {"schema_version": SCHEMA, "subject": subject, "asset": asset, "environment": environment, "label": label,
            "status": "broken" if problem else "attention" if any(row["status"] == ATTENTION for row in steps) else "clean",
            "first_problem": {key: problem[key] for key in ("key", "label", "summary")} if problem else None,
            "identity": identity, "steps": steps, "note": NOTE}


def _rows(root: Path, name: str, **match: Any) -> tuple[list[dict[str, Any]], bool]:
    rows = _jsonl(root / name)
    if rows is None:
        return [], False
    return [row for row in rows if all(row.get(key) == value for key, value in match.items())], True


def _list(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _when(value: Any) -> str | None:
    """Paper stamps confirmations in epoch seconds; everything else is ISO."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return str(value) if value else None


def _gap(key: str, label: str, what: str, name: str, found: bool) -> dict[str, Any]:
    return step(key, label, UNKNOWN, f"没有{what}" if found else f"读不到 {name}，不知道{what}有没有发生", source=name)


def ai_step(judgments: list[dict[str, Any]], asset: Any, start: Any) -> dict[str, Any]:
    at = _at(start)
    calls = [(moment, row) for row in judgments if row.get("author") == "ai" and row.get("asset") == asset
             and (moment := _at(row.get("created_at"))) and at and timedelta(0) <= at - moment <= AI_WINDOW]
    if not calls:
        return step("ai", "AI 建议", NOT_REACHED, "开始前 36 小时内没有这个品种的 AI 建议")
    call = max(calls, key=lambda item: item[0])[1]
    return _ai_call(call, f"按时间对上的最近一条：{SIDE.get(call.get('direction'), call.get('direction'))}。这只说明时间相近，不代表这一轮照它做的。",
                    joined="时间")


def advice_step(origin: dict[str, Any] | None, judgments: list[dict[str, Any]], asset: Any, start: Any) -> dict[str, Any]:
    """By key when the judgment behind it recorded the advice it adopted (#1370); otherwise the time match, labelled so."""
    key = (origin or {}).get("advice")
    if not key:
        return ai_step(judgments, asset, start)
    call = next((row for row in judgments if row.get("id") == key.get("id") and row.get("author") == "ai"), None)
    then = f"{SIDE.get(key.get('direction'), key.get('direction'))}，{key.get('created_at')}"
    if call is None:
        return step("ai", "AI 建议", UNKNOWN, f"判断记下照的是 AI 建议 #{key.get('id')}（{then}），但找不到这条建议", source="judgments",
                    facts=(("对上方式", "编号"),))
    if call.get("created_at") != key.get("created_at"):
        return step("ai", "AI 建议", ATTENTION, f"照的是 AI 建议 #{key.get('id')}，当时是{then}；这条建议后来被刷新过，现在的内容不是当时那条",
                    at=key.get("created_at"), source="judgments", facts=(("对上方式", "编号"), ("刷新后", SIDE.get(call.get("direction"), call.get("direction")))))
    return _ai_call(call, f"Park 照 AI 建议 #{call['id']} 做的判断：{SIDE.get(call.get('direction'), call.get('direction'))}", joined="编号")


def _ai_call(call: dict[str, Any], summary: str, *, joined: str | None = None) -> dict[str, Any]:
    evidence = call.get("evidence") or {}
    warnings = [text for text, bad in ((f"理由里有 {len(evidence.get('unverified') or [])} 个数字在输入里找不到", evidence.get("unverified")),
                                       ("当时新闻是旧的", evidence.get("news_stale")), ("当时读不到 K 线日报", (evidence.get("kline") or {}).get("missing"))) if bad]
    model = evidence.get("model") or {}
    return step("ai", "AI 建议", ATTENTION if warnings else OK, "；".join([summary, *warnings]), at=call.get("created_at"), source="judgments",
                facts=(("对上方式", joined), ("信心", call.get("confidence")), ("理由", call.get("reason")), ("模型通道", model.get("channel")),
                       ("价格来源", (evidence.get("price") or {}).get("source"))))


def _paper_end(root: Path, session: str, seal: dict[str, Any] | None) -> dict[str, Any]:
    identity, found = _rows(root, "park_strategy/identity.jsonl", strategy_session_id=session)
    closed = next((row for row in identity if row.get("event") == "session_closed"), None)
    if seal:
        result = seal.get("result") or {}
        reconciliation = result.get("reconciliation") or {}
        reason = str(seal.get("terminal_key") or "").rsplit(":", 1)[-1]
        status = OK if reconciliation.get("status") == "ok" else ATTENTION
        return step("end", "结束", status, f"{REASONS.get(reason, reason or '原因没有记录')}，对账 {reconciliation.get('status') or '没有记录'}", at=seal.get("recorded_at"),
                    source="park_strategy/executions.jsonl", facts=(("结束时未平仓位", (reconciliation.get("counts") or {}).get("open_positions")),))
    if closed or not found:
        return _gap("end", "结束", "结束记录", "park_strategy/executions.jsonl", found)
    return step("end", "结束", NOT_REACHED, "这一轮还没结束")


def _paper(root: Path, session: str, judgments: list[dict[str, Any]], origins: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    raw_fills = _json(root / "dualtrack" / "nautilus_authoritative" / "fills" / f"park-{session}.json")
    if raw_fills is None:
        return None
    plans, plans_found = _rows(root, "park_strategy/plans.jsonl", strategy_session_id=session, event="plan_proposed")
    confirmed, confirmed_found = _rows(root, "park_strategy/confirmations.jsonl", strategy_session_id=session, event="confirmed")
    active, active_found = _rows(root, "park_strategy/lifecycle.jsonl", strategy_session_id=session, event="plan_activated")
    executions, executions_found = _rows(root, "park_strategy/executions.jsonl", strategy_session_id=session)
    plan, confirmation = (plans[-1] if plans else None), (confirmed[-1] if confirmed else None)
    market = (plan or {}).get("market") or {}
    entries = [row for row in executions if row.get("event") == "entry_submitted"]
    seal = next((row for row in executions if row.get("event") == "terminal_paused"), None)
    fills = _paper_fills(raw_fills)
    start = (plan or {}).get("created_at") or (fills[0]["ts"] if fills else None)
    steps = [
        advice_step(origins.get((plan or confirmation or {}).get("plan_digest")), judgments, "XAU", start),
        step("proposal", "提议", OK, f"来源 {plan.get('source') or '没有记录'}", at=plan.get("created_at"), source="park_strategy/plans.jsonl",
             facts=(("当时价格", market.get("price")), ("行情来源", market.get("source")), ("行情时间", market.get("observed_at")))) if plan
        else _gap("proposal", "提议", "提议记录", "park_strategy/plans.jsonl", plans_found),
        step("confirm", "确认", OK, f"已确认（{confirmation.get('execution_environment') or '环境没有记录'}）", at=_when(confirmation.get("confirmed_at")), source="park_strategy/confirmations.jsonl") if confirmation
        else _gap("confirm", "确认", "确认记录", "park_strategy/confirmations.jsonl", confirmed_found),
        step("activate", "启动", OK, f"计划已生效（{active[-1].get('state')}）", source="park_strategy/lifecycle.jsonl",
             facts=(("策略", active[-1].get("strategy_type")), ("方向", SIDE.get(active[-1].get("direction"), active[-1].get("direction"))))) if active
        else _gap("activate", "启动", "启动记录", "park_strategy/lifecycle.jsonl", active_found),
        step("orders", "下单", OK, f"提交了 {len(entries)} 笔开仓单", at=entries[0].get("recorded_at"), source="park_strategy/executions.jsonl") if entries
        else _gap("orders", "下单", "下单记录", "park_strategy/executions.jsonl", executions_found),
        step("fills", "成交", OK, f"{len(fills)} 笔成交", at=fills[0]["ts"], source="nautilus_authoritative/fills") if fills
        else step("fills", "成交", NOT_REACHED, "没有成交"),
        _paper_end(root, session, seal),
    ]
    digest = (plan or confirmation or {}).get("plan_digest")
    return _finish({"kind": "run", "id": f"paper:{session}"}, asset="XAU", environment="paper", label="黄金 Paper", steps=steps,
                   identity={"run_id": f"paper:{session}", "plan_digest": digest, "strategy_session_id": session, "activation_id": None, "release_sha": None, "market_source": market.get("source")})


def _testnet(root: Path, plan_id: str, judgments: list[dict[str, Any]], origins: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    state = _latest(_json(root / "dualtrack" / "grid_testnet_lifecycle" / f"{plan_id}.json"))
    if not state:
        return None
    digest = state.get("plan_digest")
    previews_raw = _json(root / "dashboard_control_plane" / "previews.json")
    confirmations_raw = _json(root / "dashboard_control_plane" / "confirmations.json")
    events_raw = _json(root / "testnet_automation" / "events.json")
    preview = next((row for row in reversed(_list(previews_raw)) if row.get("preview_digest") == digest), None)
    confirmation = next((row for row in reversed(_list(confirmations_raw)) if row.get("plan_digest") == digest and row.get("status") == "confirmed"), None)
    activation = (confirmation or {}).get("activation_id")
    activated = next((row for row in _list(events_raw) if row.get("event") == "activated" and row.get("plan_digest") == digest), None)
    events, fills, orders = state.get("events") or [], _testnet_fills(state), state.get("orders") or []
    blocked = [row for row in events if row.get("event") in {"lifecycle_blocked", "ladder_incomplete"}]
    asset, status = _coin(state), str(state.get("status"))
    if preview is None:
        preview_step = _gap("preview", "预览", "预览记录", "dashboard_control_plane/previews.json", previews_raw is not None)
    elif preview.get("execution_ready") is True:
        preview_step = step("preview", "预览", OK, "预览通过", source="dashboard_control_plane/previews.json",
                            facts=(("交易所", preview.get("venue_profile_id")), ("品种", preview.get("instrument_id")), ("策略", preview.get("strategy_family"))))
    else:
        preview_step = step("preview", "预览", FAILED, "预览没通过，这一轮却启动了：" + "、".join(map(str, preview.get("blockers") or ["原因没有记录"])), source="dashboard_control_plane/previews.json")
    if status in {"terminal", "stopped_by_operator"}:
        reason = REASONS.get(str(state.get("terminal_reason")), TESTNET_STATUS.get(status, status))
        end = step("end", "结束", OK, reason, at=events[-1].get("timestamp") if events else None, source="grid_testnet_lifecycle")
    elif status.startswith("blocked"):
        end = step("end", "结束", FAILED, f"{TESTNET_STATUS.get(status, status)}：{state.get('blocker') or '原因没有记录'}", at=events[-1].get("timestamp") if events else None, source="grid_testnet_lifecycle")
    else:
        end = step("end", "结束", NOT_REACHED, f"这一轮还没结束（{status}）")
    steps = [
        advice_step(origins.get(digest), judgments, asset, state.get("created_at")),
        preview_step,
        step("confirm", "确认", OK, f"{confirmation.get('operator_id') or '操作人没有记录'} 已确认", at=confirmation.get("confirmed_at"), source="dashboard_control_plane/confirmations.json") if confirmation
        else _gap("confirm", "确认", "确认记录", "dashboard_control_plane/confirmations.json", confirmations_raw is not None),
        step("activate", "启动", OK, "协调器已接手", at=activated.get("occurred_at"), source="testnet_automation/events.json") if activated
        else _gap("activate", "启动", "启动记录", "testnet_automation/events.json", events_raw is not None),
        step("orders", "下单", ATTENTION if blocked else OK,
             f"挂了 {len(orders)} 笔单" + (f"；被门禁拦过 {len(blocked)} 次，第一次：{blocked[0].get('reason') or '原因没有记录'}" if blocked else ""),
             at=blocked[0].get("timestamp") if blocked else None, source="grid_testnet_lifecycle"),
        step("fills", "成交", OK, f"{len(fills)} 笔成交", at=fills[0]["ts"], source="grid_testnet_lifecycle") if fills else step("fills", "成交", NOT_REACHED, "没有成交"),
        end,
    ]
    return _finish({"kind": "run", "id": f"testnet:{plan_id}"}, asset=asset, environment="testnet", label=f"{asset} Testnet", steps=steps,
                   identity={"run_id": f"testnet:{plan_id}", "plan_digest": digest, "strategy_session_id": state.get("strategy_session_id"), "activation_id": activation,
                             "release_sha": (preview or {}).get("release_sha"), "market_source": None})


def explain_run(paper_output: Path, run_id: str, judgments: list[dict[str, Any]], *, origins: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """``origins``: plan digest -> the desk judgment that started it. A run started elsewhere, or before #1370, has none."""
    root = Path(paper_output)
    kind, _, key = run_id.partition(":")
    if not key or "/" in key or ".." in key:
        return None
    origins = origins or {}
    return _paper(root, key, judgments, origins) if kind == "paper" else _testnet(root, key, judgments, origins) if kind == "testnet" else None


def _run_of(root: Path, digest: Any) -> str | None:
    """The run a confirmed digest became, or None."""
    for path in sorted((root / "dualtrack" / "grid_testnet_lifecycle").glob("*.json")):
        state = _latest(_json(path))
        if state.get("plan_digest") == digest:
            return f"testnet:{path.stem}"
    active, _ = _rows(root, "park_strategy/lifecycle.jsonl", plan_digest=digest, event="plan_activated")
    return f"paper:{active[-1]['strategy_session_id']}" if active else None


def explain_judgment(paper_output: Path, judgment: dict[str, Any], executions: list[dict[str, Any]], judgments: list[dict[str, Any]]) -> dict[str, Any]:
    root = Path(paper_output)
    rows = sorted(executions, key=lambda row: row["id"])
    last = lambda stage: next((row for row in reversed(rows) if row["stage"] == stage), None)  # noqa: E731
    asset, plan, approved = judgment.get("asset"), judgment.get("plan") or {}, judgment.get("action") == "approved"
    side = SIDE.get(judgment.get("direction"), judgment.get("direction"))
    ai = (_ai_call(judgment, f"AI 判断：{side}") if judgment.get("author") == "ai" else advice_step(judgment, judgments, asset, judgment.get("created_at")))
    steps = [ai, step("judgment", "判断", OK, f"{'Park' if judgment.get('author') != 'ai' else 'AI'} {side}，{'已批准' if approved else '只记录'}", at=judgment.get("created_at"), source="judgments",
                      facts=(("信心", judgment.get("confidence")), ("理由", judgment.get("reason")), ("当时价格", judgment.get("price_at"))))]

    def stop(*keys: str) -> None:
        labels = {"preview": "预览", "press": "按下执行", "result": "下单结果", "run": "这一轮"}
        steps.extend(step(key, labels[key], NOT_REACHED, "没走到这一步") for key in keys)

    identity: dict[str, Any] = {"run_id": None, "plan_digest": None, "strategy_session_id": None, "activation_id": None, "release_sha": None, "market_source": None}

    def done(*keys: str) -> dict[str, Any]:
        stop(*keys)
        return _finish({"kind": "judgment", "id": judgment.get("id")}, asset=asset, environment="desk", label=f"{asset} 判断 #{judgment.get('id')}", steps=steps, identity=identity)

    if not approved:
        steps.append(step("plan", "计划", NOT_REACHED, "只记录了判断，没有批准计划"))
        return done("preview", "press", "result", "run")
    kind = plan.get("kind")
    if kind == "unavailable" or not plan:
        steps.append(step("plan", "计划", FAILED, str(plan.get("title") or "这条判断没有计划记录"), source="judgments"))
        return done("preview", "press", "result", "run")
    if kind != "new":
        steps.append(step("plan", "计划", OK, PLAN_STOPS.get(kind, str(plan.get("title") or kind)), source="judgments"))
        return done("preview", "press", "result", "run")
    if not plan.get("executable"):
        steps.append(step("plan", "计划", FAILED, f"{plan.get('title') or '新计划'}，但不能执行", source="judgments"))
        return done("preview", "press", "result", "run")
    steps.append(step("plan", "计划", OK, str(plan.get("title") or "新网格计划"), source="judgments"))

    preview = last("preview")
    if preview is None:
        steps.append(_gap("preview", "预览", "预览记录", "executions", True))
        return done("press", "result", "run")
    identity["plan_digest"] = preview.get("preview_digest")
    detail = preview.get("detail") or {}
    identity["release_sha"] = detail.get("release")
    if detail.get("execution_ready") is not True:
        steps.append(step("preview", "预览", FAILED, "预览没通过：" + "；".join(map(str, detail.get("blockers") or ["原因没有记录"])), at=preview.get("created_at"), source="executions"))
        return done("press", "result", "run")
    steps.append(step("preview", "预览", OK, "预览通过", at=preview.get("created_at"), source="executions", facts=(("最多亏损", detail.get("max_loss")),)))

    pressed = last("execute_started")
    if pressed is None:
        steps.append(step("press", "按下执行", NOT_REACHED, "还没按执行"))
        return done("result", "run")
    steps.append(step("press", "按下执行", OK, "Park 按下了执行", at=pressed.get("created_at"), source="executions"))

    refused, failed, executed = last("refused"), last("execute_failed"), last("executed")
    if refused:
        steps.append(step("result", "下单结果", FAILED, str((refused.get("detail") or {}).get("reason") or "被拒绝，原因没有记录"), at=refused.get("created_at"), source="executions"))
        return done("run")
    if failed:
        info = failed.get("detail") or {}
        codes = [row.get("reason_code") or row.get("status") for row in info.get("attempts") or []]
        steps.append(step("result", "下单结果", FAILED, f"没有完成（{info.get('status') or '状态没有记录'}）" + (f"：{codes[-1]}" if codes else ""), at=failed.get("created_at"), source="executions",
                          facts=(("尝试次数", len(codes)),)))
        return done("run")
    if executed is None:
        steps.append(step("result", "下单结果", UNKNOWN, "按了执行，但没有结果记录。可能中途崩溃，先看交易后台的真实状态", source="executions"))
        return done("run")
    info = executed.get("detail") or {}
    identity.update(activation_id=info.get("activation_id"), release_sha=info.get("release") or identity["release_sha"], plan_digest=executed.get("preview_digest") or identity["plan_digest"])
    steps.append(step("result", "下单结果", OK, f"已启动（{info.get('status')}）", at=executed.get("created_at"), source="executions"))
    identity["run_id"] = _run_of(root, identity["plan_digest"])
    steps.append(step("run", "这一轮", OK, "已对上这一轮的记录", source="plan_digest") if identity["run_id"]
                 else step("run", "这一轮", UNKNOWN, "下单成功了，但找不到这一轮的运行记录", source="plan_digest"))
    return done()
