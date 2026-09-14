"""Daily review card: today's reading vs. Park's call vs. what the price did, sized for a phone video frame."""
from __future__ import annotations

from html import escape
from typing import Any

DIRECTION = {"long": "看多", "short": "看空", "flat": "观望"}
OUTCOME = {"hit": ("对了", "hit"), "miss": ("错了", "miss"), "even": ("持平", "even"), "unverifiable": ("无法核对", "even")}


def change_24h(bars: list[list[Any]]) -> tuple[float | None, float | None]:
    """Last close and its % change against the close 24 one-hour bars earlier."""
    closes = [float(b[4]) for b in bars if len(b) >= 5 and b[4] is not None]
    if len(closes) < 25:
        return (closes[-1] if closes else None), None
    return closes[-1], (closes[-1] - closes[-25]) / closes[-25] * 100


def first_sentence(text: str | None) -> str:
    text = (text or "").strip()
    for mark in "。；":
        if mark in text:
            return text.split(mark)[0] + "。"
    return text


def render(date: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    blocks = []
    for row in rows:
        price, move = row["price"], row["move"]
        tone = "up" if (move or 0) > 0 else "down" if (move or 0) < 0 else "flat"
        move_text = f"{move:+.2f}%" if move is not None else "—"
        judgment = row.get("judgment")
        if judgment:
            call = f"<b>{DIRECTION.get(judgment['direction'], judgment['direction'])}</b> · 信心 {judgment.get('confidence') or '—'}/5"
            reason = escape(judgment.get("reason") or "")
        else:
            call, reason = "<span class='muted'>今天还没下判断</span>", ""
        last = row.get("last")
        verdict = ""
        if last:
            label, cls = OUTCOME.get(str(last.get("outcome")), ("待核对", "even"))
            verdict = (f"<div class='verdict {cls}'>上次 {DIRECTION.get(last['direction'], last['direction'])}"
                       f" → {escape(label)}{'（' + format(last['move_pct'], '+.2f') + '%）' if last.get('move_pct') is not None else ''}</div>")
        blocks.append(f"""
<section class="asset">
  <header><h2>{escape(row['label'])}</h2><div class="px"><span class="num">{f'{price:,.2f}' if price is not None else '—'}</span><span class="chg {tone} num">{move_text}</span></div></header>
  <div class="k">K 线日报</div><p>{escape(row['reading'] or '今天的 K 线日报没有这个品种')}</p>
  <div class="k">我的判断</div><p>{call}</p>{f'<p class="reason">{reason}</p>' if reason else ''}
  {verdict}
  <div class="grid">网格：{escape(row['grid'] or '没有在跑')}</div>
</section>""")
    rate = f"{summary['hits']}/{summary['resolved']}" if summary.get("resolved") else "还没有可核对的判断"
    return PAGE.format(date=escape(date), blocks="".join(blocks), rate=escape(rate))


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>复盘卡 {date}</title>
<style>
:root{{--bg:#0F1419;--card:#171E25;--ink:#EEF2F5;--ink-2:#A9B4BE;--line:#27313A;--brass:#D5A94E;--up:#F0786A;--down:#5CC593}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:"PingFang SC","Noto Sans SC",sans-serif;padding-inline:16px;padding-block:20px}}
main{{max-width:540px;margin:0 auto;display:grid;gap:14px}}
.top{{display:flex;justify-content:space-between;align-items:baseline;border-bottom:2px solid var(--brass);padding-bottom:10px}}
.top h1{{margin:0;font-size:26px;letter-spacing:.04em}} .top span{{color:var(--brass);font-size:15px}}
.asset{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.asset header{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}}
h2{{margin:0;font-size:21px}} .px{{display:flex;gap:10px;align-items:baseline;font-size:18px}}
.num{{font-variant-numeric:tabular-nums}} .chg.up{{color:var(--up)}} .chg.down{{color:var(--down)}} .chg.flat{{color:var(--ink-2)}}
.k{{margin-top:10px;font-size:12px;color:var(--brass);letter-spacing:.08em}}
p{{margin:3px 0 0;font-size:15px;line-height:1.6}} .reason{{color:var(--ink-2);font-size:14px}} .muted{{color:var(--ink-2)}}
.verdict{{margin-top:10px;font-weight:700;font-size:15px}} .verdict.hit{{color:var(--up)}} .verdict.miss{{color:var(--down)}} .verdict.even{{color:var(--ink-2)}}
.grid{{margin-top:10px;padding-top:8px;border-top:1px solid var(--line);font-size:13px;color:var(--ink-2)}}
footer{{display:flex;justify-content:space-between;color:var(--ink-2);font-size:13px}}
</style></head><body><main>
<div class="top"><h1>Park 的交易复盘</h1><span class="num">{date}</span></div>
{blocks}
<footer><span>判断命中：{rate}</span><span>红涨绿跌 · 测试盘/纸面盘</span></footer>
</main></body></html>"""
