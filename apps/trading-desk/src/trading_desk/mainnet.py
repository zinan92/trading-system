"""Hyperliquid Mainnet setup and dry-run status (trading-desk#1, trading-system#1277).

Park types the API wallet key and sub-account address into the desk himself. The key goes straight
into a 0600 file in the format standard-broker's LocalFileSecretProvider reads; it is never returned,
logged or stored in desk.db. Saving is Park's approval for the read-only observer to use that account.
Loss numbers and the lock belong to trading-system; the desk only shows them and forwards Park's unlock.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_KEY = re.compile(r"^(?:0x)?([0-9a-fA-F]{64})$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class SetupRefused(ValueError):
    """User-facing reason; never contains the submitted key."""


def mask(address: str | None) -> str | None:
    return f"{address[:6]}…{address[-4:]}" if address else None


def _release_sha(checkout: Path) -> str:
    try:
        sha = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupRefused("找不到正式盘用的 standard-broker 代码，先让执行员装好再保存") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SetupRefused("找不到正式盘用的 standard-broker 代码，先让执行员装好再保存")
    return sha


def _write_private(path: Path, text: str) -> None:
    """Create the temp file 0600 before any byte is written, then move it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_setup(config: Any, *, key: str, subaccount: str, now: datetime | None = None) -> dict[str, Any]:
    match = _KEY.fullmatch((key or "").strip())
    subaccount = (subaccount or "").strip()
    if not match:
        raise SetupRefused("密钥格式不对：应是 64 位十六进制（可带 0x）")
    if not _ADDRESS.fullmatch(subaccount):
        raise SetupRefused("子账户地址格式不对：应是 0x 开头的 40 位十六进制")
    release_sha = _release_sha(config.standard_broker_mainnet)
    now = now or datetime.now(timezone.utc)
    _write_private(config.mainnet_key, f"HYPERLIQUID_PK=0x{match.group(1)}\n")
    _write_private(config.mainnet_config, json.dumps({
        "subaccount": subaccount, "approval_id": f"park-mainnet-readonly-{now:%Y%m%d%H%M%S}",
        "approved_by": "park", "approved_at": now.isoformat(), "release_sha": release_sha,
    }, indent=2) + "\n")
    return {"ok": True, "subaccount": mask(subaccount)}


def _observer(config: Any) -> Any:
    if str(config.trading_system_checkout) not in sys.path:
        sys.path.insert(0, str(config.trading_system_checkout))
    from services import mainnet_observer  # noqa: PLC0415 - trading-system owns the loss rule and the lock
    return mainnet_observer


def status(config: Any) -> dict[str, Any]:
    subaccount = None
    try:
        subaccount = json.loads(config.mainnet_config.read_text(encoding="utf-8")).get("subaccount")
    except (OSError, ValueError):
        pass
    configured = bool(subaccount) and config.mainnet_key.exists()
    try:
        observed = _observer(config).mainnet_status(config.paper_output)
    except ImportError:
        return {"configured": configured, "subaccount": mask(subaccount), "state": "observer_missing",
                "reason": "交易系统还没有正式盘观察程序"}
    observed.pop("subaccount", None)
    return {**observed, "configured": configured, "subaccount": mask(subaccount)}


def unlock(config: Any, now: datetime | None = None) -> dict[str, Any]:
    return _observer(config).unlock(config.paper_output, operator="park", now=now or datetime.now(timezone.utc))
