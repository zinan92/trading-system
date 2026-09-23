"""K-line daily / weekly as scannable cards, and the review block that opens the morning brief.

The newsletters stay the source of truth; this module only changes how they are shown:
one row per asset with a direction and one sentence, everything else folded away.
"""
from __future__ import annotations

import json
import re
from html import escape
from datetime import datetime
from pathlib import Path
from typing import Any

DIRECTION = {"long": ("偏多", "▲"), "short": ("偏空", "▼"), "wait": ("观望", "–")}
CALL = {"long": "看多", "short": "看空", "flat": "观望"}
OUTCOME = {"hit": ("说中了", "hit"), "miss": ("没说中", "miss"), "even": ("基本没动", "even"), "unverifiable": ("无法核对", "even")}
MINE = ("bitcoin", "gold")
IMG = re.compile(r"!\[([^\]]*)\]\((?:\./)?snapshots/([0-9a-f]{64}\.png)\)")
FIELD = re.compile(r"^\*\*(.+?)\*\*：(.*)$")


def direction_of(structure: str | None, odds: str | None = None) -> str:
    text = f"{structure or ''} {odds or ''}"
    if "分歧" in (structure or "") or "等待确认" in (structure or ""):
        return "wait"
    if "偏多" in text or "做多方向" in text:
        return "long"
    if "偏空" in text or "做空方向" in text:
        return "short"
    return "wait"


def _strip_label(text: str | None, label: str) -> str:
    text = (text or "").strip()
    return text[len(label) + 1:].strip() if text.startswith(label + "：") else text


def first_sentence(text: str | None) -> str:
    """Up to the first full stop; a long first sentence stops at its first semicolon instead."""
    text = (text or "").strip()
    match = re.search(r"[。！？]", text)
    sentence = text[: match.end()] if match else text
    if len(sentence) > 80 and "；" in sentence:
        sentence = sentence[: sentence.index("；") + 1]
    return sentence


def latest_kline_file(folder: Path, suffix: str) -> Path | None:
    """Newest K-line daily issue. A rerun writes `<date>-kline-daily-newsletter-<hash><suffix>` next to the first
    file, so pick the newest date, then the newest write; `-unavailable` placeholders never count."""
    if not folder.exists():
        return None
    pattern = re.compile(r"(20\d\d-\d\d-\d\d)-kline-daily-newsletter(?:-[0-9a-f]{6,64})?" + re.escape(suffix) + "$")
    found = [(m.group(1), path.stat().st_mtime, path) for path in folder.iterdir() if (m := pattern.match(path.name))]
    return max(found)[2] if found else None


# ---- daily ---------------------------------------------------------------------------
def daily_cards(folder: Path) -> dict[str, Any]:
    latest = latest_kline_file(folder, ".article.json")
    if latest is None:
        return {"ok": False, "reason": "还没有 K 线日报"}
    try:
        article = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "reason": "K 线日报正在生成，稍后刷新"}
    rows: dict[str, dict[str, Any]] = {}
    for block in article.get("blocks") or []:
        key = block.get("asset_key")
        if not key:
            continue
        row = rows.setdefault(key, {"key": key, "name": key, "periods": [], "images": []})
        kind = block.get("type")
        if kind == "asset_heading":
            row["name"] = block.get("display_name") or key
        elif kind == "asset_summary":
            row.update(position=_strip_label(block.get("position"), "位置"), structure=_strip_label(block.get("structure"), "结构"),
                       odds=block.get("odds"), synthesis=block.get("synthesis"), meaning=block.get("market_meaning"))
        elif kind == "period_text" and block.get("text"):
            row["periods"].append({"label": block.get("label"), "text": block.get("text")})
        elif kind == "image" and block.get("path"):
            name = Path(str(block["path"])).name
            if re.fullmatch(r"[0-9a-f]{64}\.png", name):
                row["images"].append({"label": block.get("label") or "", "src": f"/newsletter-asset/kline/{name}"})
    cards = [dict(r, direction=direction_of(r.get("structure"), r.get("odds"))) for r in rows.values() if r.get("synthesis")]
    cards.sort(key=lambda r: (r["key"] not in MINE, MINE.index(r["key"]) if r["key"] in MINE else 0))
    return {"ok": True, "date": latest.name[:10], "cutoff_at": article.get("cutoff_at"), "cards": cards}


