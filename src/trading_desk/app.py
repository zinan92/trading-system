"""FastAPI app. Orders are only ever placed from the 执行 button Park presses (Hyperliquid Testnet)."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import plans, review
from .config import Config
from .executor import ExecutionRefused, Executor
from .sources import Sources, http_json
from .store import Store

STATIC = Path(__file__).parent / "static"
KLINE_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>K 线日报 {title}</title>
<style>body{{font:15px/1.7 "Noto Sans SC","PingFang SC",system-ui,sans-serif;max-width:860px;margin:0 auto;padding:24px;color:#18221F;background:#F8FAF9}}
img{{max-width:100%;border-radius:6px}} h1,h2,h3{{font-family:"Noto Serif SC",serif}} table{{border-collapse:collapse}} td,th{{border:1px solid #CBD4D0;padding:4px 8px}}</style>
</head><body>{body}</body></html>"""
PAUSED_ROWS = re.compile(r"^\| (com\.[\w.-]+) \| ([^|]+) \| ([^|]+) \|$")


class JudgmentIn(BaseModel):
    asset: str = Field(min_length=1, max_length=20)
    direction: Literal["long", "short", "flat"]
    confidence: int = Field(ge=1, le=5)
    reason: str = Field(default="", max_length=2000)
    cited: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    action: Literal["recorded", "approved"] = "recorded"


class PlanIn(BaseModel):
    asset: str
    direction: Literal["long", "short", "flat"]


class NoteIn(BaseModel):
    asset: str
    body: str = Field(min_length=1, max_length=2000)


class AssetIn(BaseModel):
    coin: str = Field(min_length=1, max_length=20)
    news_queries: list[str] = Field(default_factory=list, max_length=10)


class NewsQueriesIn(BaseModel):
    news_queries: list[str] = Field(min_length=1, max_length=10)


class ExecuteIn(BaseModel):
    judgment_id: int
    shown_max_loss: float | None = None
    confirm_text: Literal["执行"]


