"""AI 透明度 (#1318): which numbers in an AI line cannot be found in what the AI was given.

Deterministic and model-free. A number counts as found when the same value appears in the
inputs: 65K and 6.5万 are the same value, 4% and 4.00% too. "N 基点 / bp" also counts when it is
the gap between two percentages in the inputs (4.00% and 3.75% give 25 基点). Anything else is
reported as 「输入里没有」: no source was found, which is not the same as wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

_NUMBER = re.compile(r"(?<![\d.])(\d+(?:,\d{3})*(?:\.\d+)?)\s*(%|基点|bp|[KMB万亿])?(?![A-Za-z])", re.I)
_SCALE = {"k": 1e3, "m": 1e6, "b": 1e9, "万": 1e4, "亿": 1e8}


@dataclass(frozen=True)
class Number:
    text: str
    value: float
    kind: str  # "plain", "percent" or "bp"


def numbers(text: str) -> list[Number]:
    out = []
    for match in _NUMBER.finditer(text or ""):
        unit = (match.group(2) or "").lower()
        value = float(match.group(1).replace(",", "")) * _SCALE.get(unit, 1)
        kind = "percent" if unit == "%" else "bp" if unit in {"基点", "bp"} else "plain"
        out.append(Number(match.group(0).replace(" ", ""), value, kind))
    return out


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def unverified(text: str, sources: Iterable[str]) -> list[str]:
    """The numbers in `text` that none of `sources` backs, in order, each once."""
    given = [n for source in sources for n in numbers(str(source or ""))]
    plain = [n.value for n in given if n.kind != "percent"]
    percent = [n.value for n in given if n.kind == "percent"]
    gaps = [abs(a - b) * 100 for i, a in enumerate(percent) for b in percent[i + 1:]]
    missing: list[str] = []
    for number in numbers(text):
        if number.kind == "percent":
            found = any(_same(number.value, v) for v in percent)
        elif number.kind == "bp":
            found = any(_same(number.value, v) for v in gaps)
        else:
            found = any(_same(number.value, v) for v in plain)
        if not found and number.text not in missing:
            missing.append(number.text)
    return missing
