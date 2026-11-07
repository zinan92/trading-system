"""#1378: what the desk does for each venue kind, recorded before the kind branches became venue modules.

The snapshot is the 回放对比 of #1360: the swap to a venue registry changes structure, not output.
Regenerate only for an intended behaviour change: ``DESK_SNAPSHOT_UPDATE=1 pytest tests/test_venue_characterization.py``.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from test_desk import PREVIEW_OK, client_for
from trading_desk import plans
from trading_desk.app import current_state
from trading_desk.executor import ExecutionRefused

SNAPSHOT = Path(__file__).with_name("venue_characterization.json")
KEYS = ("BTC", "XAU", "SILVER")
VOLATILE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:\d{2}|Z)?")


def _clean(value):
    """Times move between runs; everything else must match exactly."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in {"created_at", "pressed_at", "generated_at", "observed_at"}}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        return VOLATILE.sub("<time>", value)
    return value


def _refusal(call):
    try:
        return {"ok": call()}
    except ExecutionRefused as exc:
        return {"refused": str(exc), "detail": exc.detail}


def _record(config, sources, store, fetch, paper_output) -> dict:
    fetch.routes["/api/dashboard-control/preview"] = PREVIEW_OK
    fetch.routes["/api/dashboard-control/control"] = {"control": {"status": "stop_requested"}}
    fetch.routes["/api/dashboard-control/confirm"] = {"confirmation": {"status": "rejected", "blockers": ["fixture"]}}
    fetch.routes["/api/park-paper/ai-chat"] = {"reply": "ok", "preview": {"execution_ready": False, "blockers": ["fixture"]}}
    client, ex = client_for(config, sources, store, fetch)
    (paper_output / "testnet_automation").mkdir(parents=True, exist_ok=True)
    (paper_output / "testnet_automation" / "current.json").write_text(json.dumps({"status": "grid_running", "selected_instrument_id": "BTC-USD-PERP"}))
    out: dict = {"assets": client.get("/api/assets").json()}
    for key in KEYS:
        asset = store.asset(key)
        row: dict = {"kind": asset["kind"]}
        desk = client.get(f"/api/desk/{key}").json()
        row["desk"] = {k: desk[k] for k in ("meta", "price", "grid", "control")}
        row["state"] = current_state(sources, asset)
        row["bars"] = {tf: client.get(f"/api/bars/{key}?tf={tf}").json() for tf in ("1h", "4h")}
        row["plan"] = {d: client.post("/api/plan", json={"asset": key, "direction": d}).json() for d in ("long", "short", "flat")}
        row["template"] = asset["kind"] in plans.TEMPLATE
        row["plan_without_price"] = plans.build_plan(asset["kind"], "long", None, {}) if asset["kind"] != "watch" else plans.build_plan("watch", "long", None, {})
        fetch.calls.clear()
        built = row["plan"]["short"]
        row["preview"] = _refusal(lambda: ex.preview(asset, built))
        row["preview_calls"] = [(url.replace(config.dashboard_url, "<dash>"), body) for url, body in fetch.calls if "dashboard-control" in url or "park-paper" in url]
        row["execute"] = _refusal(lambda: ex.execute(asset, built, shown_max_loss=4.2, judgment_id=1))
        row["stop"] = (lambda r: (r.status_code, r.json()))(client.post("/api/grid/stop", json={"asset": key, "confirm_text": "停止"}))
        row["rearm"] = (lambda r: (r.status_code, r.json().get("detail") if r.status_code != 200 else r.json()["paused"]))(client.post("/api/grid/rearm-hold", json={"asset": key, "paused": True}))
        current = paper_output / "testnet_automation" / "current.json"
        before = current.read_text()
        current.write_text(json.dumps({"status": "idle"}))
        row["execute_idle"] = _refusal(lambda: ex.execute(asset, built, shown_max_loss=4.2, judgment_id=1))
        row["driver_calls"] = len(ex.driver_calls)
        current.write_text(before)
        out[key] = row
    out["system"] = [(c["name"], c["ok"]) for c in client.get("/api/system").json()["checks"]]
    return _clean(json.loads(json.dumps(out, ensure_ascii=False, default=str)))


def test_the_desk_does_the_same_for_every_kind(config, sources, store, fetch, paper_output) -> None:
    got = _record(config, sources, store, fetch, paper_output)
    if os.environ.get("DESK_SNAPSHOT_UPDATE") == "1":
        SNAPSHOT.write_text(json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert got == json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_price_alerts_only_watch_testnet_grids(config, sources, store, fetch) -> None:
    from trading_desk.notify import Notifier
    seen = []
    sources.hl_grid = lambda asset: (seen.append(asset["key"]), {"ok": False})[1]
    notifier = Notifier.__new__(Notifier)
    notifier.store, notifier.sources = store, sources
    notifier._prices({"sent": {}}, None)
    assert seen == ["BTC", "ETH"]