# ---- weekly --------------------------------------------------------------------------
def weekly_cards(markdown: str) -> dict[str, Any]:
    title = next((line[2:].strip() for line in markdown.splitlines() if line.startswith("# ")), "宏观 K 线周报")
    group, current = "", None
    cards: list[dict[str, Any]] = []
    shortlist: dict[str, str] = {}
    in_list = False
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            group = line[3:].strip()
            in_list = group == "本周机会清单"
            current = None
            continue
        if in_list and line.startswith("- ") and "：" in line:
            name, verdict = line[2:].rsplit("：", 1)
            shortlist[name.strip()] = verdict.strip()
            continue
        if line.startswith("### "):
            current = {"name": line[4:].strip(), "group": group, "periods": [], "images": []}
            cards.append(current)
            continue
        if current is None:
            continue
        image = IMG.search(line)
        if image:
            current["images"].append({"label": image.group(1).split("｜")[-1], "src": f"/newsletter-asset/weekly/{image.group(2)}"})
            continue
        field = FIELD.match(line)
        if not field:
            continue
        label, value = field.group(1), field.group(2).strip()
        if label == "位置":
            current["position"] = _strip_label(value, "位置")
        elif label == "结构":
            current["structure"] = _strip_label(value, "结构")
        elif label == "赔率":
            current["odds"] = value
        elif label == "综合结论":
            current["synthesis"] = value
        elif label == "市场含义":
            current["meaning"] = value
        else:
            current["periods"].append({"label": label, "text": value})
    for card in cards:
        card["direction"] = direction_of(card.get("structure"), card.get("odds"))
        card["verdict"] = shortlist.get(card["name"])
    return {"ok": bool(cards), "title": title, "cards": [c for c in cards if c.get("synthesis") or c.get("structure")],
            "reason": None if cards else "周报里没有读到资产分析"}


# ---- rendering -----------------------------------------------------------------------
def render_cards(title: str, subtitle: str, cards: list[dict[str, Any]], full_href: str, grouped: bool) -> str:
    sections: list[str] = []
    last_group = None
    counts = {"long": 0, "short": 0, "wait": 0}
    for card in cards:
        counts[card["direction"]] += 1
        if grouped and card.get("group") != last_group:
            last_group = card.get("group")
            sections.append(f'<h2 class="group">{escape(last_group or "")}</h2>')
        label, arrow = DIRECTION[card["direction"]]
        mine = ' mine' if card.get("key") in MINE or any(word in card["name"] for word in ("BTC", "黄金")) else ''
        verdict = f'<span class="verdict v-{escape(card["verdict"])}">{escape(card["verdict"])}</span>' if card.get("verdict") else ""
        detail = "".join(f'<p><b>{escape(p["label"] or "")}</b>{escape(p["text"])}</p>' for p in card.get("periods") or [])
        facts = "".join(f'<p><b>{name}</b>{escape(card[key])}</p>' for key, name in (("position", "位置"), ("structure", "结构"), ("odds", "赔率"), ("meaning", "市场含义")) if card.get(key))
        images = "".join(f'<figure><img loading="lazy" src="{escape(i["src"])}" alt="{escape(i["label"])}"><figcaption>{escape(i["label"])}</figcaption></figure>' for i in card.get("images") or [])
        sections.append(f"""<article class="card d-{card['direction']}{mine}">
  <div class="head"><span class="dir">{arrow} {label}</span><h3>{escape(card['name'])}</h3>{verdict}</div>
  <p class="one">{escape(first_sentence(card.get('synthesis') or card.get('structure')))}</p>
  <details><summary>全文与图表</summary><div class="more">{facts}{detail}<p><b>综合</b>{escape(card.get('synthesis') or '')}</p><div class="figs">{images}</div></div></details>
</article>""")
    tally = f'<span class="d-long">▲ 偏多 {counts["long"]}</span><span class="d-short">▼ 偏空 {counts["short"]}</span><span class="d-wait">– 观望 {counts["wait"]}</span>'
    return CARDS_PAGE.format(title=escape(title), subtitle=escape(subtitle), tally=tally, body="".join(sections), full=escape(full_href))


