"""FastAPI app: read-through panels plus Park's judgment log. Nothing here submits orders."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import plans, review
from .config import Config
from .sources import ASSETS, Sources
from .store import Store

STATIC = Path(__file__).parent / "static"
Asset = Literal["BTC", "XAU"]


class JudgmentIn(BaseModel):
    asset: Asset
    direction: Literal["long", "short", "flat"]
    confidence: int = Field(ge=1, le=5)
    reason: str = Field(default="", max_length=2000)
    cited: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    action: Literal["recorded", "approved"] = "recorded"


class PlanIn(BaseModel):
    asset: Asset
    direction: Literal["long", "short", "flat"]


class NoteIn(BaseModel):
    asset: Asset
    body: str = Field(min_length=1, max_length=2000)


def create_app(config: Config | None = None, sources: Sources | None = None, store: Store | None = None) -> FastAPI:
    config = config or Config()
    sources = sources or Sources(config)
    store = store or Store(config.db_path)
    app = FastAPI(title="Park 交易台", docs_url=None, redoc_url=None)

    def asset_state(asset: str) -> dict[str, Any]:
        if asset == "BTC":
            grid = sources.btc_grid()
            return {"grid": grid, "price": grid.get("price") if grid.get("ok") else None}
        paper = sources.xau_paper()
        if not paper.get("ok"):
            return {"grid": {"ok": False, "reason": paper.get("reason")}, "price": None}
        return {"grid": paper["grid"], "price": paper.get("price")}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "trading-desk", "submits_orders": False}

    @app.get("/api/accounts")
    def accounts() -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            btc = pool.submit(sources.btc_account)
            xau = pool.submit(sources.xau_paper)
            btc_account, paper = btc.result(), xau.result()
        return {"accounts": [
            btc_account if btc_account.get("ok") else {**btc_account, "venue": "Hyperliquid Testnet · BTC 网格"},
            paper["account"] if paper.get("ok") else {"ok": False, "venue": "Binance 纸面盘 · 黄金网格", "reason": paper.get("reason")},
        ]}

    @app.get("/api/desk/{asset}")
    def desk(asset: Asset) -> dict[str, Any]:
        state = asset_state(asset)
        today = datetime.now(timezone.utc).astimezone().date().isoformat()
        mine = store.judgments(asset, limit=50)
        todays = [j for j in mine if _local_date(j["created_at"]) == today]
        latest = todays[0] if todays else None
        grid = state["grid"]
        steps = {
            "look": True,
            "judge": latest is not None,
            "plan": bool(latest and latest.get("plan")),
            "approve": bool(latest and latest.get("action") == "approved"),
            "watch": bool(grid.get("ok") and grid.get("status") not in {"terminal", "TERMINAL", "stopped_by_operator"}),
            "review": any(j.get("resolved_at") for j in mine),
        }
        return {"asset": asset, "meta": {k: v for k, v in ASSETS[asset].items() if k not in {"news_queries", "primary"}},
                "price": state["price"], "grid": grid, "today_judgment": latest, "steps": steps,
                "notes": store.notes(asset, limit=10)}

    @app.get("/api/news/{asset}")
    def news(asset: Asset) -> dict[str, Any]:
        return sources.news(asset)

    @app.get("/api/bars/{asset}")
    def bars(asset: Asset, tf: Literal["1h", "4h", "1d"] = "4h") -> dict[str, Any]:
        return sources.bars(asset, tf, limit=60)

    @app.post("/api/plan")
    def plan(body: PlanIn) -> dict[str, Any]:
        state = asset_state(body.asset)
        return plans.build_plan(body.asset, body.direction, state["price"], state["grid"])

    @app.post("/api/judgments")
    def add_judgment(body: JudgmentIn) -> dict[str, Any]:
        state = asset_state(body.asset)
        built = plans.build_plan(body.asset, body.direction, state["price"], state["grid"])
        if body.action == "approved" and built.get("kind") == "unavailable":
            raise HTTPException(409, "现价读不到，计划不完整，暂时不能批准。可以先只记录判断。")
        saved = store.add_judgment(asset=body.asset, direction=body.direction, confidence=body.confidence,
                                   reason=body.reason, cited=body.cited, price_at=state["price"],
                                   plan=built, action=body.action)
        saved["handoff"] = (
            "已批准并交给执行员：按这份计划走 Dashboard 预览 → 确认，结果会回到本页「盯」。本页没有下单。"
            if body.action == "approved" else "判断已记录，到期后自动出现在复盘里。"
        )
        return saved

    @app.get("/api/review")
    def review_list(asset: Asset | None = None) -> dict[str, Any]:
        resolved_now = 0
        rows = store.judgments(asset, limit=100)
        bars_cache: dict[str, list] = {}
        for row in rows:
            target = review.due(row, config.review_hours)
            if target is None:
                continue
            if row["asset"] not in bars_cache:
                got = sources.bars(row["asset"], "1h", limit=300)
                bars_cache[row["asset"]] = got.get("bars") or []
            after = review.price_at(bars_cache[row["asset"]], target)
            if after is None:
                continue
            move = (after - float(row["price_at"])) / float(row["price_at"]) * 100
            store.resolve(row["id"], price_after=after, move_pct=round(move, 3), outcome=review.outcome(row["direction"], move))
            resolved_now += 1
        rows = store.judgments(asset, limit=100) if resolved_now else rows
        done = [r for r in rows if r.get("outcome")]
        hits = sum(1 for r in done if r["outcome"] == "hit")
        return {"items": rows, "review_hours": config.review_hours,
                "summary": {"resolved": len(done), "hits": hits, "pending": sum(1 for r in rows if not r.get("outcome"))}}

    @app.post("/api/notes")
    def add_note(body: NoteIn) -> dict[str, Any]:
        try:
            return store.add_note(asset=body.asset, body=body.body)
        except ValueError:
            raise HTTPException(422, "内容是空的") from None

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _local_date(iso: str) -> str:
    at = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return at.astimezone().date().isoformat()
