from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import load_json, write_json


class RunnerStatusStore:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def record(self, run_date: str, status: dict) -> dict:
        payload = {
            "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "run_date": run_date,
            **status,
        }
        current_path = self.output_root / "runner_status" / "current.json"
        current_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        history_path = self.output_root / "runner_status" / f"{run_date}.json"
        history = load_json(history_path)
        history.append(payload)
        write_json(history_path, history)
        return payload

    def current(self) -> dict:
        path = self.output_root / "runner_status" / "current.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