def watch_block(watch: dict[str, Any], now: datetime) -> str:
    """本周要看的三件事 at the top of the morning brief; the same list the trading page shows."""
    from .watch import when_label

    items = watch.get("items") or []
    if not items:
        return WATCH.format(cards='<p class="w-empty">三件事还在生成，稍后刷新。</p>', meta="")
    cards = "".join(
        f"""<article class="w-card v-{'high' if item.get('volatility') == '高' else 'mid'}"><div class="w-when">{escape(when_label(item, now))}<span>波动 {escape(item.get('volatility') or '中')}</span></div>
<h3>{escape(item.get('title') or '')}</h3>{f'<div class="w-mk">{"".join(f"<span>{escape(m)}</span>" for m in item.get("markets") or [])}</div>' if item.get('markets') else ''}
<p>{escape(item.get('why') or item.get('source_title') or '')}</p>{f'<small>{escape(item["detail"])}</small>' if item.get('kind') == 'event' and item.get('detail') else ''}</article>"""
        for item in items)
    at = str(watch.get("generated_at") or "")[11:16]
    meta = f"{at} 更新 · {'模型按全市场冲击排序' if watch.get('provider') == 'codex' else '模型暂不可用，按日历重要性排序'}"
    return WATCH.format(cards=cards, meta=escape(meta))


def review_block(rows: list[dict[str, Any]], summary: dict[str, Any], review_hours: int) -> str:
    """Opening block of the morning brief: were yesterday's calls right, and today's call per asset."""
    items = []
    for row in rows:
        last, today, reading = row.get("last"), row.get("today"), row.get("reading")
        if last:
            label, cls = OUTCOME.get(str(last.get("outcome")), ("待核对", "even"))
            move = f"（{last['move_pct']:+.2f}%）" if last.get("move_pct") is not None else ""
            past = f'<span class="o-{cls}">{CALL.get(last["direction"], last["direction"])} → {label}{move}</span>'
        elif row.get("pending"):
            past = f'<span class="muted">{CALL.get(row["pending"]["direction"], "")} · 等 {review_hours} 小时后核对</span>'
        else:
            past = '<span class="muted">还没有判断记录</span>'
        chip = ""
        if reading:
            dlabel, arrow = DIRECTION[reading["direction"]]
            chip = f'<span class="chip d-{reading["direction"]}" title="{escape(reading.get("synthesis") or "")}">K 线日报 {arrow} {dlabel}</span>'
        ai = row.get("ai")
        ai_line = (f'<div class="rv-ai"><span class="k">AI 建议</span><b class="d-{"wait" if ai["direction"] == "flat" else ai["direction"]}">{CALL[ai["direction"]]}</b>'
                   f' · 信心 {ai["confidence"]}/5 · {escape(ai.get("reason") or "")}</div>') if ai else '<div class="rv-ai muted">AI 建议 08:40 生成</div>'
        buttons = "".join(
            f'<button type="button" data-asset="{escape(row["key"])}" data-d="{d}" aria-pressed="{str(bool(today and today["direction"] == d)).lower()}">{CALL[d]}</button>'
            for d in ("long", "flat", "short"))
        items.append(f"""<div class="rv-row"><div class="rv-asset"><b>{escape(row['label'])}</b>{chip}</div>
  <div class="rv-past"><div><span class="k">上次判断</span>{past}</div>{ai_line}</div>
  <div class="rv-today"><span class="k">今天</span><span class="rv-btns" role="group" aria-label="{escape(row['label'])} 今天的判断">{buttons}</span></div></div>""")
    ai_tally = summary.get("ai") or {}
    if summary.get("resolved") or ai_tally.get("resolved"):
        rate = f"你说中 {summary.get('hits', 0)}/{summary.get('resolved', 0)} · AI 说中 {ai_tally.get('hits', 0)}/{ai_tally.get('resolved', 0)}"
    else:
        rate = f"你和 AI 的判断都在 {review_hours} 小时后按收盘价自动核对"
    return REVIEW.format(rows="".join(items), rate=escape(rate))


