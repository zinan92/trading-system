"""#1364: one redacted log format. A secret that reaches a message must not reach the file."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import stat

import pytest

from services import structured_log as sl

PRIVATE_KEY = "0x" + "ab" * 32
SECRETS = ("Bearer abc.def-123", "tsd_abcdefghijkl", "sk-abcdefghijklmnop1234", PRIVATE_KEY, "token=hunter2", 'password: "hunter2"', "passcode=4242")


@pytest.fixture
def log(tmp_path: Path):
    path = sl.configure("test", tmp_path)
    yield sl.get_logger("t"), path
    sl.configure("test", None)
    for handler in list(logging.getLogger(sl.ROOT).handlers):
        if not isinstance(handler, logging.NullHandler):
            logging.getLogger(sl.ROOT).removeHandler(handler)


def rows(path: Path) -> list[dict]:
    for handler in logging.getLogger(sl.ROOT).handlers:
        handler.flush()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("secret", SECRETS)
def test_secret_text_is_masked(secret: str) -> None:
    masked = sl.redact(f"failed with {secret} here")
    assert sl.REDACTED in masked and "hunter2" not in masked and "abcdefghij" not in masked and "4242" not in masked and "abab" not in masked


def test_a_wallet_address_and_a_digest_stay_readable() -> None:
    address, digest = "0x7b9d494f79217246b0C80576c26ceDdf2c315f04", "sha256:" + "ab" * 32
    assert sl.redact(f"{address} {digest}") == f"{address} {digest}"


@pytest.mark.parametrize("key", ["api_key", "API-KEY", "secret_file", "Authorization", "private_key", "cookie", "x_token", "passcode", "signature"])
def test_secret_fields_are_masked_whatever_they_hold(key: str) -> None:
    assert sl.redact({"nested": [{key: "/some/path"}]}) == {"nested": [{key: sl.REDACTED}]}


def test_a_secret_in_a_message_a_field_or_a_traceback_never_reaches_the_file(log) -> None:
    logger, path = log
    try:
        raise ValueError(f"boom token={PRIVATE_KEY}")
    except ValueError:
        logger.warning("caught %s", "Bearer abc.def-123", exc_info=True, extra={"fields": {"api_key": "plain-value", "note": PRIVATE_KEY}})
    text = path.read_text(encoding="utf-8")
    assert PRIVATE_KEY not in text and "abc.def-123" not in text and "plain-value" not in text
    row = rows(path)[0]
    assert "ValueError" in row["exception"] and "test_structured_log" in row["exception"]


def test_a_record_names_service_place_and_request(log) -> None:
    logger, path = log
    assert sl.set_request_id("abcdef012345") == "abcdef012345"
    sl.event(logger, "preview", asset="BTC")
    row = rows(path)[0]
    assert row["service"] == "test" and row["event"] == "preview" and row["asset"] == "BTC" and row["request_id"] == "abcdef012345"
    assert "test_a_record_names_service_place_and_request" in row["where"]
    sl.set_request_id("")


@pytest.mark.parametrize("value", [None, "", "short", "NOT-HEX-AT-ALL", "a" * 41, "abc\ndef01234"])
def test_a_bad_request_id_is_replaced(value) -> None:
    assert sl.set_request_id(value) != value and len(sl.request_id()) == 12


def test_the_file_is_private_and_configure_twice_does_not_double_write(tmp_path: Path) -> None:
    sl.configure("twice", tmp_path)
    path = sl.configure("twice", tmp_path)
    sl.get_logger("t").info("once")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(rows(path)) == 1
    sl.configure("twice", None)


def test_rotation_is_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sl, "MAX_BYTES", 400)
    monkeypatch.setattr(sl, "BACKUPS", 2)
    path = sl.configure("rot", tmp_path)
    for i in range(100):
        sl.get_logger("t").info("line %d", i)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rot.jsonl", "rot.jsonl.1", "rot.jsonl.2"]
    assert path.stat().st_size <= 400
    sl.configure("rot", None)


def test_an_unwritable_directory_does_not_stop_the_service(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert sl.configure("x", blocker / "logs") is None
    sl.get_logger("t").info("still fine")
    sl.configure("x", None)


def test_recent_problems_counts_the_window_and_names_the_latest(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    lines = [{"at": "2026-11-05T00:00:00+00:00", "level": "ERROR", "event": "old"},
             {"at": "2026-11-06T01:00:00+00:00", "level": "WARNING", "event": "w"},
             {"at": "2026-11-06T02:00:00+00:00", "level": "ERROR", "event": "first", "request_id": "1"},
             {"at": "2026-11-06T03:00:00+00:00", "level": "ERROR", "event": "last", "where": "x:y:1", "request_id": "2"},
             {"at": "2026-11-06T03:30:00+00:00", "level": "INFO", "event": "i"}]
    path.write_text("not json\n" + "".join(json.dumps(row) + "\n" for row in lines))
    out = sl.recent_problems([path, tmp_path / "missing.jsonl"], now=datetime(2026, 11, 6, 4, tzinfo=timezone.utc))
    assert (out["errors"], out["warnings"]) == (2, 1)
    assert out["latest"] == {"at": "2026-11-06T03:00:00+00:00", "event": "last", "where": "x:y:1", "request_id": "2"}
