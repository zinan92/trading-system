"""Read-only observer for Park's Hyperliquid Mainnet BTC sub-account (#1277).

Every minute it reads the sub-account's equity twice, through standard-broker's
read-only Mainnet profile and through Hyperliquid's public info API, computes
the loss numbers Park approved on 2026-09-15, and keeps a lock only Park can
clear. It has no order path: standard-broker's profile cannot write.

Loss rule:
- equity is the account value (realized, unrealized, fees and funding included);
- daily loss = equity at the last 08:00 Asia/Shanghai minus current equity, limit 25 USD;
- total loss = recorded starting equity minus current equity, limit 75 USD;
- reaching a limit writes a lock; Park can clear it only on a later local date.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen

MAINNET_INFO_URL = "https://api.hyperliquid.xyz/info"
DAILY_LOSS_LIMIT = Decimal("25")
TOTAL_LOSS_LIMIT = Decimal("75")
STALE_SECONDS = 120
RESET_HOUR = 8
REPORT_HOURS = 48
REPORT_MAX_DIFF = Decimal("0.50")
REPORT_MIN_COVERAGE = 0.95
SHANGHAI = timezone(timedelta(hours=8))
CONFIG_PATH = Path.home() / ".config/trading-system/hyperliquid-mainnet.json"
KEY_PATH = Path.home() / ".config/trading-system/hyperliquid-mainnet.pk"
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")

EquityReader = Callable[["MainnetConfig"], Decimal]


class MainnetObserverError(RuntimeError):
    """Redacted observer blocker; the message is a stable reason code."""


@dataclass(frozen=True)
class MainnetConfig:
    subaccount: str
    approval_id: str
    approved_by: str
    approved_at: datetime
    release_sha: str
    key_path: Path = KEY_PATH

    @classmethod
    def load(cls, path: Path = CONFIG_PATH, key_path: Path = KEY_PATH) -> "MainnetConfig | None":
        """Return None when Park has not set up the Mainnet account yet."""

        if not path.exists() or not key_path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            approved_at = datetime.fromisoformat(str(raw["approved_at"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MainnetObserverError("mainnet_config_invalid") from exc
        subaccount = str(raw.get("subaccount") or "")
        release_sha = str(raw.get("release_sha") or "")
        if not _ADDRESS.fullmatch(subaccount) or not _SHA.fullmatch(release_sha) or approved_at.tzinfo is None:
            raise MainnetObserverError("mainnet_config_invalid")
        if str(raw.get("approved_by") or "") != "park" or not str(raw.get("approval_id") or "").strip():
            raise MainnetObserverError("mainnet_config_invalid")
        return cls(subaccount=subaccount, approval_id=str(raw["approval_id"]), approved_by="park",
                   approved_at=approved_at, release_sha=release_sha, key_path=key_path)


# ---- readers -------------------------------------------------------------------------------

def public_equity(config: MainnetConfig, *, opener: Callable[..., Any] = urlopen, timeout: float = 8.0) -> Decimal:
    """Independent reader: Hyperliquid public clearinghouseState, not via standard-broker."""

    body = json.dumps({"type": "clearinghouseState", "user": config.subaccount}).encode()
    request = Request(MAINNET_INFO_URL, data=body, headers={"content-type": "application/json"}, method="POST")
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
        return _decimal(payload["marginSummary"]["accountValue"])
    except MainnetObserverError:
        raise
    except Exception as exc:  # noqa: BLE001 - any transport/shape failure is one redacted blocker
        raise MainnetObserverError("public_equity_unavailable") from exc


def broker_equity(config: MainnetConfig, *, client_factory: Callable[[str, str], object] | None = None) -> Decimal:
    """System-of-record reader through standard-broker's read-only Mainnet profile."""

    try:
        from standard_broker import (
            AccountReference,
            AccountScope,
            BrokerEnvironment,
            BrokerRuntimeSession,
            ExternalBrokerBuildContext,
            ExternalEnvironmentApproval,
            ExternalRuntimeIdentity,
            RuntimeActivationPolicy,
            SignerKind,
            SignerReference,
        )
        from standard_broker.adapters.hyperliquid.external import NAUTILUS_HYPERLIQUID_COMMIT, NAUTILUS_HYPERLIQUID_VERSION
        from standard_broker.adapters.hyperliquid import (
            HyperliquidMainnetReadOnlyBackendConfig,
            LocalFileSecretProvider,
            NautilusHyperliquidMainnetReadOnlyBackend,
            NautilusHyperliquidRuntime,
            NautilusRuntimeConfig,
            build_hyperliquid_mainnet_readonly_host,
            mainnet_btc_readonly_capabilities,
        )
        from standard_broker.adapters.hyperliquid.read_facts import HyperliquidExternalFactAdapter
        from standard_broker.external_host import ExternalHostRequest
        from standard_broker.host import CanonicalHostRequest, CanonicalPortQuery
        from standard_broker.market_data import FreshnessPolicy
    except ImportError as exc:
        raise MainnetObserverError("standard_broker_mainnet_unavailable") from exc

    lifecycle = f"mainnet-observer:{config.approval_id}"
    reference = "local-file://hyperliquid-mainnet"
    secrets = LocalFileSecretProvider({reference: config.key_path})
    capabilities = mainnet_btc_readonly_capabilities()
    session = BrokerRuntimeSession(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.MAINNET,
        account=AccountReference(AccountScope.SUBACCOUNT, config.subaccount),
        signer=SignerReference(SignerKind.API_AGENT, "local-file", reference),
        signer_provider=secrets,
        capabilities=capabilities,
        execution_scope="hypercore:default",
        lifecycle_id=lifecycle,
    )
    approval = ExternalEnvironmentApproval(
        environment=BrokerEnvironment.MAINNET,
        approval_id=config.approval_id,
        release_sha=config.release_sha,
        approved_by=config.approved_by,
        approved_at=config.approved_at,
        account_address=config.subaccount,
        lifecycle_id=lifecycle,
    )
    try:
        backend = NautilusHyperliquidMainnetReadOnlyBackend(
            session=session,
            config=HyperliquidMainnetReadOnlyBackendConfig(account_address=config.subaccount),
            secrets=secrets,
            client_factory=client_factory,
        )
        runtime = NautilusHyperliquidRuntime(
            session=session,
            backend=backend,
            config=NautilusRuntimeConfig(
                expected_version=NAUTILUS_HYPERLIQUID_VERSION,
                expected_commit=NAUTILUS_HYPERLIQUID_COMMIT,
                policy=RuntimeActivationPolicy(mainnet_approval=approval),
                expected_release_sha=config.release_sha,
            ),
        )
        runtime.start()
        context = ExternalBrokerBuildContext(
            session=session,
            runtime_identity=ExternalRuntimeIdentity(
                adapter_id="nautilus-hyperliquid",
                version=NAUTILUS_HYPERLIQUID_VERSION,
                commit=NAUTILUS_HYPERLIQUID_COMMIT,
                mapping_revision=capabilities.revision,
                transport_state="external_mainnet",
            ),
            release_sha=config.release_sha,
            approval=approval,
        )
        host = build_hyperliquid_mainnet_readonly_host(context=context, runtime=runtime)
        mapper = HyperliquidExternalFactAdapter(context=context, instruments=None,
                                                freshness_policy=FreshnessPolicy(timedelta(minutes=2)))
        envelope = host.read_fact(
            request=ExternalHostRequest(
                request_id=f"{lifecycle}:account",
                request=CanonicalHostRequest(port="account", operation="read", payload=CanonicalPortQuery(kind="account")),
            ),
            mapper=lambda raw: mapper.map_account(request_id=f"{lifecycle}:account", raw=raw),
        )
        return _decimal(envelope.data.equity)
    except MainnetObserverError:
        raise
    except Exception as exc:  # noqa: BLE001 - never surface provider text, it may echo request context
        raise MainnetObserverError(f"broker_equity_unavailable:{type(exc).__name__}") from exc


