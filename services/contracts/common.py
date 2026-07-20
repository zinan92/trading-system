"""Shared validation patterns and coercion helpers for dashboard contract builders."""

from __future__ import annotations

import re


_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


_CYCLE_ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_(DAY|NIGHT)$")


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
