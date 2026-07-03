"""Build a Feishu interactive card payload for a gold trade-ticket review.

Presentation-only: receives an already-derived, already-translated ``view`` dict
(built by :class:`TradeTicketNotifier`) and assembles the Feishu ``interactive``
card JSON. No config, env, or business logic lives here.

The card is a flow-structured record: the signal → execution decision path is
rendered as an ordered checklist (✅ passed / ❌ failed), followed by the
nominal-dollar position, the price plan, backtest, and evidence. Feishu custom-bot
webhooks accept ``{"msg_type": "interactive", "card": <this>}``; buttons may only
carry ``url`` actions (webhooks cannot receive click callbacks).
"""

from __future__ import annotations

from typing import Any

VALID_TEMPLATES = {
    "blue", "wathet", "turquoise", "green", "yellow", "orange",
    "red", "carmine", "violet", "purple", "indigo", "grey",
}


def _div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _hr() -> dict:
    return {"tag": "hr"}


def _note(content: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "lark_md", "content": content}]}


def _fields(pairs: list[tuple[str, str]]) -> dict:
    return {
        "tag": "div",
        "fields": [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"}}
            for label, value in pairs
        ],
    }


def _button(text: str, url: str) -> dict:
    return {
        "tag": "action",
        "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": text}, "url": url, "type": "default"}
        ],
    }


def _mark(passed) -> str:
    if passed is True:
        return "✅"
    if passed is False:
        return "❌"
    return "⬜"


def build_ticket_card(view: dict[str, Any]) -> dict:
    """Assemble the flow-structured Feishu interactive card from a derived ``view``."""
    template = str(view.get("header_template", "grey"))
    if template not in VALID_TEMPLATES:
        template = "grey"

    elements: list[dict] = []

    subtitle = str(view.get("subtitle", "")).strip()
    approval = str(view.get("approval", "")).strip()
    lead = "\n".join(part for part in (f"**{subtitle}**" if subtitle else "", approval) if part)
    if lead:
        elements.append(_div(lead))

    account = view.get("account", {}) if isinstance(view.get("account"), dict) else {}
    mode_text = str(account.get("mode", "")).strip()
    armed = account.get("armed")
    if armed is True:
        mode_text = f"{mode_text}｜已武装".lstrip("｜")
    elif armed is False and mode_text:
        mode_text = f"{mode_text}｜未武装"
    elements.append(_fields([("账户", str(account.get("label", "未知账户"))), ("执行模式", mode_text or "—")]))

    flow = [n for n in view.get("flow", []) if isinstance(n, dict)]
    if flow:
        elements.append(_hr())
        lines = ["**决策路径**"]
        for node in flow:
            detail = str(node.get("detail", "")).strip()
            tail = f"　—　{detail}" if detail else ""
            lines.append(f"{_mark(node.get('passed'))} {node.get('step', '')} · {node.get('name', '')}{tail}")
        terminal = view.get("flow_terminal", {}) if isinstance(view.get("flow_terminal"), dict) else {}
        if terminal.get("text"):
            lines.append(f"{terminal.get('icon', '⚡')} **{terminal['text']}**")
        elements.append(_div("\n".join(lines)))
        note = str(view.get("risk_divergence", "")).strip()
        if note:
            elements.append(_note(f"⚠️ {note}"))

    pu = view.get("position_usd", {}) if isinstance(view.get("position_usd"), dict) else {}
    if pu.get("nominal"):
        lev = str(pu.get("leverage", "")).strip()
        margin = str(pu.get("margin", "")).strip()
        head = f"名义价值 **{pu['nominal']}**"
        if lev or margin:
            head += "（" + "　·　".join(part for part in (f"{lev}× 杠杆" if lev else "", f"保证金 {margin}" if margin else "") if part) + "）"
        elements.append(
            _div(
                f"**仓位（美元名义）**\n{head}\n"
                f"止损这笔亏 **{pu.get('stop', '—')}**　·　止盈这笔赚 **{pu.get('target', '—')}**"
            )
        )

    price = view.get("price", {}) if isinstance(view.get("price"), dict) else {}
    if price:
        elements.append(_hr())
        elements.append(
            _fields(
                [
                    ("入场区间", str(price.get("entry_zone", "—"))),
                    ("估算开单价", str(price.get("entry_price", "—"))),
                    ("止盈", str(price.get("targets", "—"))),
                    ("止损", str(price.get("stop_loss", "—"))),
                ]
            )
        )

    backtest = view.get("backtest", {}) if isinstance(view.get("backtest"), dict) else {}
    dashboard_url = str(view.get("dashboard_url", "")).strip()
    evidence = str(view.get("evidence_note", "")).strip()
    if backtest:
        elements.append(
            _div(
                f"**回测**：{backtest.get('verdict', '—')}　胜率 {backtest.get('win_rate', '—')}"
                f"　平均 R {backtest.get('avg_r', '—')}　样本 {backtest.get('sample', '—')}"
            )
        )
    if dashboard_url:
        elements.append(_button("查看完整决策路径", dashboard_url))
    elif evidence:
        elements.append(_note(f"证据：{evidence}"))

    meta = view.get("meta", {}) if isinstance(view.get("meta"), dict) else {}
    generated = str(meta.get("generated_at", "")).strip() or "待补"
    priced = str(meta.get("price_line", "")).strip() or "开单时价 待补"
    elements.append(_note(f"生成时间 {generated}　·　{priced}"))

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": str(view.get("title", "黄金开单审查卡"))},
        },
        "elements": elements,
    }
