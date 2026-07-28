#!/usr/bin/env python3
"""One-shot, credential-free bridge for an external AI decision provider.

The strategy service writes the exact prompt into a shared exchange directory.
An attended operator runs the model in a separate trusted environment and
writes the JSON response named by the prompt SHA-256. No model credentials
cross this boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _output_path(argv: list[str]) -> Path:
    try:
        index = argv.index("--output-last-message")
        return Path(argv[index + 1])
    except (ValueError, IndexError) as exc:
        raise SystemExit("bridge_missing_output_last_message") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--exchange-dir", required=True)
    parser.add_argument("--bridge-timeout-seconds", type=int, default=210)
    options, provider_args = parser.parse_known_args(argv)

    prompt = sys.stdin.read()
    if not prompt.strip():
        raise SystemExit("bridge_empty_prompt")

    exchange = Path(options.exchange_dir)
    exchange.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    request_path = exchange / "request.json"
    response_path = exchange / f"response-{digest}.json"
    output_path = _output_path(provider_args)
    request = {
        "schema_version": "strategy-recommendation-file-bridge-v1",
        "prompt_sha256": digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "response_filename": response_path.name,
        "prompt": prompt,
    }
    _atomic_write(
        request_path,
        json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2),
    )

    deadline = time.monotonic() + max(1, options.bridge_timeout_seconds)
    while time.monotonic() < deadline:
        if response_path.exists() and response_path.stat().st_size > 0:
            try:
                raw = response_path.read_text(encoding="utf-8")
                parsed = json.loads(raw)
            except (OSError, json.JSONDecodeError):
                time.sleep(0.25)
                continue
            if not isinstance(parsed, dict):
                raise SystemExit("bridge_response_must_be_json_object")
            _atomic_write(
                output_path,
                json.dumps(parsed, ensure_ascii=False, sort_keys=True, indent=2),
            )
            return 0
        time.sleep(0.25)
    raise SystemExit(f"bridge_response_timeout:{digest}")


if __name__ == "__main__":
    raise SystemExit(main())
