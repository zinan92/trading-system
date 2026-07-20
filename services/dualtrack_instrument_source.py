"""Fetch the execution instrument contract from the dedicated datafeed.

The execution repo must never recreate exchange precision, multiplier, or
margin settings from local defaults.  This client reads a versioned upstream
definition and rejects every response that cannot build a trusted instrument.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from services.dualtrack_nautilus_instrument import validate_instrument_definition


DEFAULT_INSTRUMENT_ENDPOINT = "http://127.0.0.1:8100/api/instruments/commodity/XAUUSDT"


def fetch_execution_instrument_definition(
    *,
    endpoint: str = DEFAULT_INSTRUMENT_ENDPOINT,
    source: str = "binance_usdm_futures",
    timeout_seconds: float = 8.0,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    separator = "&" if "?" in endpoint else "?"
    url = f"{endpoint}{separator}{urlencode({'source': source, 'require_execution_venue': 'true'})}"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read()
    except (OSError, URLError) as exc:
        raise RuntimeError(f"execution instrument definition unavailable: {exc}") from exc
    if status != 200:
        raise RuntimeError(f"execution instrument definition returned HTTP {status}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("execution instrument definition returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("execution instrument definition must be an object")
    return validate_instrument_definition(payload)
