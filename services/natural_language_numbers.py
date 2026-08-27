"""Small, deterministic helpers for explicit Chinese-number strategy fields."""

from __future__ import annotations

import re


_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_SMALL_UNITS = {"十": 10, "百": 100, "千": 1_000}
_LARGE_UNITS = {"万": 10_000, "亿": 100_000_000}
CHINESE_NUMBER_TOKEN = r"[零〇一二两三四五六七八九十百千万亿]+"
_NUMBER_TOKEN = rf"(?:[0-9]+(?:\.[0-9]+)?|{CHINESE_NUMBER_TOKEN})"
_COUNT_RE = re.compile(
    rf"(?:一共|共|总共)\s*(?P<count>{_NUMBER_TOKEN})\s*(?:笔|单|档|次)",
    re.IGNORECASE,
)


def parse_chinese_number(value: str) -> float | None:
    """Parse an explicit integer-like Chinese number, including ``七万八``."""

    token = str(value or "").strip().replace(",", "").replace("，", "")
    if not token:
        return None
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", token):
        return float(token)
    if any(char not in _DIGITS and char not in _SMALL_UNITS and char not in _LARGE_UNITS for char in token):
        return None
    if "亿" in token:
        high, remainder = token.split("亿", 1)
        high_value = _parse_section(high) or 1
        return high_value * _LARGE_UNITS["亿"] + (parse_chinese_number(remainder) or 0)
    if "万" in token:
        high, remainder = token.split("万", 1)
        high_value = _parse_section(high) or 1
        if remainder and len(remainder) == 1 and remainder in _DIGITS:
            # Colloquial prices use ``七万八`` for 78,000.
            remainder_value = _DIGITS[remainder] * 1_000
        else:
            remainder_value = parse_chinese_number(remainder) or 0
        return high_value * _LARGE_UNITS["万"] + remainder_value
    return float(_parse_section(token))


def extract_explicit_entry_count(text: str) -> int | None:
    """Return an explicit ``一共 N 笔`` count, without inferring a count."""

    match = _COUNT_RE.search(str(text or ""))
    if match is None:
        return None
    value = parse_chinese_number(match.group("count"))
    if value is None or not value.is_integer() or value <= 0:
        return None
    return int(value)


def extract_explicit_entry_ladder(text: str) -> tuple[list[float], int] | None:
    """Read an explicit level list followed by its stated total count."""

    source = str(text or "")
    count_match = _COUNT_RE.search(source)
    if count_match is None:
        return None
    token = _NUMBER_TOKEN
    sequence = re.compile(
        rf"(?P<levels>{token}(?:\s*[、,，/和及]\s*{token})+)"
    )
    matches = list(sequence.finditer(source[: count_match.start()]))
    if not matches:
        return None
    levels = [
        parse_chinese_number(item)
        for item in re.findall(_NUMBER_TOKEN, matches[-1].group("levels"))
    ]
    count = extract_explicit_entry_count(source)
    if count is None or len(levels) != count or any(item is None or item <= 0 for item in levels):
        return None
    return [float(item) for item in levels if item is not None], count


def _parse_section(token: str) -> int:
    total = 0
    pending = 0
    for char in str(token or ""):
        if char in _DIGITS:
            pending = _DIGITS[char]
            continue
        unit = _SMALL_UNITS.get(char)
        if unit is not None:
            total += (pending or 1) * unit
            pending = 0
    return total + pending


__all__ = [
    "CHINESE_NUMBER_TOKEN",
    "extract_explicit_entry_count",
    "extract_explicit_entry_ladder",
    "parse_chinese_number",
]
