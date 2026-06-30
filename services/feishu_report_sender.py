from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from services.alert_notifier import FeishuSender
from services.journal_store import load_json, write_json
from services.live_env import apply_live_env


DEFAULT_MAX_CHARS = 3500
DIGEST_KINDS = {"pm_morning", "pm_evening", "strategy_research"}
REPORT_WEBHOOK_KEYS = (
    "TRADING_ORCHESTRATOR_REPORT_FEISHU_WEBHOOK_URL",
    "TRADING_ORCHESTRATOR_FEISHU_REPORT_WEBHOOK_URL",
)
REPORT_SECRET_KEYS = (
    "TRADING_ORCHESTRATOR_REPORT_FEISHU_SECRET",
    "TRADING_ORCHESTRATOR_FEISHU_REPORT_SECRET",
)


class FeishuReportSender:
    """Send CEO-facing trading reports to the configured report channel.

    Health alerts use their own Feishu/Telegram sender. Reports may use a
    separate Feishu webhook via the report-specific env keys above, while still
    falling back to the default Feishu webhook for backward compatibility.
    """

    def __init__(self, output_root: Path, sender=None) -> None:
        self.output_root = Path(output_root)
        self.sender = sender if sender is not None else resolve_report_sender()

    def run(
        self,
        run_date: str,
        kind: str,
        title: str,
        source_path: Path | None = None,
        message: str | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> dict:
        if not title.strip():
            raise ValueError("title is required")
        if source_path is None and message is None:
            raise ValueError("either source_path or message is required")

        source_text = ""
        resolved_source = ""
        if source_path is not None:
            resolved = Path(source_path).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(str(resolved))
            source_text = resolved.read_text(encoding="utf-8")
            resolved_source = str(resolved)
        if message is not None:
            source_text = message

        text, truncated, summarized = self._format_message(
            run_date=run_date,
            kind=kind,
            title=title,
            body=source_text,
            source_path=resolved_source,
            max_chars=max_chars,
        )

        configured = bool(getattr(self.sender, "configured", False))
        if configured:
            delivery = self.sender.send(text)
            delivered = bool(delivery.get("ok"))
            channel = str(delivery.get("channel", getattr(self.sender, "channel", "unknown")))
        else:
            delivery = {"ok": False, "channel": "log", "reason": "not_configured"}
            delivered = False
            channel = "log"
            print(f"[FEISHU_REPORT] {text}")

        payload = {
            "run_date": run_date,
            "kind": kind,
            "title": title,
            "source_path": resolved_source,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "pass" if delivered else "fail",
            "delivered": delivered,
            "channel": channel,
            "truncated": truncated,
            "summarized": summarized,
            "message_chars": len(source_text),
            "sent_chars": len(text),
            "delivery": delivery,
        }
        self._record(payload)
        return payload

    def _format_message(
        self,
        run_date: str,
        kind: str,
        title: str,
        body: str,
        source_path: str,
        max_chars: int,
    ) -> tuple[str, bool, bool]:
        header_lines = [
            f"{title}",
            f"日期: {run_date}",
            f"类型: {kind}",
        ]
        header = "\n".join(header_lines).strip()
        body = (body or "").strip()
        summarized = kind in DIGEST_KINDS and bool(source_path)
        if summarized:
            body = self._digest_message(kind=kind, body=body, source_path=source_path)
        elif source_path:
            header_lines.append(f"完整 artifact: {source_path}")
            header = "\n".join(header_lines).strip()
        budget = max(200, int(max_chars)) - len(header) - len("\n\n")
        truncated = len(body) > budget
        if truncated:
            body = body[: max(0, budget - 40)].rstrip() + "\n\n[已截断，完整内容见 artifact]"
        return f"{header}\n\n{body}".strip(), truncated, summarized

    def _digest_message(self, kind: str, body: str, source_path: str) -> str:
        if kind in {"pm_morning", "pm_evening"}:
            return _pm_digest(kind, body, source_path)
        if kind == "strategy_research":
            return _strategy_research_digest(body, source_path)
        return body

    def _record(self, payload: dict) -> None:
        dated_path = self.output_root / "feishu_reports" / f"{payload['run_date']}.json"
        rows = load_json(dated_path)
        rows = [
            row
            for row in rows
            if not (
                row.get("kind") == payload.get("kind")
                and row.get("source_path", "") == payload.get("source_path", "")
                and row.get("title") == payload.get("title")
            )
        ]
        rows.append(payload)
        write_json(dated_path, rows)
        write_json(self.output_root / "feishu_reports" / "current.json", [payload])


def resolve_report_sender():
    apply_live_env()
    webhook_url = _first_env(REPORT_WEBHOOK_KEYS)
    secret = _first_env(REPORT_SECRET_KEYS)
    if webhook_url:
        return FeishuSender(webhook_url=webhook_url, secret=secret)
    return FeishuSender()


def _first_env(keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return None


def _pm_digest(kind: str, body: str, source_path: str) -> str:
    conclusion = _first_paragraph(_section(body, "PM 结论", "结论")) or _first_paragraph(body)
    second_conclusion = _paragraphs(_section(body, "PM 结论", "结论"), limit=2)
    system = _pick_bullets(_section(body, "系统与安全"), ("maturity", "Health", "live readiness", "paper auto", "执行安全", "主动 demo"), 3)
    portfolio = _pick_bullets(_section(body, "组合快照", "Portfolio"), ("今日执行", "今日 closed", "realized", "marked", "样本量", "open positions"), 3)
    profitability = _pick_bullets(_section(body, "盈利性判断", "策略判断"), ("已证明", "近 promising", "likely losing", "不能称盈利", "Proven"), 3)
    actions = _pick_numbered(_section(body, "PM 动作", "未来", "Actions"), limit=3)

    lines = [
        "结论",
        _shorten(_clean_text(conclusion), 150),
    ]
    if len(second_conclusion) > 1:
        lines.append(_shorten(_clean_text(second_conclusion[1]), 150))

    lines.extend(["", "你要知道"])
    lines.extend(_prefixed("安全", system[:1]))
    lines.extend(_prefixed("组合", portfolio[:2]))
    lines.extend(_prefixed("策略", profitability[:2]))

    lines.extend(["", "下一步"])
    if actions:
        lines.extend(f"- {_shorten(_clean_text(item), 120)}" for item in actions[:3])
    else:
        lines.append("- 继续只读观察；任何加风险、调参数、切 active/demo 都需要单独批准。")

    noun = "早报" if kind == "pm_morning" else "晚报"
    lines.extend(["", f"完整{noun}已保存：{Path(source_path).name}"])
    return "\n".join(_drop_empty_tail(lines))


def _strategy_research_digest(body: str, source_path: str) -> str:
    summary = _first_paragraph(_section(body, "PM summary", "PM Summary", "PM 摘要")) or _first_paragraph(body)
    proposed = _section(body, "Proposed class", "Proposed Class", "方案", "策略设计")
    implementation = _section(body, "Implementation", "实现")
    verification = _section(body, "Verification", "验证")
    next_section = _section(body, "Next validation step", "Next Validation Step", "下一步")
    next_step = _first_paragraph(next_section)
    next_bullets = _pick_bullets(next_section, tuple(), 2)
    if next_step.rstrip().endswith(":") and next_bullets:
        next_step = f"{next_step} {'；'.join(next_bullets)}"
    recommendation = _first_paragraph(_section(body, "CEO-facing recommendation", "CEO Recommendation", "CEO 建议"))

    strategy_id = _find_value(proposed, "Strategy id", "Strategy class", "策略 id", "策略 class")
    changed_files = _pick_bullets(implementation, ("added", "registered", "configs/", "tests/", "新增", "注册", "配置", "测试"), 3)
    verification_line = _first_matching_line(verification, ("pytest", "passed", "pass", "通过"))

    lines = [
        "结论",
        _shorten(_clean_text(summary), 180),
    ]
    if strategy_id:
        lines.append(f"新方案：{_clean_text(strategy_id)}")

    lines.extend(["", "你要知道"])
    if changed_files:
        lines.append("- 产出：" + "；".join(_shorten(_clean_text(item), 70) for item in changed_files[:3]))
    if verification_line:
        lines.append("- 验证：" + _shorten(_clean_text(verification_line), 110))
    if recommendation:
        lines.append("- 建议：" + _shorten(_clean_text(recommendation), 150))

    lines.extend(["", "下一步"])
    if next_step:
        lines.append("- " + _shorten(_clean_text(next_step), 160))
    else:
        lines.append("- 只放 shadow/paper 观察；满足足够 closed sample 前不推广。")
    lines.append("- 今天不需要你批准任何 demo/live/加风险动作，除非后续另开审批。")

    lines.extend(["", f"完整研究已保存：{Path(source_path).name}"])
    return "\n".join(_drop_empty_tail(lines))


def _section(body: str, *needles: str) -> str:
    lines = body.splitlines()
    start = None
    lowered = [needle.lower() for needle in needles]
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and any(needle in stripped.lower() for needle in lowered):
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].strip().startswith("#"):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def _paragraphs(text: str, limit: int = 2) -> list[str]:
    blocks = []
    current = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        if line.startswith("#") or line.startswith("|"):
            continue
        if re.match(r"^[-*]\s+", line) or re.match(r"^\d+\.\s+", line):
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(" ".join(current))
    return blocks[:limit]


def _first_paragraph(text: str) -> str:
    paragraphs = _paragraphs(text, limit=1)
    return paragraphs[0] if paragraphs else ""


def _pick_bullets(text: str, keywords: tuple[str, ...], limit: int) -> list[str]:
    bullets = []
    lower_keywords = tuple(keyword.lower() for keyword in keywords)
    for raw in text.splitlines():
        line = raw.strip()
        if not re.match(r"^[-*]\s+", line):
            continue
        item = re.sub(r"^[-*]\s+", "", line)
        if not lower_keywords or any(keyword in item.lower() for keyword in lower_keywords):
            bullets.append(item)
        if len(bullets) >= limit:
            break
    return bullets


def _pick_numbered(text: str, limit: int) -> list[str]:
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if re.match(r"^\d+\.\s+", line):
            items.append(re.sub(r"^\d+\.\s+", "", line))
        elif re.match(r"^[-*]\s+", line):
            items.append(re.sub(r"^[-*]\s+", "", line))
        if len(items) >= limit:
            break
    return items


def _find_value(text: str, *labels: str) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        for label in labels:
            if line.lower().startswith(label.lower() + ":"):
                return line.split(":", 1)[1].strip()
    return ""


def _first_matching_line(text: str, keywords: tuple[str, ...]) -> str:
    for keyword in keywords:
        lower_keyword = keyword.lower()
        for raw in text.splitlines():
            line = raw.strip()
            if line and lower_keyword in line.lower():
                return line
    return ""


def _prefixed(label: str, values: list[str]) -> list[str]:
    return [f"- {label}：{_shorten(_clean_text(value), 120)}" for value in values if value.strip()]


def _clean_text(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#+\s*", "", text.strip())
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    candidate = text[: max(0, limit - 1)].rstrip()
    punctuation = "。；;，,、. "
    best = max(candidate.rfind(mark) for mark in punctuation)
    if best >= int(limit * 0.55):
        candidate = candidate[: best + 1].rstrip()
    return candidate + "…"


def _drop_empty_tail(lines: list[str]) -> list[str]:
    while lines and not lines[-1]:
        lines.pop()
    return lines
