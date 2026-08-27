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
ARABIC_NUMBER_TOKEN = r"[0-9]+(?:\.[0-9]+)?(?:\s*[kK])?"
EXPLICIT_NUMBER_TOKEN = rf"(?:{ARABIC_NUMBER_TOKEN}|{CHINESE_NUMBER_TOKEN})"
_NUMBER_TOKEN = EXPLICIT_NUMBER_TOKEN
_COUNT_RE = re.compile(
    rf"(?:一共|共|总共)\s*(?P<count>{_NUMBER_TOKEN})\s*(?:笔|单|档|次)",
    re.IGNORECASE,
)


def parse_chinese_number(value: str) -> float | None:
    """Parse an explicit integer-like Chinese number, including ``七万八``."""

    token = str(value or "").strip().replace(",", "").replace("，", "")
    if not token:
        return None
    compact_match = re.fullmatch(r"(?P<number>[0-9]+(?:\.[0-9]+)?)\s*[kK]", token)
    if compact_match is not None:
        return float(compact_match.group("number")) * 1_000
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


def extract_explicit_two_level_entry_ladder(text: str) -> tuple[list[float], int] | None:
    """Read two stated price levels only when Park calls them two levels."""

    source = str(text or "")
    match = re.search(
        rf"(?P<first>{_NUMBER_TOKEN})\s*(?:~|～|-|到|至|、|,|，|和|及)\s*"
        rf"(?P<second>{_NUMBER_TOKEN})\s*(?:这|就|共|总共)?\s*(?:两|二|2)\s*个?\s*"
        r"(?:价位|价格|入场位|入场点|档|笔)",
        source,
        re.IGNORECASE,
    )
    if match is None:
        return None
    first = parse_chinese_number(match.group("first"))
    second = parse_chinese_number(match.group("second"))
    if first is None or second is None or first <= 0 or second <= 0 or first == second:
        return None
    return [float(first), float(second)], 2


def extract_explicit_exit_prices(text: str) -> dict[str, float]:
    """Extract TP/SL labels, including compact number-before-label forms."""

    source = str(text or "")
    parenthetical = r"(?:\s*[\(（][^\)）]*[\)）])?"

    def matches(label: str) -> tuple[re.Match[str] | None, re.Match[str] | None]:
        # Keep number-before-label syntax bounded to compact ``82k`` notation.
        prefix = re.search(
            rf"(?P<number>[0-9]+(?:\.[0-9]+)?\s*[kK])\s*(?:{label})",
            source,
            re.IGNORECASE,
        )
        suffix = re.search(
            rf"(?:{label}){parenthetical}\s*(?:位|价|price)?\s*[:：=]?\s*"
            rf"(?P<number>{_NUMBER_TOKEN})",
            source,
            re.IGNORECASE,
        )
        return prefix, suffix

    stop_prefix, stop_suffix = matches(r"止损|stop(?:[_\s]+(?:loss|price))?")
    take_prefix, take_suffix = matches(r"止盈|take(?:[_\s]+profit)?(?:[_\s]+price)?|tp")
    if stop_prefix is not None and take_prefix is not None:
        stop_match, take_match = stop_prefix, take_prefix
    elif stop_suffix is not None and take_suffix is not None:
        stop_match, take_match = stop_suffix, take_suffix
    else:
        stop_match, take_match = stop_prefix or stop_suffix, take_prefix or take_suffix

    def value(match: re.Match[str] | None) -> float | None:
        return parse_chinese_number(match.group("number")) if match is not None else None

    result: dict[str, float] = {}
    stop = value(stop_match)
    take_profit = value(take_match)
    if stop is not None:
        result["stop_price"] = float(stop)
    if take_profit is not None:
        result["take_profit_price"] = float(take_profit)
    return result


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
    "ARABIC_NUMBER_TOKEN",
    "CHINESE_NUMBER_TOKEN",
    "EXPLICIT_NUMBER_TOKEN",
    "extract_explicit_entry_count",
    "extract_explicit_entry_ladder",
    "extract_explicit_exit_prices",
    "extract_explicit_two_level_entry_ladder",
    "parse_chinese_number",
]