# ---- loss ----------------------------------------------------------------------------------

def trading_day(moment: datetime) -> str:
    """Asia/Shanghai date of the 08:00 session that contains ``moment``."""

    return (moment.astimezone(SHANGHAI) - timedelta(hours=RESET_HOUR)).date().isoformat()


def compute_loss(*, equity: Decimal | None, read_at: datetime | None, now: datetime,
                 baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Pure loss numbers from the current reading and the stored baselines."""

    if equity is None or read_at is None or (now - read_at).total_seconds() > STALE_SECONDS:
        return {"state": "stale", "daily_loss": None, "total_loss": None, "breach": None}
    day = baseline.get("day") or {}
    if day.get("trading_day") != trading_day(now):
        return {"state": "no_baseline", "daily_loss": None, "total_loss": None, "breach": None}
    daily = max(Decimal("0"), _decimal(day["equity"]) - equity)
    total = max(Decimal("0"), _decimal(baseline["starting_equity"]) - equity)
    breach = "total" if total >= TOTAL_LOSS_LIMIT else "daily" if daily >= DAILY_LOSS_LIMIT else None
    return {"state": "breach" if breach else "ok", "daily_loss": str(daily), "total_loss": str(total), "breach": breach}


def update_baseline(baseline: Mapping[str, Any], *, equity: Decimal, now: datetime) -> dict[str, Any]:
    """Record starting equity once and the day's equity at the first reading of each 08:00 session."""

    result = dict(baseline)
    if "starting_equity" not in result:
        result["starting_equity"] = str(equity)
        result["starting_at"] = now.isoformat()
    day = trading_day(now)
    if (result.get("day") or {}).get("trading_day") != day:
        local = now.astimezone(SHANGHAI)
        on_time = local.hour == RESET_HOUR and local.minute < 2
        result["day"] = {"trading_day": day, "equity": str(equity), "at": now.isoformat(),
                         "source": "08:00" if on_time else "first_reading_after_08:00"}
    return result


# ---- lock ----------------------------------------------------------------------------------

def read_lock(root: Path) -> dict[str, Any] | None:
    path = _dir(root) / "lock.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"kind": "unreadable", "triggered_at": None}  # unreadable lock stays locked
    return value if isinstance(value, dict) else {"kind": "unreadable", "triggered_at": None}


def trigger_lock(root: Path, *, kind: str, loss: Mapping[str, Any], equity: Decimal, now: datetime) -> dict[str, Any]:
    existing = read_lock(root)
    if existing is not None:
        return existing
    lock = {"kind": kind, "triggered_at": now.isoformat(), "trading_day": trading_day(now),
            "local_date": now.astimezone(SHANGHAI).date().isoformat(), "equity": str(equity),
            "daily_loss": loss.get("daily_loss"), "total_loss": loss.get("total_loss")}
    _write_json(_dir(root) / "lock.json", lock)
    _append(_dir(root) / "lock_history.jsonl", {"event": "locked", **lock})
    return lock


def unlock(root: Path, *, operator: str, now: datetime) -> dict[str, Any]:
    """Park clears the lock, only on a later Asia/Shanghai date than the trigger."""

    if operator != "park":
        return {"ok": False, "reason": "operator_not_park"}
    lock = read_lock(root)
    if lock is None:
        return {"ok": True, "reason": "not_locked"}
    local_date = now.astimezone(SHANGHAI).date().isoformat()
    if lock.get("local_date") is None or local_date <= str(lock["local_date"]):
        return {"ok": False, "reason": "same_day", "unlock_from": _next_date(lock.get("local_date"))}
    (_dir(root) / "lock.json").unlink()
    _append(_dir(root) / "lock_history.jsonl", {"event": "unlocked", "by": operator, "at": now.isoformat(), "lock": lock})
    return {"ok": True, "reason": "unlocked"}


# ---- one observation -----------------------------------------------------------------------

def observe_once(root: Path, *, now: datetime | None = None, config: MainnetConfig | None | bool = False,
                 broker_reader: EquityReader = broker_equity, public_reader: EquityReader = public_equity) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if config is False:
        try:
            config = MainnetConfig.load()
        except MainnetObserverError as exc:
            return _status(root, {"state": str(exc), "observed_at": now.isoformat()})
    if config is None:
        return _status(root, {"state": "not_configured", "observed_at": now.isoformat()})

    readings: dict[str, Any] = {}
    for name, reader in (("broker", broker_reader), ("public", public_reader)):
        try:
            readings[name] = reader(config)
        except MainnetObserverError as exc:
            readings[name] = None
            readings[f"{name}_error"] = str(exc)
    equity = readings.get("broker")
    diff = abs(equity - readings["public"]) if equity is not None and readings.get("public") is not None else None
    row = {"at": now.isoformat(), "broker_equity": _str(equity), "public_equity": _str(readings.get("public")),
           "diff": _str(diff), **{k: v for k, v in readings.items() if k.endswith("_error")}}
    _append(_dir(root) / "snapshots" / f"{now.astimezone(SHANGHAI).date().isoformat()}.jsonl", row)

    baseline_path = _dir(root) / "baseline.json"
    baseline = _read_json(baseline_path)
    if baseline_path.exists() and not baseline:
        # Never re-seed starting equity over a file we could not read: that would erase the total loss.
        return _status(root, {"state": "baseline_unreadable", "observed_at": now.isoformat(),
                              "subaccount": config.subaccount, "equity": _str(equity), "lock": read_lock(root),
                              "mode": "dry_run"})
    if equity is not None:
        baseline = update_baseline(baseline, equity=equity, now=now)
        _write_json(baseline_path, baseline)
    loss = compute_loss(equity=equity, read_at=now if equity is not None else None, now=now, baseline=baseline)
    lock = read_lock(root)
    if loss["breach"] and equity is not None:
        lock = trigger_lock(root, kind=loss["breach"], loss=loss, equity=equity, now=now)
    return _status(root, {"state": loss["state"], "observed_at": now.isoformat(), "subaccount": config.subaccount,
                          "equity": _str(equity), "public_equity": _str(readings.get("public")), "diff": _str(diff),
                          "daily_loss": loss["daily_loss"], "daily_limit": str(DAILY_LOSS_LIMIT),
                          "total_loss": loss["total_loss"], "total_limit": str(TOTAL_LOSS_LIMIT),
                          "starting_equity": baseline.get("starting_equity"), "day_baseline": baseline.get("day"),
                          "lock": lock, "errors": {k: v for k, v in readings.items() if k.endswith("_error")},
                          "mode": "dry_run"})


def mainnet_status(root: Path) -> dict[str, Any]:
    status = _read_json(_dir(root) / "status.json") or {"state": "not_configured"}
    status["lock"] = read_lock(root)
    status["report"] = dry_run_report(root)
    return status


def dry_run_report(root: Path, *, now: datetime | None = None, hours: int = REPORT_HOURS) -> dict[str, Any]:
    """Pass when both readers agree within 0.50 USD across >= 48 h with >= 95% of minutes present."""

    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    rows = []
    folder = _dir(root) / "snapshots"
    for path in sorted(folder.glob("*.jsonl")) if folder.exists() else ():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                at = datetime.fromisoformat(row["at"])
            except (ValueError, KeyError):
                continue
            if start <= at <= now:
                rows.append(row)
    first = min((datetime.fromisoformat(r["at"]) for r in rows), default=None)
    compared = [Decimal(r["diff"]) for r in rows if r.get("diff") is not None]
    expected = hours * 60
    coverage = len(compared) / expected
    max_diff = max(compared, default=None)
    covered_hours = (now - first).total_seconds() / 3600 if first else 0.0
    passed = bool(compared) and covered_hours >= hours - 0.1 and coverage >= REPORT_MIN_COVERAGE and max_diff <= REPORT_MAX_DIFF
    return {"passed": passed, "window_hours": hours, "covered_hours": round(covered_hours, 2),
            "minutes_compared": len(compared), "coverage": round(coverage, 4), "max_diff": _str(max_diff),
            "max_diff_allowed": str(REPORT_MAX_DIFF)}


# ---- helpers -------------------------------------------------------------------------------

def _dir(root: Path) -> Path:
    return Path(root) / "mainnet"


def _status(root: Path, status: dict[str, Any]) -> dict[str, Any]:
    _write_json(_dir(root) / "status.json", status)
    return status


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MainnetObserverError("equity_invalid") from exc
    if not result.is_finite():
        raise MainnetObserverError("equity_invalid")
    return result


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _next_date(local_date: object) -> str | None:
    try:
        return (datetime.fromisoformat(str(local_date)).date() + timedelta(days=1)).isoformat()
    except ValueError:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _append(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
