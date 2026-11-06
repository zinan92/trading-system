"""One log format for the desk and the Dashboard (#1364): JSON lines, rotated, private, redacted.

Nothing here decides anything. Stdlib only, so every service can import it. A record names the
service, the request and the code location; `redact` runs on the whole record, including the
traceback, so a secret that reaches a message still does not reach the file. A failure to log never
reaches the caller: the `logging` module swallows handler errors and the file is optional.
"""
from __future__ import annotations

import contextvars
from datetime import datetime, timedelta, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Any

ROOT = "trading"
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5
TAIL_BYTES = 4 * 1024 * 1024
REDACTED = "[redacted]"
_SECRET_KEY = re.compile(r"secret|token|passw|passcode|api[_-]?key|private[_-]?key|authorization|cookie|signature|mnemonic", re.I)
_SECRET_TEXT = (
    (re.compile(r"(?i)\bbearer\s+[^\s\"',]+"), f"Bearer {REDACTED}"),
    (re.compile(r"\btsd_[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\b0x[0-9a-fA-F]{64}\b"), REDACTED),
    (re.compile(r"(?i)((?:token|secret|passcode|password|api[_-]?key|signature)\s*[=:]\s*)[\"']?[^\s\"'&,]+"), rf"\1{REDACTED}"),
)
_REQUEST_ID = re.compile(r"[0-9a-f]{8,40}")
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

logging.getLogger(ROOT).addHandler(logging.NullHandler())  # unconfigured (tests, imports) stays silent


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{name}")


def new_request_id() -> str:
    return secrets.token_hex(6)


def set_request_id(value: str | None = None) -> str:
    """Use a well-formed id from the caller, or mint one. The result is what later records carry."""
    chosen = value if value and _REQUEST_ID.fullmatch(value) else new_request_id()
    _request_id.set(chosen)
    return chosen


def request_id() -> str:
    return _request_id.get()


def redact(value: Any, key: str = "") -> Any:
    if key and _SECRET_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for pattern, replacement in _SECRET_TEXT:
            value = pattern.sub(replacement, value)
        return value
    return value if value is None or isinstance(value, (bool, int, float)) else redact(str(value))


class JsonLines(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        row: dict[str, Any] = {
            "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname, "service": self.service, "event": record.getMessage(),
            "where": f"{record.name}:{record.funcName}:{record.lineno}", "request_id": request_id() or None,
        }
        row.update(getattr(record, "fields", None) or {})
        if record.exc_info:
            row["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(row), ensure_ascii=False, default=str)


def event(logger: logging.Logger, name: str, *, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, name, extra={"fields": fields}, stacklevel=2)


class PrivateRotatingFile(RotatingFileHandler):
    """Every file it opens, including the fresh one after a rollover, is owner-only (0600)."""

    def _open(self):
        return open(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors,
                     opener=lambda name, flags: os.open(name, flags, 0o600))


def log_path(directory: Path, service: str) -> Path:
    return Path(directory) / f"{service}.jsonl"


def configure(service: str, directory: Path | None, *, level: int = logging.INFO) -> Path | None:
    """Send `trading.*` to <directory>/<service>.jsonl, warnings also to stderr. Safe to call twice."""
    root = logging.getLogger(ROOT)
    for handler in [h for h in root.handlers if not isinstance(h, logging.NullHandler)]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)
    formatter = JsonLines(service)
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.WARNING)
    stderr.setFormatter(formatter)
    root.addHandler(stderr)
    if directory is None:
        return None
    path = log_path(directory, service)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
        os.chmod(path, 0o600)
    except OSError:
        return None  # a read-only disk must not stop a service; warnings still reach stderr
    file_handler = PrivateRotatingFile(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    return path


def recent_problems(paths: list[Path], *, hours: int = 24, now: datetime | None = None) -> dict[str, Any]:
    """Errors and warnings in the last `hours`, from the tail of each current log file."""
    since = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    errors, warnings = 0, 0
    latest: dict[str, Any] | None = None
    for path in paths:
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - TAIL_BYTES))
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                at = datetime.fromisoformat(row["at"])
            except (ValueError, KeyError, TypeError):
                continue
            if at < since:
                continue
            errors += row.get("level") in {"ERROR", "CRITICAL"}
            warnings += row.get("level") == "WARNING"
            if row.get("level") in {"ERROR", "CRITICAL"} and (latest is None or at > latest["at"]):
                latest = {"at": at, "event": row.get("event"), "where": row.get("where"), "request_id": row.get("request_id")}
    return {"errors": errors, "warnings": warnings, "latest": {**latest, "at": latest["at"].isoformat()} if latest else None}
