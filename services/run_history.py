"""Lightweight, queryable run history.

An append-only JSONL log of compact per-cycle records — enough to answer "what
happened over the last N cycles / days" without the heavyweight JSON→DB
migration (deliberately deferred). One line per cycle; queries are simple
filters over the file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config


class RunHistory:
    def __init__(self, output_root: Path | None = None) -> None:
        base = output_root or ROOT / load_pipeline_config().get("output_root", "outputs")
        self.path = Path(base) / "run_history" / "history.jsonl"

    def append(self, record: dict) -> dict:
        entry = {"recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), **record}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue  # skip a corrupt line rather than break the whole history
        return records

    def recent(self, limit: int = 50) -> list[dict]:
        records = self._load()
        return list(reversed(records[-limit:]))

    def query(self, since: str | None = None, until: str | None = None, state: str | None = None) -> list[dict]:
        out = []
        for record in self._load():
            run_date = record.get("run_date", "")
            if since and run_date < since:
                continue
            if until and run_date > until:
                continue
            if state is not None and record.get("state") != state:
                continue
            out.append(record)
        return out


def record_run(record: dict, output_root: Path | None = None) -> dict:
    return RunHistory(output_root).append(record)
