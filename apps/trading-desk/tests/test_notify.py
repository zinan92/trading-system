from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from trading_desk.notify import Notifier

BJ = timezone(timedelta(hours=8))


class StubWatch:
    def __init__(self, data):
        self.data = data

    def current(self):
        return self.data


class StubAdvisor:
    def __init__(self, calls):
        self.calls = calls

    def today(self, key):
        return self.calls.get(key)


def notifier(config, store, sources, watch, clock, sent):
    return Notifier(config, store, sources, watch, StubAdvisor({"BTC": {"direction": "flat", "reason": "美联储决议前观望"}}),
                    queue=lambda key, kind, text: sent.append((key, kind, text)) or {}, now=lambda: clock[0])


FOMC = {"ref": "event:fomc", "kind": "event", "title": "美联储利率决议", "at": "2026-09-17T02:00:00+08:00", "why": "市场押注降息", "volatility": "高"}
FLASH = {"ref": "news:9", "kind": "breaking", "title": "霍尔木兹石油流量骤降", "at": "2026-09-15T14:25:00+08:00", "why": "能源冲击", "volatility": "高"}


def test_one_morning_digest_after_ten_with_three_things_ai_scores_and_nudge(config, sources, store, fetch):
    sent, clock = [], [datetime(2026, 9, 15, 9, 50, tzinfo=BJ)]
    watch = StubWatch({"items": [FOMC, FLASH], "replaced": []})
    park = store.add_judgment(asset="XAU", direction="long", confidence=3, reason="", cited=[], price_at=100.0, plan=None, action="recorded",
                              created_at="2026-09-10T00:00:00+00:00")
    store.resolve(park["id"], price_after=101.0, move_pct=1.0, outcome="hit")
    n = notifier(config, store, sources, watch, clock, sent)
    assert n.run() == []  # before 10:00
    clock[0] = datetime(2026, 9, 15, 10, 5, tzinfo=BJ)
    assert n.run() == ["digest:2026-09-15"]
    key, kind, text = sent[0]
    assert key == "desk:digest:2026-09-15" and kind == "desk_digest"
    assert "美联储利率决议（周四 02:00 · 还有 1 天 15 小时）" in text
    assert "BTC：观望，美联储决议前观望" in text and "黄金：还没生成" in text
    assert "你 XAU 做多 → 说中（+1.00%）" in text
    assert "今天还没记判断：BTC、黄金" in text and "K 线日报今天没出" in text
    clock[0] += timedelta(minutes=20)
    assert n.run() == [] and len(sent) == 1  # once a day


def test_event_two_hours_ahead_breaking_throttled_and_price_near_the_grid(config, sources, store, fetch):
    sent, clock = [], [datetime(2026, 9, 17, 0, 10, tzinfo=BJ)]
    state = config.db_path.parent / "notify-state.json"
    state.write_text(json.dumps({"sent": {"digest:2026-09-17": "2026-09-17T00:00:00+08:00"}}))
    watch = StubWatch({"items": [FOMC, FLASH], "replaced": ["news:9"]})
    n = notifier(config, store, sources, watch, clock, sent)
    first = n.run()
    assert "event:event:fomc" in first and "breaking:news:9" in first
    event_text = next(t for k, _, t in sent if k == "desk:event:event:fomc")
    assert "美联储利率决议" in event_text and "暂停补单" in event_text
    watch.data = {"items": [FOMC, {**FLASH, "ref": "news:10", "title": "又一条突发"}], "replaced": ["news:10"]}
    clock[0] += timedelta(minutes=40)
    assert n.run() == []  # second flash inside two hours waits
    clock[0] += timedelta(hours=2)
    assert n.run() == ["breaking:news:10"]
    # grid fixture: upper 76816.5, hard stop 72000, price 77862.5 (1.36% above the top): no alert yet
    assert not any(k.startswith("desk:price") for k, _, _ in sent)
    lifecycle = next((config.paper_output / "dualtrack" / "grid_testnet_lifecycle").glob("*.json"))
    rows = json.loads(lifecycle.read_text())
    rows[-1]["last_market_price"] = 77000.0
    lifecycle.write_text(json.dumps(rows))
    clock[0] += timedelta(minutes=20)
    assert n.run() == ["price:BTC:top:2026-09-17"]
    assert "买单可能开始成交" in sent[-1][2]


def test_nothing_is_sent_without_the_telegram_identity(config, sources, store, fetch):
    n = Notifier(config, store, sources, StubWatch({"items": []}), StubAdvisor({}), queue=None,
                 now=lambda: datetime(2026, 9, 15, 10, 30, tzinfo=BJ))
    assert n.run() == []