def create_app(config: Config | None = None, sources: Sources | None = None, store: Store | None = None,
               executor: Executor | None = None) -> FastAPI:
    config = config or Config()
    sources = sources or Sources(config)
    store = store or Store(config.db_path)
    executor = executor or Executor(config, lambda url, body: http_json(url, body, timeout=150))
    app = FastAPI(title="Park 交易台", docs_url=None, redoc_url=None)

    def asset_or_404(key: str) -> dict[str, Any]:
        asset = store.asset(key.upper())
        if not asset:
            raise HTTPException(404, "没有这个品种")
        return asset

    def asset_state(asset: dict[str, Any]) -> dict[str, Any]:
        if asset["kind"] == "xau_paper":
            paper = sources.xau_paper()
            grid = paper["grid"] if paper.get("ok") else {"ok": False, "reason": paper.get("reason")}
            price = paper.get("price") if paper.get("ok") else None
        else:
            grid = sources.hl_grid(asset)
            price = grid.get("price") if grid.get("ok") else None
        if price is None:
            latest = sources.bars(asset, "1h", limit=2)
            price = latest["bars"][-1][4] if latest.get("ok") else None
        return {"grid": grid, "price": price}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "trading-desk", "submits_orders": True, "requires_human_press": True,
                "execution_scope": "hyperliquid.testnet"}

    # ---- assets ----------------------------------------------------------------
    @app.get("/api/assets")
    def assets() -> dict[str, Any]:
        return {"assets": [{k: a[k] for k in ("key", "label", "kind", "venue_profile_id", "instrument_id", "news_queries")} for a in store.assets()]}

    @app.get("/api/catalog")
    def catalog() -> dict[str, Any]:
        result = sources.catalog()
        have = {a["key"] for a in store.assets()}
        for item in result.get("instruments", []):
            item["added"] = str(item["coin"]).upper() in have
        return result

    @app.post("/api/assets")
    def add_asset(body: AssetIn) -> dict[str, Any]:
        found = next((i for i in sources.catalog().get("instruments", []) if str(i["coin"]).upper() == body.coin.upper()), None)
        if not found:
            raise HTTPException(422, "交易所目录里没有这个品种")
        try:
            return store.add_hl_asset(coin=found["coin"], instrument_id=found["instrument_id"], news_queries=body.news_queries or None)
        except ValueError:
            raise HTTPException(409, "这个品种已经在交易台里了") from None

    @app.put("/api/assets/{key}/news")
    def update_news(key: str, body: NewsQueriesIn) -> dict[str, Any]:
        asset_or_404(key)
        try:
            return store.update_asset_news(key.upper(), body.news_queries)
        except ValueError:
            raise HTTPException(422, "至少留一个新闻关键词") from None

    @app.delete("/api/assets/{key}")
    def remove_asset(key: str) -> dict[str, Any]:
        asset_or_404(key)
        try:
            store.remove_asset(key.upper())
        except ValueError:
            raise HTTPException(409, "至少要保留一个品种") from None
        return {"ok": True, "note": "只从交易台移除，交易所上的挂单和持仓不受影响。"}

    # ---- accounts / desk ---------------------------------------------------------
    @app.get("/api/accounts")
    def accounts() -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            hl, xau = pool.submit(sources.hl_account), pool.submit(sources.xau_paper)
            hl_account, paper = hl.result(), xau.result()
        return {"accounts": [
            hl_account if hl_account.get("ok") else {**hl_account, "venue": "Hyperliquid 测试盘账户"},
            paper["account"] if paper.get("ok") else {"ok": False, "venue": "Binance 纸面盘 · 黄金网格", "reason": paper.get("reason")},
        ]}

    @app.get("/api/desk/{key}")
    def desk(key: str) -> dict[str, Any]:
        asset = asset_or_404(key)
        state = asset_state(asset)
        today = datetime.now(timezone.utc).astimezone().date().isoformat()
        mine = store.judgments(asset["key"], limit=50)
        latest = next((j for j in mine if _local_date(j["created_at"]) == today), None)
        grid = state["grid"]
        runs = store.executions(asset["key"], limit=5)
        steps = {
            "look": True, "judge": latest is not None, "plan": bool(latest and latest.get("plan")),
            "approve": bool(latest and latest.get("action") == "approved"),
            "watch": bool(grid.get("ok") and grid.get("status") not in {"terminal", "TERMINAL", "stopped_by_operator"} and not grid.get("sealed")),
            "review": any(j.get("resolved_at") for j in mine),
        }
        venue = {"hl_testnet": ("Hyperliquid Testnet", "测试盘（假钱）"), "xau_paper": ("Binance 纸面盘", "纸面模拟")}[asset["kind"]]
        return {"asset": asset["key"], "meta": {"label": asset["label"], "venue": venue[0], "money": venue[1], "kind": asset["kind"]},
                "price": state["price"], "grid": grid, "today_judgment": latest, "steps": steps,
                "notes": store.notes(asset["key"], limit=10), "executions": runs, "kline": sources.kline_view(asset),
                "news_queries": asset["news_queries"]}

    @app.get("/api/news/{key}")
    def news(key: str) -> dict[str, Any]:
        return sources.news(asset_or_404(key))

    @app.get("/api/bars/{key}")
    def bars(key: str, tf: Literal["1h", "4h", "1d"] = "4h") -> dict[str, Any]:
        return sources.bars(asset_or_404(key), tf, limit=60)

    @app.post("/api/plan")
    def plan(body: PlanIn) -> dict[str, Any]:
        asset = asset_or_404(body.asset)
        state = asset_state(asset)
        return plans.build_plan(asset["kind"], body.direction, state["price"], state["grid"])

    @app.post("/api/judgments")
    def add_judgment(body: JudgmentIn) -> dict[str, Any]:
        asset = asset_or_404(body.asset)
        state = asset_state(asset)
        built = plans.build_plan(asset["kind"], body.direction, state["price"], state["grid"])
        if body.action == "approved" and built.get("kind") == "unavailable":
            raise HTTPException(409, "现价读不到，计划不完整，暂时不能批准。可以先只记录判断。")
        saved = store.add_judgment(asset=asset["key"], direction=body.direction, confidence=body.confidence,
                                   reason=body.reason, cited=body.cited, price_at=state["price"], plan=built, action=body.action)
        if body.action == "approved" and built.get("kind") == "new" and built.get("executable"):
            try:
                preview = executor.preview(asset, built)
            except ExecutionRefused as exc:
                preview = {"execution_ready": False, "blockers": [str(exc)]}
            except Exception as exc:  # noqa: BLE001 - preview failure must not lose the recorded judgment
                preview = {"execution_ready": False, "blockers": [f"预览失败：{type(exc).__name__}"]}
            store.log_execution(asset=asset["key"], stage="preview", judgment_id=saved["id"], preview_digest=preview.get("preview_digest"), detail=preview)
            saved["preview"] = preview
            saved["handoff"] = ("预览通过：以下是交易所会挂的真实价位。确认没问题再按「执行」，不按不会下单。" if preview.get("execution_ready")
                                else "预览没有通过，不能执行：" + "；".join(map(str, preview.get("blockers") or [])))
        elif body.action == "approved":
            saved["handoff"] = "已记录批准。这份计划不需要下单（沿用现有网格、观望，或纸面盘暂不支持执行）。"
        else:
            saved["handoff"] = "判断已记录，到期后自动出现在复盘里。"
        return saved

    @app.post("/api/execute")
    def execute(body: ExecuteIn) -> dict[str, Any]:
        judgment = store.judgment(body.judgment_id)
        if not judgment or judgment.get("action") != "approved" or not judgment.get("plan"):
            raise HTTPException(409, "只有已批准的计划才能执行")
        if any(e["stage"] in {"executed", "execute_started"} for e in store.executions(judgment["asset"], limit=50) if e["judgment_id"] == judgment["id"]):
            raise HTTPException(409, "这份计划已经执行过了")
        asset = asset_or_404(judgment["asset"])
        store.log_execution(asset=asset["key"], stage="execute_started", judgment_id=judgment["id"], preview_digest=None, detail={"pressed_at": datetime.now(timezone.utc).isoformat()})
        try:
            result = executor.execute(asset, judgment["plan"], shown_max_loss=body.shown_max_loss, judgment_id=judgment["id"])
        except ExecutionRefused as exc:
            store.log_execution(asset=asset["key"], stage="refused", judgment_id=judgment["id"], preview_digest=None, detail={"reason": str(exc), **exc.detail})
            raise HTTPException(409, str(exc)) from None
        store.log_execution(asset=asset["key"], stage="executed" if result.get("started") else "execute_failed", judgment_id=judgment["id"],
                            preview_digest=(result.get("preview") or {}).get("preview_digest"), detail=result)
        return {**result, "message": "网格已经挂上测试盘，系统接管盯盘。" if result.get("started") else f"下单没有完成（{result.get('status')}），请看系统页记录。"}

    # ---- review / notes ------------------------------------------------------------
    @app.get("/api/review")
    def review_list(asset: str | None = None) -> dict[str, Any]:
        resolved_now = 0
        rows = store.judgments(asset.upper() if asset else None, limit=100)
        bars_cache: dict[str, list] = {}
        for row in rows:
            target = review.due(row, config.review_hours)
            if target is None:
                continue
            meta = store.asset(row["asset"])
            if meta is None:
                continue
            if row["asset"] not in bars_cache:
                bars_cache[row["asset"]] = sources.bars(meta, "1h", limit=300).get("bars") or []
            after = review.price_at(bars_cache[row["asset"]], target)
            if after is None:
                if review.out_of_window(bars_cache[row["asset"]], target):
                    store.resolve(row["id"], price_after=None, move_pct=None, outcome="unverifiable")
                    resolved_now += 1
                continue
            move = (after - float(row["price_at"])) / float(row["price_at"]) * 100
            store.resolve(row["id"], price_after=after, move_pct=round(move, 3), outcome=review.outcome(row["direction"], move))
            resolved_now += 1
        rows = store.judgments(asset.upper() if asset else None, limit=100) if resolved_now else rows
        done = [r for r in rows if r.get("outcome") in {"hit", "miss", "even"}]
        return {"items": rows, "review_hours": config.review_hours,
                "summary": {"resolved": len(done), "hits": sum(1 for r in done if r["outcome"] == "hit"),
                            "pending": sum(1 for r in rows if not r.get("outcome")),
                            "unverifiable": sum(1 for r in rows if r.get("outcome") == "unverifiable")}}

    @app.post("/api/notes")
    def add_note(body: NoteIn) -> dict[str, Any]:
        asset_or_404(body.asset)
        try:
            return store.add_note(asset=body.asset.upper(), body=body.body)
        except ValueError:
            raise HTTPException(422, "内容是空的") from None

    # ---- newsletters ------------------------------------------------------------------
    @app.get("/api/newsletters")
    def newsletters() -> dict[str, Any]:
        archive = sorted(config.morning_archive.glob("20??-??-??.html"), reverse=True)[:14] if config.morning_archive.exists() else []
        def stamp(path: Path) -> str | None:
            return datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M") if path.exists() else None
        kline = sorted(config.kline_archive.glob("20??-??-??-kline-daily-newsletter.md")) if config.kline_archive.exists() else []
        return {"morning": {"latest": stamp(newest_morning()), "archive": [p.stem for p in archive]},
                "kline": {"latest": stamp(kline[-1]) if kline else stamp(config.kline_latest_html)}, "weekly": {"latest": stamp(config.weekly_latest_html)}}

    def newest_morning() -> Path:
        dated = sorted(config.morning_archive.glob("20??-??-??.html")) if config.morning_archive.exists() else []
        return dated[-1] if dated else config.morning_latest

    @app.get("/newsletter-asset/kline/{name}")
    def kline_asset(name: str) -> FileResponse:
        if not re.fullmatch(r"[0-9a-f]{64}\.png", name):
            raise HTTPException(404, "没有这张图")
        path = config.kline_latest_html.parent / "snapshots" / name
        if not path.exists():
            raise HTTPException(404, "没有这张图")
        return FileResponse(path)

    @app.get("/newsletter/{name}", response_class=HTMLResponse)
    def newsletter(name: str) -> HTMLResponse:
        if name == "kline":
            dated = sorted(config.kline_archive.glob("20??-??-??-kline-daily-newsletter.md")) if config.kline_archive.exists() else []
            if dated:
                import markdown
                body = markdown.markdown(dated[-1].read_text(encoding="utf-8", errors="replace").split("---", 2)[-1], extensions=["tables"])
                body = re.sub(r'src="(?:\./)?snapshots/([0-9a-f]{64}\.png)"', r'src="/newsletter-asset/kline/\1"', body)
                return HTMLResponse(KLINE_PAGE.format(title=dated[-1].name[:10], body=body))
        if name == "morning":
            path = newest_morning()
        elif name == "kline":
            path = config.kline_latest_html
        elif name == "weekly":
            path = config.weekly_latest_html
        elif re.fullmatch(r"20\d\d-\d\d-\d\d", name):
            path = config.morning_archive / f"{name}.html"
        else:
            raise HTTPException(404, "没有这份日报")
        if not path.exists():
            return HTMLResponse(f"<p style='font-family:sans-serif;padding:24px'>这份日报还没有生成（{path.name}）。</p>", status_code=404)
        return HTMLResponse(path.read_text(encoding="utf-8", errors="replace"))

    # ---- system ---------------------------------------------------------------------
    @app.get("/api/system")
    def system() -> dict[str, Any]:
        checks = []
        def check(name: str, fn) -> None:
            try:
                ok, detail = fn()
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, type(exc).__name__
            checks.append({"name": name, "ok": ok, "detail": detail})
        first = store.assets()[0]
        check("新闻（Intel 8001）", lambda: (lambda r: (r.get("ok", False), r.get("reason") or f"{len(r.get('items', []))} 条"))(sources.news(first)))
        check("交易后台（Dashboard 8765）", lambda: (lambda r: (bool(r.get("schema_version")), f"纸面盘：{r.get('status')}"))(http_json(f"{config.dashboard_url}/api/park-paper/read-model", None, timeout=10)))
        check("测试盘 K 线（Hyperliquid）", lambda: (lambda r: (r["ok"], r.get("reason") or r.get("latest_timestamp")))(sources.bars(next((a for a in store.assets() if a["kind"] == "hl_testnet"), first), "1h", 2)))
        coordinator = executor.coordinator()
        scheduler = _json_last(config.paper_output / "testnet_automation" / "scheduler" / "current.json")
        checks.append({"name": "测试盘调度器", "ok": scheduler.get("status") in {"active", "idle", "awaiting_operator"},
                       "detail": f"{scheduler.get('status')} · {scheduler.get('blocker') or '无阻塞'}"})
        checks.append({"name": "测试盘网格协调器", "ok": coordinator.get("status") not in {"grid_blocked", "unknown"}, "detail": coordinator.get("status")})
        kline_dated = sorted(config.kline_archive.glob("20??-??-??-kline-daily-newsletter.md")) if config.kline_archive.exists() else []
        for label, path in (("今日晨报", newest_morning()), ("K 线日报", kline_dated[-1] if kline_dated else config.kline_latest_html)):
            fresh = path.exists() and (datetime.now().timestamp() - path.stat().st_mtime) < 36 * 3600
            checks.append({"name": label, "ok": fresh, "detail": datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M") if path.exists() else "没有文件"})
        paused = []
        if config.paused_manifest.exists():
            for line in config.paused_manifest.read_text(encoding="utf-8").splitlines():
                m = PAUSED_ROWS.match(line.strip())
                if m:
                    paused.append({"label": m.group(1), "what": m.group(2).strip(), "why": m.group(3).strip()})
        return {"checks": checks, "paused": paused, "restore": "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist",
                "executions": store.executions(None, limit=20)}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _json_last(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    data = data[-1] if isinstance(data, list) and data else data
    return data if isinstance(data, dict) else {}


def _local_date(iso: str) -> str:
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().date().isoformat()