CARDS_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#f6f7f5;--card:#fff;--ink:#1a211e;--ink2:#5b6661;--line:#dfe4e1;--up:#23835a;--down:#c23a2b;--wait:#7a847f;--acc:#8b6a3e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0b0c0f;--card:#14161b;--ink:#eceef2;--ink2:#9aa3ad;--line:#252a31;--up:#2fc98a;--down:#f2495f;--wait:#8b939c;--acc:#f0a52e}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "PingFang SC","Noto Sans SC",system-ui,sans-serif;padding-inline:16px;padding-block:18px 40px}}
main{{max-width:880px;margin:0 auto;display:grid;gap:10px}}
header{{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 14px;border-bottom:1px solid var(--line);padding-bottom:10px}}
h1{{margin:0;font-size:21px}}.sub{{color:var(--ink2);font-size:13px}}.tally{{display:flex;gap:12px;font-size:13px;font-variant-numeric:tabular-nums}}
.full{{margin-left:auto;color:var(--acc);font-size:13px}}
h2.group{{margin:14px 0 0;font-size:13px;color:var(--ink2);letter-spacing:.08em;font-weight:600}}
.card{{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--wait);border-radius:8px;padding:10px 14px}}
.card.d-long{{border-left-color:var(--up)}}.card.d-short{{border-left-color:var(--down)}}.card.mine{{box-shadow:0 0 0 1px var(--acc) inset}}
.head{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}}h3{{margin:0;font-size:16px}}
.dir{{font-weight:700;font-size:14px;min-width:4.6em;font-variant-numeric:tabular-nums}}.d-long .dir,.d-long{{color:var(--up)}}.d-short .dir,.d-short{{color:var(--down)}}.d-wait .dir,.d-wait{{color:var(--wait)}}
.card h3{{color:var(--ink)}}.verdict{{margin-left:auto;font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);color:var(--ink2)}}.v-参与{{color:var(--acc);border-color:var(--acc)}}
.one{{margin:4px 0 0;color:var(--ink)}}details{{margin-top:4px}}summary{{cursor:pointer;color:var(--ink2);font-size:13px}}
.more p{{margin:6px 0;color:var(--ink2);font-size:14px}}.more b{{color:var(--ink);margin-right:6px}}
.figs{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px}}figure{{margin:0}}img{{max-width:100%;border-radius:6px;border:1px solid var(--line)}}figcaption{{font-size:12px;color:var(--ink2)}}
</style></head><body><main>
<header><h1>{title}</h1><span class="sub">{subtitle}</span><span class="tally">{tally}</span><a class="full" href="{full}">看原版全文</a></header>
{body}
<p class="sub">模型生成、未经人工复核；方向取自报告里的「结构」判断，只供参考，不自动交易。</p>
</main></body></html>"""


WATCH = """<style>
#desk-watch{{font:14px/1.55 "PingFang SC","Noto Sans SC",system-ui,sans-serif;max-width:880px;margin:14px auto 0;color:#2e2e2b;box-sizing:border-box}}
#desk-watch .top{{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:baseline;margin-bottom:8px}}#desk-watch h2{{margin:0;font-size:17px}}#desk-watch .meta{{color:#6b7570;font-size:12.5px}}
#desk-watch .w-row{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}}
#desk-watch .w-card{{background:#fffdf7;border:1px solid #e3d6bd;border-left:4px solid #c9a86a;border-radius:8px;padding:9px 12px;display:grid;gap:3px;align-content:start}}
#desk-watch .w-card.v-high{{border-left-color:#b6614f}}#desk-watch .w-when{{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:#8b6a3e;font-variant-numeric:tabular-nums}}
#desk-watch .w-when span{{color:#6b7570}}#desk-watch h3{{margin:0;font-size:15px}}#desk-watch p{{margin:0;font-size:13px;color:#4a4a45}}#desk-watch small{{font-size:12px;color:#8f8f88}}
#desk-watch .w-mk{{display:flex;flex-wrap:wrap;gap:4px}}#desk-watch .w-mk span{{font-size:11.5px;padding:0 6px;border-radius:4px;background:#f1ebdd;color:#5b5b56}}#desk-watch .w-empty{{color:#6b7570}}
@media (max-width:760px){{#desk-watch .w-row{{grid-template-columns:1fr}}}}
</style>
<section id="desk-watch" aria-labelledby="desk-watch-h"><div class="top"><h2 id="desk-watch-h">本周要看的三件事</h2><span class="meta">{meta}</span></div><div class="w-row">{cards}</div></section>"""


REVIEW = """<style>
#desk-review{{font:14px/1.55 "PingFang SC","Noto Sans SC",system-ui,sans-serif;max-width:880px;margin:14px auto;padding:12px 16px;border:1px solid #e3d6bd;border-radius:10px;background:#fffdf7;color:#2e2e2b;box-sizing:border-box}}
#desk-review h2{{margin:0;font-size:17px}}#desk-review .top{{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:baseline;margin-bottom:6px}}#desk-review .muted,#desk-review .k,#desk-review .rate{{color:#6b7570;font-size:12.5px}}
#desk-review .rv-row{{display:grid;grid-template-columns:minmax(120px,1.2fr) 2fr auto;gap:6px 14px;align-items:center;padding:8px 0;border-top:1px solid #eadfc6}}
#desk-review .rv-asset{{display:flex;flex-direction:column}}#desk-review .rv-ai{{font-size:12.5px;margin-top:2px}}#desk-review .k{{margin-right:6px}}#desk-review .chip{{font-size:12px}}
#desk-review .d-long,#desk-review .o-hit{{color:#4f8a60}}#desk-review .d-short,#desk-review .o-miss{{color:#b6614f}}#desk-review .d-wait,#desk-review .o-even{{color:#7a847f}}
#desk-review .rv-btns{{display:inline-flex;border:1px solid #cdbf9f;border-radius:7px;overflow:hidden}}#desk-review button{{font:inherit;font-size:13px;border:0;background:#fff;color:#1a211e;padding:5px 12px;cursor:pointer}}
#desk-review button+button{{border-left:1px solid #cdbf9f}}#desk-review button[aria-pressed="true"]{{background:#1a211e;color:#fff}}#desk-review .toast{{font-size:12.5px;color:#8b6a3e;min-height:1em}}
@media (max-width:640px){{#desk-review .rv-row{{grid-template-columns:1fr}}}}
</style>
<section id="desk-review" aria-labelledby="desk-review-h"><div class="top"><h2 id="desk-review-h">昨天判断对了吗</h2><span class="rate">{rate}</span><a href="/trade" target="_top" style="margin-left:auto;font-size:13px;color:#8b6a3e">去交易页写理由、看计划 →</a></div>
{rows}<div class="toast" id="desk-review-toast" role="status"></div></section>
<script>
document.querySelectorAll('#desk-review button[data-d]').forEach(function(b){{b.addEventListener('click',async function(){{
  var t=document.getElementById('desk-review-toast');t.textContent='正在记录…';
  try{{var r=await fetch('/api/judgments',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{asset:b.dataset.asset,direction:b.dataset.d,confidence:3,reason:'',cited:[],action:'recorded'}})}});
  var d=await r.json().catch(function(){{return {{}}}});if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'没记上，稍后再试');
  b.parentNode.querySelectorAll('button').forEach(function(x){{x.setAttribute('aria-pressed',String(x===b))}});t.textContent='已记下：'+b.textContent+'。理由和计划可以到交易页补。';}}
  catch(e){{t.textContent=e.message}}}})}});
</script>"""


def inject_review(html: str, block: str) -> str:
    """Right under the brief's sticky nav, above its digest; after <body> when the layout is unknown."""
    anchor = re.search(r'<div class="(?:digest|wrap)"', html)
    if anchor:
        return html[: anchor.start()] + block + html[anchor.start():]
    match = re.search(r"<body[^>]*>", html, flags=re.IGNORECASE)
    if not match:
        return block + html
    return html[: match.end()] + block + html[match.end():]
