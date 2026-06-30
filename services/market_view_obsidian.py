from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from services.journal_store import write_json
from services.market_view import OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR, MarketViewStore
from services.market_view_intake import MarketViewIntake


class MarketViewObsidianSync:
    """Derive runtime market-view artifacts from the Obsidian daily note.

    Obsidian is the human-editable source of truth. The JSON files under
    outputs/market_views are generated runtime artifacts for the trading gates.
    """

    def __init__(self, output_root: Path, obsidian_root: Path) -> None:
        self.output_root = Path(output_root)
        self.obsidian_root = Path(obsidian_root)
        self.store = MarketViewStore(self.output_root)

    def note_path(self, run_date: str) -> Path:
        return self.obsidian_root / OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR / f"{run_date}.md"

    def sync(self, run_date: str, *, note_path: Path | None = None) -> dict:
        source_path = Path(note_path) if note_path else self.note_path(run_date)
        if not source_path.exists():
            raise FileNotFoundError(f"Obsidian market-view note not found: {source_path}")
        note = source_path.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(note)
        sections = markdown_sections(body)
        draft = MarketViewIntake(self.output_root).draft(run_date, body or note)
        expiry_text = _first_section_text(sections, ["过期条件", "失效条件"])
        expiry_draft = MarketViewIntake(self.output_root).draft(run_date, expiry_text) if expiry_text else draft

        score = _frontmatter_score(frontmatter)
        timeframes = _frontmatter_list(frontmatter, "timeframes") or draft.timeframes
        summary = _first_section_text(sections, ["一句话结论", "摘要", "结论"]) or draft.summary
        key_levels = _section_bullets(sections, ["关键价位", "关键位置"]) or draft.key_levels
        trade_plan = _first_section_text(sections, ["今日交易计划", "交易计划", "交易原则提炼"]) or draft.trade_plan

        payload = self.store.record(
            run_date=run_date,
            score=score if score is not None else draft.score,
            summary=summary,
            raw_text=body.strip() or note.strip(),
            key_levels=key_levels,
            timeframes=timeframes,
            trade_plan=trade_plan,
            source="Obsidian每日交易分析",
            valid_for_hours=expiry_draft.valid_for_hours,
            expires_at=expiry_draft.expires_at,
            expires_if_price_moves_pct=expiry_draft.expires_if_price_moves_pct,
            expiry_target_price=expiry_draft.expiry_target_price,
            expire_above=expiry_draft.expire_above,
            expire_below=expiry_draft.expire_below,
            write_obsidian=False,
        )
        payload["intake"] = {
            "parser": "market_view_obsidian_v1",
            "source_path": str(source_path),
            "source_note": str(source_path.relative_to(self.obsidian_root)) if _is_relative_to(source_path, self.obsidian_root) else str(source_path),
            "frontmatter_keys": sorted(frontmatter),
            "extracted": {
                "score": payload["direction_score"],
                "timeframes": payload["timeframes"],
                "key_level_count": len(payload["key_levels"]),
                "expiry_target_price": expiry_draft.expiry_target_price,
                "valid_for_hours": expiry_draft.valid_for_hours,
                "expires_at": expiry_draft.expires_at,
                "expires_if_price_moves_pct": expiry_draft.expires_if_price_moves_pct,
                "expire_above": expiry_draft.expire_above,
                "expire_below": expiry_draft.expire_below,
            },
        }
        payload["source_artifacts"] = {"obsidian": str(source_path)}
        write_json(self.output_root / "market_views" / f"{run_date}.json", [payload])
        write_json(self.output_root / "market_views" / "current.json", [payload])
        return payload


def split_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    text = markdown.lstrip("\ufeff")
    if not text.startswith("---\n"):
        return {}, markdown
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, markdown
    raw = text[4:end]
    body = text[end + len("\n---\n") :]
    return parse_simple_frontmatter(raw), body


def parse_simple_frontmatter(raw: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    current_key = ""
    for line in raw.splitlines():
        if not line.strip():
            continue
        key_match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if key_match:
            current_key = key_match.group(1)
            raw_value = key_match.group(2).strip()
            values[current_key] = [] if raw_value == "" else _coerce_frontmatter_value(raw_value)
            continue
        if current_key and line.startswith("  - "):
            current = values.setdefault(current_key, [])
            if not isinstance(current, list):
                current = []
                values[current_key] = current
            current.append(_coerce_frontmatter_value(line[4:].strip()))
    return values


def markdown_sections(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in markdown.splitlines():
        match = re.match(r"^##+\s+(.+?)\s*$", line)
        if match:
            current = _normalize_heading(match.group(1))
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def _frontmatter_score(frontmatter: dict[str, Any]) -> int | None:
    for key in ("direction_bias_score", "direction_score", "score"):
        value = frontmatter.get(key)
        if value in (None, ""):
            continue
        try:
            score = int(round(float(value)))
        except (TypeError, ValueError):
            continue
        return max(0, min(100, score))
    return None


def _frontmatter_list(frontmatter: dict[str, Any], key: str) -> list[str]:
    value = frontmatter.get(key)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _first_section_text(sections: dict[str, str], names: list[str]) -> str:
    for name in names:
        content = sections.get(_normalize_heading(name), "").strip()
        if content:
            return _clean_section_text(content)
    return ""


def _section_bullets(sections: dict[str, str], names: list[str]) -> list[str]:
    content = _first_section_text(sections, names)
    if not content:
        return []
    bullets = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
    return bullets


def _clean_section_text(content: str) -> str:
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            lines.append(stripped[4:].strip())
        else:
            lines.append(stripped)
    return "\n".join(lines).strip()


def _normalize_heading(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().strip("#"))


def _coerce_frontmatter_value(value: str) -> Any:
    stripped = value.strip().strip('"').strip("'")
    if re.fullmatch(r"-?\d+", stripped):
        return int(stripped)
    if re.fullmatch(r"-?\d+\.\d+", stripped):
        return float(stripped)
    return stripped


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
