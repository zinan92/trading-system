"""Deterministic Park Telegram proposal/confirmation worker.

This worker is intentionally a pre-execution seam.  It can create a clean
Park proposal and record an exact confirmation, but it cannot submit an order.
The later Paper execution story must consume the immutable confirmation
capability behind its own cutover gate.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from services.journal_store import load_json, write_json
from services.park_conversation_contract import (
    extract_explicit_strategy_patch,
    has_explicit_execution_intent,
)
from services.park_codex_intent_parser import (
    deterministic_legacy_clean_slate_candidate,
    deterministic_neutral_grid_candidate,
)
from services.park_confirmation import (
    ParkConfirmationError,
    ParkConfirmationLedger,
    parse_confirmation_command,
    parse_confirmation_shortcut,
)
from services.park_legacy_cutover import (
    ParkLegacyCutoverError,
    ParkLegacyCutoverLedger,
    parse_legacy_cutover_digest,
)
from services.park_strategy_lifecycle import admit_clean_slate
from services.park_strategy_plan import (
    ParkStrategyPlanError,
    build_deterministic_risk_plan,
    normalize_park_input,
)
from services.park_strategy_preview import build_deterministic_risk_preview
from services.park_strategy_session import (
    ParkStrategyIdentityError,
    ParkStrategyIdentityJournal,
    recording_window,
)
from services.park_telegram_control import ParkTelegramControlError, ParkTelegramLedger
from services.park_telegram_continuation import (
    ParkContinuationError,
    ParkTelegramContinuationLedger,
)
from services.park_telegram_conversation import (
    ParkTelegramConversationAgent,
    ParkTelegramConversationLedger,
)
from services.telegram_bot_transport import (
    TelegramBotTransport,
    TelegramBotTransportError,
)
from services.testnet_soak_readiness import TestnetSoakReadiness
from services.live_activation_gate import LiveActivationGate

PARK_TELEGRAM_RUNTIME_SCHEMA = "park-telegram-runtime-v1"


class ParkTelegramRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _default_cycle_id(now: str) -> str:
    return str(recording_window(now)["record_window_id"])


def default_market_reader(*, output_root: Path | str | None = None) -> dict[str, Any]:
    """Read one trusted market envelope without inventing a current price."""

    from services.datafeed_execution_market_client import (
        DatafeedExecutionMarketClient,
        ExecutionMarketUnavailable,
        execution_market_payload,
        market_source_mode,
        write_compare_receipt,
    )

    mode = market_source_mode()
    if mode in {"dual", "datafeed"}:
        client = DatafeedExecutionMarketClient()
        try:
            payload = client.read(venue="binance", instrument_id="XAUUSDT.BINANCE")
            datafeed = execution_market_payload(
                payload,
                source="binance_usdm_futures",
                provider="binance_usdm_futures",
                environment="production",
                instrument_id="XAUUSDT.BINANCE",
                symbol="XAUUSDT",
            )
        except ExecutionMarketUnavailable as exc:
            if mode == "datafeed":
                raise ParkTelegramRuntimeError("market_unavailable", str(exc)) from exc
            datafeed = {"price": None, "observed_at": None, "fresh": False, "age_seconds": None, "reason": str(exc)}
        if mode == "datafeed":
            if datafeed.get("trusted") is not True or datafeed.get("fresh") is not True:
                raise ParkTelegramRuntimeError("market_unavailable", "execution market is not trusted and fresh")
            return datafeed

    try:
        from pipelines.dashboard_server import build_dualtrack_market_bars_response

        source = dict(build_dualtrack_market_bars_response(timeframe="1m", limit=240))
    except Exception as exc:  # noqa: BLE001 - turned into a typed worker blocker.
        raise ParkTelegramRuntimeError("market_unavailable", type(exc).__name__) from exc
    direct = {
        "price": source.get("latest_close"),
        "trusted": source.get("status") in {"ready", "derived"}
        and source.get("is_synthetic") is False
        and bool(source.get("provider") or source.get("source_mode")),
        "fresh": bool(source.get("fresh")),
        "source": str(source.get("provider") or source.get("source_mode") or ""),
        "observed_at": str(source.get("latest_timestamp") or ""),
        "provider": str(source.get("provider") or ""),
        "raw_status": str(source.get("status") or ""),
    }
    if mode == "dual":
        write_compare_receipt(
            output_root or os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", "outputs"),
            mode=mode,
            direct=direct,
            datafeed=datafeed,
        )
    return direct


def mark_paper_account_to_market(
    account: Mapping[str, Any],
    positions: list[Mapping[str, Any]],
    market: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Project current Paper NAV from authoritative cash and a trusted mark.

    The execution snapshot's ``equity`` is an historical mark.  It is useful
    evidence, but it must not be presented as current while positions remain
    open.  This helper is deliberately read-only and fail-closed: a missing,
    stale, synthetic, or otherwise untrusted mark yields no current NAV.
    """

    projected = dict(account)
    projected["historical_snapshot_equity"] = account.get("equity")
    projected["nav_is_current"] = False
    projected["nav_status"] = "blocked"
    projected["nav_blocker"] = "current_market_required"
    marked_positions = [dict(row) for row in positions]

    # Direct historical readers may not have a market seam.  Keep the
    # historical value explicitly labelled for compatibility, but never call
    # it current NAV.  Production Telegram/Dashboard paths always pass the
    # same market envelope used for their read-only context.
    if market is None:
        if marked_positions:
            projected["equity"] = None
            projected["unrealized_pnl"] = None
            projected["nav_status"] = "blocked"
            projected["nav_blocker"] = "current_market_required"
            return projected, marked_positions
        projected["nav_status"] = "historical_snapshot_only"
        return projected, marked_positions

    if not marked_positions:
        try:
            ending_cash = float(account.get("ending_cash"))
        except (TypeError, ValueError):
            projected["equity"] = None
            projected["unrealized_pnl"] = None
            projected["nav_blocker"] = "authoritative_ending_cash_missing"
            return projected, marked_positions
        if not math.isfinite(ending_cash):
            projected["equity"] = None
            projected["unrealized_pnl"] = None
            projected["nav_blocker"] = "authoritative_ending_cash_invalid"
            return projected, marked_positions
        projected["equity"] = round(ending_cash, 8)
        projected["unrealized_pnl"] = 0.0
        projected["nav_is_current"] = True
        projected["nav_status"] = "cash_only"
        projected.pop("nav_blocker", None)
        return projected, marked_positions

    market = dict(market or {})
    try:
        mark_price = float(market.get("price", market.get("latest_close")))
    except (TypeError, ValueError):
        mark_price = math.nan
    trusted = (
        market.get("trusted") is True
        and market.get("fresh") is True
        and math.isfinite(mark_price)
        and mark_price > 0
    )
    if not trusted:
        projected["equity"] = None
        projected["unrealized_pnl"] = None
        projected["nav_blocker"] = (
            "market_not_fresh"
            if market.get("trusted") is True and market.get("fresh") is not True
            else "market_not_authoritative"
        )
        return projected, marked_positions

    try:
        ending_cash = float(account.get("ending_cash"))
    except (TypeError, ValueError):
        projected["equity"] = None
        projected["unrealized_pnl"] = None
        projected["nav_blocker"] = "authoritative_ending_cash_missing"
        return projected, marked_positions
    if not math.isfinite(ending_cash):
        projected["equity"] = None
        projected["unrealized_pnl"] = None
        projected["nav_blocker"] = "authoritative_ending_cash_invalid"
        return projected, marked_positions

    unrealized = 0.0
    for row in marked_positions:
        if str(row.get("status") or "").lower() != "open":
            continue
        try:
            entry_price = float(row.get("entry_price"))
            units = float(row.get("remaining_units", row.get("units")))
        except (TypeError, ValueError):
            projected["equity"] = None
            projected["unrealized_pnl"] = None
            projected["nav_blocker"] = "open_position_mark_fields_missing"
            return projected, marked_positions
        if not math.isfinite(entry_price) or not math.isfinite(units) or units < 0:
            projected["equity"] = None
            projected["unrealized_pnl"] = None
            projected["nav_blocker"] = "open_position_mark_fields_invalid"
            return projected, marked_positions
        side = str(row.get("side") or "").lower()
        if side not in {"long", "short"}:
            projected["equity"] = None
            projected["unrealized_pnl"] = None
            projected["nav_blocker"] = "open_position_side_missing"
            return projected, marked_positions
        position_unrealized = (mark_price - entry_price) * units
        if side == "short":
            position_unrealized *= -1.0
        row["unrealized_pnl"] = round(position_unrealized, 8)
        row["mark_price"] = mark_price
        unrealized += position_unrealized

    projected["unrealized_pnl"] = round(unrealized, 8)
    projected["equity"] = round(ending_cash + unrealized, 8)
    projected["nav_is_current"] = True
    projected["nav_status"] = "marked_to_market"
    projected.pop("nav_blocker", None)
    projected["mark"] = {
        "price": mark_price,
        "fresh": True,
        "trusted": True,
        "source": str(market.get("source") or market.get("provider") or "market"),
        "observed_at": str(market.get("observed_at") or market.get("latest_timestamp") or ""),
    }
    return projected, marked_positions


def default_account_reader(
    output_root: Path,
    cycle_id: str,
    *,
    config: Mapping[str, Any] | None = None,
    market: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the Park-authoritative Paper snapshot; never use a fake equity.

    Park admission must use the same direct Paper authority that the Park
    runtime will execute against.  The legacy configured execution builder
    carries the DualTrack Shadow cutover gate and is therefore not a valid
    account-read path for this single-track control plane.
    """

    try:
        from services.park_paper_runtime import build_park_authoritative_adapter

        build_kwargs: dict[str, Any] = {"config": config}
        # The initial migration must be able to inspect legacy Paper state
        # while the Park execution flag is still default-off.  The returned
        # adapter remains mutation-gated; this is a read-only composition
        # escape hatch, never an execution authorization.
        if isinstance(config, Mapping) and config.get("feature_enabled") is False:
            build_kwargs["allow_disabled_read"] = True
        adapter = build_park_authoritative_adapter(Path(output_root), **build_kwargs).adapter
        snapshot = dict(adapter.snapshot(cycle_id))
        reconciliation = dict(adapter.reconcile(cycle_id))
    except Exception as exc:  # noqa: BLE001 - turned into a typed worker blocker.
        raise ParkTelegramRuntimeError("paper_account_unavailable", type(exc).__name__) from exc
    positions = [
        dict(row)
        for row in snapshot.get("positions") or []
        if str(row.get("status") or "").lower() == "open"
    ]
    orders = [
        dict(row)
        for row in snapshot.get("orders") or []
        if str(row.get("state") or "").lower() == "accepted"
    ]
    # Clean-slate admission is account-wide, not recording-window-scoped.  A
    # legacy cycle in another namespace must remain visible and block Park;
    # never silently adopt, cancel, or flatten it.
    account_wide_orders: dict[str, dict[str, Any]] = {
        str(row.get("order_id") or f"current-order-{index}"): {
            **dict(row),
            "cycle_id": str(row.get("cycle_id") or cycle_id),
        }
        for index, row in enumerate(orders)
    }
    account_wide_positions: dict[str, dict[str, Any]] = {
        str(row.get("position_id") or row.get("trade_id") or f"current-position-{index}"): dict(row)
        for index, row in enumerate(positions)
    }
    account_wide_reconciliation_ok = reconciliation.get("status") == "ok" and not reconciliation.get("issues")
    adapter_root = getattr(adapter, "output_root", None)
    account_candidates: list[tuple[float, str, dict[str, Any]]] = []
    if adapter_root is not None:
        snapshot_dir = Path(adapter_root) / "dualtrack" / "nautilus_authoritative" / "snapshots"
        for path in sorted(snapshot_dir.glob("*.json")):
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
                row = rows[-1] if isinstance(rows, list) and rows else rows
                if not isinstance(row, Mapping) or not isinstance(row.get("orders"), list) or not isinstance(row.get("positions"), list):
                    raise ValueError("account_snapshot_shape_invalid")
                snapshot_cycle_id = str(row.get("cycle_id") or path.stem).strip()
                if not snapshot_cycle_id:
                    raise ValueError("account_snapshot_cycle_id_missing")
                if isinstance(row.get("account"), Mapping) and row.get("account", {}).get("equity") not in (None, ""):
                    account_candidates.append(
                        (path.stat().st_mtime, snapshot_cycle_id, dict(row["account"]))
                    )
                for order in row["orders"]:
                    if not isinstance(order, Mapping):
                        raise ValueError("account_snapshot_order_invalid")
                    if str(order.get("state") or "").lower() == "accepted" and str(order.get("order_id") or ""):
                        account_wide_orders[str(order["order_id"])] = {
                            **dict(order),
                            "cycle_id": str(order.get("cycle_id") or snapshot_cycle_id),
                        }
                for position in row["positions"]:
                    if not isinstance(position, Mapping):
                        raise ValueError("account_snapshot_position_invalid")
                    if str(position.get("status") or "").lower() == "open":
                        key = str(position.get("position_id") or position.get("trade_id") or "")
                        if key:
                            account_wide_positions[key] = dict(position)
                try:
                    cycle_reconciliation = dict(adapter.reconcile(snapshot_cycle_id))
                except Exception as exc:  # noqa: BLE001 - unknown account facts block clean-slate admission.
                    raise ParkTelegramRuntimeError("paper_account_reconciliation_invalid", type(exc).__name__) from exc
                account_wide_reconciliation_ok = account_wide_reconciliation_ok and cycle_reconciliation.get("status") == "ok" and not cycle_reconciliation.get("issues")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ParkTelegramRuntimeError("paper_account_snapshot_invalid", type(exc).__name__) from exc
    account = dict(snapshot.get("account") or {})
    selected_account_cycle = str(cycle_id)
    active_identity = ParkStrategyIdentityJournal(Path(output_root)).active_session()
    active_snapshot_cycle = (
        f"park-session-{active_identity.get('strategy_session_id')}"
        if isinstance(active_identity, Mapping) and active_identity.get("strategy_session_id")
        else ""
    )
    preferred = [
        candidate
        for candidate in account_candidates
        if active_snapshot_cycle and candidate[1] == active_snapshot_cycle
    ]
    if preferred:
        _mtime, selected_account_cycle, account = preferred[-1]
    elif account_candidates:
        _mtime, selected_account_cycle, account = max(account_candidates, key=lambda item: item[0])
    account, marked_positions = mark_paper_account_to_market(
        account,
        list(account_wide_positions.values()),
        market,
    )
    snapshot["account"] = dict(account)
    snapshot["account_source"] = f"authoritative_snapshot:{selected_account_cycle}"
    runtime = {}
    try:
        from services.strategy_control_plane import StrategyControlPlane

        runtime = StrategyControlPlane(Path(output_root)).runtime_state(cycle_id)
    except Exception:
        runtime = {}
    last_control_event = runtime.get("last_control_event")
    last_control_event = last_control_event if isinstance(last_control_event, Mapping) else {}
    runtime_after = last_control_event.get("runtime_after")
    runtime_after = runtime_after if isinstance(runtime_after, Mapping) else {}
    proven_stale_record = (
        runtime.get("stale_cycle") is True
        and runtime.get("previous_runtime_unresolved") is False
        and str(runtime_after.get("actual_state") or "") == "stopped"
        and str(runtime_after.get("desired_state") or "") == "stopped"
    )
    legacy_cutover_completion: dict[str, Any] | None = None
    cutover_path = Path(output_root) / "park_strategy" / "legacy_cutover.jsonl"
    try:
        cutover_rows = [
            json.loads(line)
            for line in cutover_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ] if cutover_path.exists() else []
        latest_terminal = next(
            (row for row in reversed(cutover_rows) if isinstance(row, Mapping) and row.get("event") in {"completed", "blocked"}),
            None,
        )
        quarantine = latest_terminal.get("legacy_runner_quarantine") if isinstance(latest_terminal, Mapping) else None
        quarantine = quarantine if isinstance(quarantine, Mapping) else {}
        ownership = quarantine.get("ownership") if isinstance(quarantine.get("ownership"), Mapping) else {}
        if (
            latest_terminal
            and latest_terminal.get("event") == "completed"
            and latest_terminal.get("clean_slate_verified") is True
            and latest_terminal.get("enabled_park_paper") is True
            and quarantine.get("ok") is True
            and quarantine.get("park_control") is True
            and quarantine.get("legacy_cycle_runner") is False
            and ownership.get("ok") is True
        ):
            # A completed exact-set clean-slate receipt is the durable proof
            # that the legacy namespace was reconciled and quarantined.  It
            # intentionally supersedes stale historical runtime metadata
            # (`previous_runtime_unresolved`) from that namespace; otherwise
            # the same completed cutover could never admit the first new Park
            # proposal.  Current positions/orders and the active Park identity
            # are still checked independently below.
            legacy_cutover_completion = dict(latest_terminal)
    except (OSError, ValueError, json.JSONDecodeError):
        legacy_cutover_completion = None
    unresolved = legacy_cutover_completion is None and (
        bool(runtime.get("previous_runtime_unresolved"))
        or (
            str(runtime.get("actual_state") or "") in {"starting", "running", "replanning", "stopping"}
            and not proven_stale_record
        )
    )
    return {
        "equity": account.get("equity"),
        "unrealized_pnl": account.get("unrealized_pnl"),
        "nav_is_current": account.get("nav_is_current") is True,
        "nav_status": account.get("nav_status"),
        "nav_blocker": account.get("nav_blocker"),
        "mark": dict(account.get("mark") or {}),
        "reconciliation_healthy": account_wide_reconciliation_ok,
        "open_positions": len(account_wide_positions),
        "open_or_accepted_orders": len(account_wide_orders),
        "unresolved_runtime": unresolved,
        "legacy_runtime_stale_record": proven_stale_record,
        "legacy_cutover_completed": legacy_cutover_completion is not None,
        "pending_terminal_actions": False,
        "snapshot": {
            **snapshot,
            "account_wide_legacy_exposure": {
                "orders": list(account_wide_orders.values()),
                "positions": marked_positions,
                "ownership": "legacy_cycle_or_unknown",
            },
        },
        "positions": marked_positions,
        "orders": list(account_wide_orders.values()),
        "reconciliation": reconciliation,
    }


class _Cursor:
    def __init__(self, output_root: Path) -> None:
        self.path = Path(output_root) / "park_strategy" / "telegram_cursor.json"

    def read(self) -> int | None:
        rows = load_json(self.path)
        if not rows or not isinstance(rows[-1], dict):
            return None
        value = rows[-1].get("next_offset")
        return int(value) if value not in (None, "") else None

    def advance(self, update_id: int) -> dict[str, Any]:
        next_offset = int(update_id) + 1
        row = {
            "schema_version": PARK_TELEGRAM_RUNTIME_SCHEMA,
            "event": "cursor_advanced",
            "last_update_id": int(update_id),
            "next_offset": next_offset,
            "recorded_at": _utc_now(),
        }
        write_json(self.path, [row])
        return row


class ParkTelegramRouter:
    """Translate Telegram text into durable Park proposal/receipt facts."""

    def __init__(
        self,
        output_root: Path,
        *,
        park_user_id: str,
        chat_id: str,
        market_reader: Callable[[], Mapping[str, Any]] | None = None,
        testnet_market_reader: Callable[[], Mapping[str, Any]] | None = None,
        account_reader: Callable[[Path, str], Mapping[str, Any]] | None = None,
        testnet_account_reader: Callable[[Path, str], Mapping[str, Any]] | None = None,
        now: Callable[[], str] | None = None,
        cycle_id_provider: Callable[[str], str] | None = None,
        confirmation_ttl_seconds: int = 900,
        intent_parser: Any | None = None,
        config: Mapping[str, Any] | None = None,
        testnet_start_handler: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.telegram = ParkTelegramLedger(self.output_root, park_user_id=park_user_id, chat_id=chat_id)
        self.live_activation = LiveActivationGate(
            self.output_root,
            park_user_id=park_user_id,
            park_chat_id=chat_id,
        )
        self.result_path = self.output_root / "park_strategy" / "telegram_results.jsonl"
        self.identity = ParkStrategyIdentityJournal(self.output_root)
        self.confirmations = ParkConfirmationLedger(self.output_root, park_user_id=park_user_id)
        self.legacy_cutover = ParkLegacyCutoverLedger(
            self.output_root,
            park_user_id=park_user_id,
            chat_id=chat_id,
        )
        self.confirmation_ttl_seconds = max(60, int(confirmation_ttl_seconds))
        self.continuations = ParkTelegramContinuationLedger(
            self.output_root,
            park_user_id=park_user_id,
            chat_id=chat_id,
            ttl_seconds=self.confirmation_ttl_seconds,
        )
        self.conversation_ledger = ParkTelegramConversationLedger(
            self.output_root,
            park_user_id=park_user_id,
            chat_id=chat_id,
            ttl_seconds=max(self.confirmation_ttl_seconds, 12 * 60 * 60),
        )
        self._uses_default_market_reader = market_reader is None
        self.market_reader = market_reader or (lambda: default_market_reader(output_root=self.output_root))
        self.testnet_market_reader = testnet_market_reader
        self.config = dict(config or {})
        self.testnet_start_handler = testnet_start_handler
        self._uses_default_account_reader = account_reader is None
        self.account_reader = account_reader or (
            lambda root, cycle: default_account_reader(root, cycle, config=self.config)
        )
        self.testnet_account_reader = testnet_account_reader
        self.now = now or _utc_now
        self.cycle_id_provider = cycle_id_provider or _default_cycle_id
        # Offline/tests may omit this seam.  The production pipeline supplies
        # a bounded Codex CLI parser explicitly; its output is untrusted.
        self.intent_parser = intent_parser
        self.conversation_agent = ParkTelegramConversationAgent(
            self.conversation_ledger,
            intent_parser,
        ) if intent_parser is not None else None
        self.provider_path = self.output_root / "park_strategy" / "provider_calls.jsonl"

    def _read_account(
        self,
        cycle_id: str,
        *,
        market: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if isinstance(market, Mapping) and market.get("environment") == "testnet":
            if self.testnet_account_reader is None:
                raise ParkTelegramRuntimeError(
                    "testnet_account_unavailable",
                    "Hyperliquid Testnet account facts are not configured",
                )
            return self.testnet_account_reader(self.output_root, cycle_id)
        if self._uses_default_account_reader:
            return default_account_reader(
                self.output_root,
                cycle_id,
                config=self.config,
                market=market,
            )
        return self.account_reader(self.output_root, cycle_id)

    @staticmethod
    def _is_testnet_text(text: str | None) -> bool:
        return bool(re.search(r"\btestnet\b|测试网", str(text or ""), re.IGNORECASE))

    def _read_market(self, text: str | None = None) -> Mapping[str, Any]:
        if self._is_testnet_text(text):
            if self.testnet_market_reader is None:
                if not self._uses_default_market_reader:
                    # Preserve explicitly injected test/integration seams;
                    # production always supplies the source-bound reader.
                    return self.market_reader()
                raise ParkTelegramRuntimeError(
                    "testnet_market_unavailable",
                    "Hyperliquid Testnet market reader is not configured",
                )
            try:
                market = dict(self.testnet_market_reader())
            except ParkTelegramRuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001 - public read failures are typed.
                raise ParkTelegramRuntimeError("testnet_market_unavailable", type(exc).__name__) from exc
            if (
                market.get("environment") != "testnet"
                or market.get("source") != "hyperliquid.external_testnet"
                or market.get("provider") != "hyperliquid"
                or market.get("instrument_id") != "BTC-USD-PERP"
            ):
                raise ParkTelegramRuntimeError(
                    "testnet_market_identity_mismatch",
                    "Hyperliquid Testnet market identity does not match BTC-USD-PERP",
                )
            if market.get("trusted") is not True or market.get("fresh") is not True:
                raise ParkTelegramRuntimeError(
                    "testnet_market_not_authoritative",
                    "Hyperliquid Testnet market facts are not trusted and fresh",
                )
            return market
        return self.market_reader()

    def handle_update(self, update: Mapping[str, Any]) -> dict[str, Any]:
        update_id = update.get("update_id")
        update_digest = _digest(update)
        prior = self._previous_result(update_id)
        if prior is not None:
            prior_digest = str(prior.get("_update_digest") or "")
            result = dict(prior.get("result") or {})
            if prior_digest and prior_digest != update_digest:
                self.telegram.record_rejected_update(
                    update=update,
                    code="duplicate_update_conflict",
                    detail="update_id was previously processed with different content",
                )
                return {
                    "status": "blocked",
                    "code": "duplicate_update_conflict",
                    "next_action": "notify_park_and_wait",
                }
            return result
        active = self.identity.active_session()
        binding = (
            {
                "strategy_session_id": active["strategy_session_id"],
                "strategy_revision_id": active["strategy_revision_id"],
            }
            if active
            else None
        )
        try:
            received = self.telegram.ingest_update(update, binding=binding)
        except ParkTelegramControlError as exc:
            result = self.telegram.record_rejected_update(update=update, code=exc.code, detail=str(exc))
            return self._remember_result(update_id, result, update_digest=update_digest)
        text = _safe_text(received.get("text"))
        legacy_candidate = deterministic_legacy_clean_slate_candidate(text)
        legacy_digest = parse_legacy_cutover_digest(text)
        if legacy_candidate is not None:
            result = self._handle_legacy_cutover(
                text,
                candidate=legacy_candidate,
                active=active,
                update_id=received.get("update_id"),
                text_digest=str(received.get("text_digest") or _digest(text)),
            )
        elif legacy_digest and self.legacy_cutover.proposal_for_digest(legacy_digest):
            result = self._handle_legacy_cutover_confirmation(
                legacy_digest,
                update_id=received.get("update_id"),
            )
        elif text.lower().startswith(("confirm live",)) or text.startswith("确认 live"):
            result = self._handle_live_activation_confirmation(text, received=received)
        elif text.lower().startswith(("confirm", "reject")) or text.startswith(("确认", "拒绝")):
            result = self._handle_confirmation(text, active=active, update_id=received.get("update_id"))
        else:
            result = self._handle_conversation_or_strategy(
                text,
                active=active,
                update_id=received.get("update_id"),
            )
        return self._remember_result(update_id, result, update_digest=update_digest)

    def _handle_live_activation_confirmation(self, text: str, *, received: Mapping[str, Any]) -> dict[str, Any]:
        """Route the exact Live activation command through the source-bound gate."""

        try:
            proposals = [row for row in self.live_activation.rows() if row.get("event") == "activation_proposed"]
        except Exception as exc:  # activation journal uncertainty is fail-closed.
            return self._block(
                code="live_activation_journal_unreadable",
                message=f"Live activation journal is unreadable: {type(exc).__name__}",
                binding=None,
                idempotency_key=f"live-activation-journal:{received.get('update_id')}",
            )
        proposal = proposals[-1] if proposals else None
        tokens = str(text or "").strip().split()
        activation_digest = tokens[2] if len(tokens) > 2 else ""
        if proposal is None:
            return self._block(
                code="live_activation_missing",
                message="No source-bound Live activation proposal is waiting for confirmation.",
                binding=None,
                idempotency_key=f"live-activation-missing:{received.get('update_id')}",
            )
        decision = self.live_activation.confirm(
            activation_digest=activation_digest,
            command_text=text,
            park_user_id=self.telegram.park_user_id,
            telegram_update_id=received.get("update_id"),
            telegram_message_id=received.get("message_id"),
            telegram_chat_id=received.get("chat_id"),
            telegram_receipt=received,
            current_preflight=proposal.get("preflight") if isinstance(proposal.get("preflight"), Mapping) else None,
            now=time.time(),
        )
        if decision.get("event") == "activation_confirmed":
            outbound = self.telegram.queue_outbound(
                idempotency_key=f"live-activation-confirmed:{decision.get('activation_digest')}",
                message_type="live_activation_receipt",
                text=(
                    "Live activation intent recorded for the exact release/account/plan. "
                    "Live writes remain disabled until the separately attended DCA canary passes."
                ),
                binding=None,
            )
            return {"status": "live_activation_confirmed", "decision": decision, "outbound": outbound, "execution_authorized": False}
        return {
            "status": "blocked",
            "code": str(decision.get("code") or "live_activation_rejected"),
            "next_action": "notify_park_and_wait",
            "decision": decision,
            "execution_authorized": False,
        }

    def recover_pending_legacy_cutovers(self) -> list[dict[str, Any]]:
        """Reprocess an explicit legacy phrase already ingested before a fix.

        Telegram's cursor is intentionally durable.  A provider failure may
        therefore leave a valid Park instruction behind the cursor.  Recovery
        is limited to that exact deterministic phrase and only replaces the
        prior typed parser result; it never replays arbitrary old commands.
        """

        if any(row.get("event") == "completed" for row in self.legacy_cutover.rows()):
            # The legacy migration is sealed. Do not replay its historical
            # message on every later tick and emit stale count-mismatch noise.
            return []
        recovered: list[dict[str, Any]] = []
        for received in self.telegram.inbox_rows():
            if received.get("event") != "inbound_received":
                continue
            candidate = deterministic_legacy_clean_slate_candidate(str(received.get("text") or ""))
            if candidate is None:
                continue
            update_id = received.get("update_id")
            prior = self._previous_result(update_id)
            prior_result = dict(prior.get("result") or {}) if prior else {}
            prior_proposal = prior_result.get("proposal") if isinstance(prior_result.get("proposal"), Mapping) else {}
            prior_proposal_id = str(prior_proposal.get("proposal_id") or "")
            pending_ids = {
                str(row.get("proposal_id") or "")
                for row in self.legacy_cutover.confirmed_pending()
            }
            if (
                prior_result.get("status") in {"legacy_cutover_confirmed", "legacy_cutover_proposal_created"}
                and prior_proposal_id
                and prior_proposal_id in pending_ids
            ):
                continue
            active = self.identity.active_session()
            result = self._handle_legacy_cutover(
                str(received.get("text") or ""),
                candidate=candidate,
                active=active,
                update_id=update_id,
                text_digest=str(received.get("text_digest") or _digest(str(received.get("text") or ""))),
            )
            self._remember_result(
                update_id,
                result,
                update_digest=str((prior or {}).get("_update_digest") or _digest(received)),
            )
            recovered.append(result)
        return recovered

    def recover_pending_strategy_inputs(self) -> list[dict[str, Any]]:
        """Reprocess a strategy message misclassified by an unavailable provider."""

        recovered: list[dict[str, Any]] = []
        for received in self.telegram.inbox_rows():
            if received.get("event") != "inbound_received":
                continue
            update_id = received.get("update_id")
            prior = self._previous_result(update_id)
            prior_result = dict(prior.get("result") or {}) if prior else {}
            prior_code = str(prior_result.get("code") or "")
            if prior_code not in {"ambiguous_strategy_type", "missing_risk_authority", "dca_exit_levels_missing", "clean_slate_blocked"}:
                if prior_code != "missing_direction":
                    continue
                try:
                    if self.continuations.latest_pending() is None:
                        continue
                except ParkContinuationError:
                    continue
            recovery_key = f"park-strategy-rejected:{update_id}:strategy-recovery"
            if any(row.get("idempotency_key") == recovery_key for row in self.telegram.outbox_rows()):
                continue
            active = self.identity.active_session()
            result = self._handle_strategy(
                _safe_text(received.get("text")),
                active=active,
                # Keep the original Telegram update id in the durable result,
                # but give the repaired response a fresh outbound key so an
                # old misclassification cannot mask the new guidance text.
                update_id=f"{update_id}:strategy-recovery",
            )
            self._remember_result(
                update_id,
                result,
                update_digest=str((prior or {}).get("_update_digest") or _digest(received)),
            )
            recovered.append(result)
        return recovered

    def _handle_legacy_cutover(
        self,
        text: str,
        *,
        candidate: Mapping[str, Any],
        active: Mapping[str, Any] | None,
        update_id: Any,
        text_digest: str,
    ) -> dict[str, Any]:
        """Record and, for the bounded operator phrase, confirm exact cleanup."""

        if active:
            return self._block(
                code="legacy_cutover_active_strategy",
                message="已有 Park 策略处于 active，不能把旧挂单清理请求混入策略切换；先等当前策略终态。",
                binding=active,
                idempotency_key=f"park-legacy-cutover-blocked:{update_id}",
            )
        expected = candidate.get("expected_order_count")
        if expected in (None, ""):
            return self._block(
                code="legacy_order_count_missing",
                message="我需要旧挂单的明确数量才能建立精确清理清单。例如：取消这19个旧挂单，确认 clean slate，启用 Park Paper。",
                binding=None,
                idempotency_key=f"park-legacy-cutover-count:{update_id}",
            )
        try:
            expected_count = int(expected)
            facts = dict(self._read_account(self.cycle_id_provider(self.now())))
            if facts.get("reconciliation_healthy") is not True:
                raise ParkLegacyCutoverError(
                    "legacy_reconciliation_unhealthy",
                    "authoritative Paper reconciliation is not healthy across the discovered namespaces",
                )
            snapshot = facts.get("snapshot") if isinstance(facts.get("snapshot"), Mapping) else {}
            exposure = snapshot.get("account_wide_legacy_exposure") if isinstance(snapshot, Mapping) else {}
            exposure = exposure if isinstance(exposure, Mapping) else {}
            orders = [dict(row) for row in exposure.get("orders") or [] if isinstance(row, Mapping)]
            positions = [dict(row) for row in exposure.get("positions") or [] if isinstance(row, Mapping)]
            proposal = self.legacy_cutover.create_proposal(
                update_id=int(update_id) if update_id not in (None, "") else None,
                source_text_digest=text_digest,
                expected_order_count=expected_count,
                orders=orders,
                positions=positions,
                reconciliation=dict(facts.get("reconciliation") or {}),
            )
            if candidate.get("explicit_confirmation") is True:
                decision = self.legacy_cutover.confirm(
                    proposal,
                    update_id=int(update_id) if update_id not in (None, "") else None,
                    mode="bounded_explicit_clean_slate_phrase",
                )
                ids = ", ".join(str(row.get("order_id") or "") for row in proposal.get("orders") or [])
                self.telegram.queue_outbound(
                    idempotency_key=f"park-legacy-cutover-confirmed:{proposal['proposal_id']}",
                    message_type="legacy_cutover_confirmed",
                    text=(
                        f"已收到 clean slate 指令：将只撤销 {expected_count} 个旧挂单（0 个持仓），不平仓、不反向。\n"
                        f"订单集合：{ids}\n"
                        f"cutover_digest={proposal['proposal_digest']}\n"
                        "下一步会再次核对同一订单集合；若有漂移会自动阻塞。"
                    ),
                    binding=None,
                )
                return {"status": "legacy_cutover_confirmed", "proposal": proposal, "decision": decision}
            self.telegram.queue_outbound(
                idempotency_key=f"park-legacy-cutover-proposal:{proposal['proposal_id']}",
                message_type="legacy_cutover_proposal",
                text=(
                    f"已生成旧挂单精确清理清单：{expected_count} 个挂单、0 个持仓。\n"
                    f"cutover_digest={proposal['proposal_digest']}\n"
                    f"请回复：确认清理旧挂单 {proposal['proposal_digest']}"
                ),
                binding=None,
            )
            return {"status": "legacy_cutover_proposal_created", "proposal": proposal}
        except (ParkLegacyCutoverError, ParkTelegramRuntimeError, ValueError, TypeError) as exc:
            return self._block(
                code=getattr(exc, "code", "legacy_cutover_blocked"),
                message=f"Park Paper clean slate 未接受：{str(exc)}",
                binding=None,
                idempotency_key=f"park-legacy-cutover-rejected:{update_id}",
            )

    def _handle_legacy_cutover_confirmation(self, digest: str, *, update_id: Any) -> dict[str, Any]:
        proposal = self.legacy_cutover.proposal_for_digest(digest)
        if proposal is None:
            return self._block(
                code="legacy_cutover_digest_unknown",
                message="这个 clean-slate digest 已不存在或不是当前待处理清单；系统不会猜测旧订单。",
                binding=None,
                idempotency_key=f"park-legacy-cutover-digest:{update_id}",
            )
        try:
            decision = self.legacy_cutover.confirm(
                proposal,
                update_id=int(update_id) if update_id not in (None, "") else None,
                mode="explicit_cutover_digest",
            )
            self.telegram.queue_outbound(
                idempotency_key=f"park-legacy-cutover-confirmed:{proposal['proposal_id']}",
                message_type="legacy_cutover_confirmed",
                text=(
                    f"已确认精确清理清单 {proposal['proposal_digest']}；下一次 Paper tick 会重新核对后只撤销这些旧挂单。"
                ),
                binding=None,
            )
            return {"status": "legacy_cutover_confirmed", "proposal": proposal, "decision": decision}
        except ParkLegacyCutoverError as exc:
            return self._block(
                code=exc.code,
                message=f"Park Paper clean slate 未确认：{str(exc)}",
                binding=None,
                idempotency_key=f"park-legacy-cutover-confirmation:{update_id}",
            )

    def _previous_result(self, update_id: Any) -> dict[str, Any] | None:
        if update_id in (None, ""):
            return None
        try:
            normalized_id = int(update_id)
        except (TypeError, ValueError):
            return None
        for row in reversed(_read_jsonl(self.result_path)):
            if row.get("update_id") == normalized_id:
                return {
                    "result": dict(row.get("result") or {}),
                    "_update_digest": str(row.get("update_digest") or ""),
                }
        return None

    def _remember_result(
        self,
        update_id: Any,
        result: Mapping[str, Any],
        *,
        update_digest: str,
    ) -> dict[str, Any]:
        try:
            normalized_id = int(update_id) if update_id not in (None, "") else None
        except (TypeError, ValueError):
            normalized_id = None
        if normalized_id is not None:
            _append_jsonl(
                self.result_path,
                {
                    "schema_version": PARK_TELEGRAM_RUNTIME_SCHEMA,
                    "update_id": normalized_id,
                    "update_digest": update_digest,
                    "result": dict(result),
                },
            )
        return dict(result)

    @staticmethod
    def _looks_like_continuation(text: str) -> bool:
        """Recognize field-completion language without inferring direction."""

        lowered = str(text or "").lower()
        return bool(
            re.search(
                r"止损|止盈|止损位|止盈位|stop(?:_price)?|take\s*profit|take_profit|\btp\b|杠杆|leverage|最大可接受亏损|最大亏损|max(?:imum)?\s*loss",
                lowered,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _looks_like_read_query(text: str) -> bool:
        lowered = str(text or "").lower()
        topic = bool(
            re.search(
                r"价格|当前价|行情|策略|有策略|持仓|挂单|订单|盈亏|美联储|联储|fomc|fed|央行|利率|cpi|ppi|非农|就业|数据|会议|pnl|price|position|order|running strategy|status",
                lowered,
                re.IGNORECASE,
            )
        )
        question = bool(
            re.search(
                r"\?|？|多少|吗|么|有没有|是否|是什么|什么|哪种|现在.*(?:跑|运行|状态)|当前.*(?:状态)|what|how many|is there",
                lowered,
                re.IGNORECASE,
            )
        )
        return topic and question

    def _conversation_context(
        self,
        active: Mapping[str, Any] | None,
        *,
        text: str | None = None,
    ) -> dict[str, Any]:
        try:
            market = dict(self._read_market(text))
        except Exception as exc:  # noqa: BLE001 - read-only context may be unavailable.
            market = {"status": "unavailable", "error_type": type(exc).__name__}
            if self._is_testnet_text(text):
                market.update(
                    {
                        "environment": "testnet",
                        "provider": "hyperliquid",
                        "source": "hyperliquid.external_testnet",
                        "instrument_id": "BTC-USD-PERP",
                    }
                )
        try:
            account = dict(
                self._read_account(
                    self.cycle_id_provider(self.now()),
                    market=market,
                )
            )
        except Exception as exc:  # noqa: BLE001 - provider must say facts are unavailable.
            account = {"status": "unavailable", "error_type": type(exc).__name__}
        snapshot = account.get("snapshot") if isinstance(account.get("snapshot"), Mapping) else {}
        strategy: dict[str, Any] = dict(active or {})
        if active is None:
            strategy = {"status": "none"}
        else:
            pending_confirmation = bool(self.confirmations.pending_proposals(active))
            strategy["status"] = "pending_confirmation" if pending_confirmation else "active"
            strategy["pending_confirmation"] = pending_confirmation
            digest = str(active.get("plan_digest") or "")
            if digest:
                plans = _read_jsonl(self.output_root / "park_strategy" / "plans.jsonl")
                plan = next(
                    (
                        row
                        for row in reversed(plans)
                        if str(row.get("plan_digest") or "") == digest
                    ),
                    None,
                )
                if plan is not None:
                    strategy["plan"] = plan
        return {
            "market": market,
            "strategy": strategy,
            "execution": {"paper_only": True, "control_plane": "telegram"},
            "account": account,
            "positions": list(account.get("positions") or snapshot.get("positions") or [])
            if isinstance(snapshot, Mapping)
            else list(account.get("positions") or []),
            "orders": list(account.get("orders") or snapshot.get("orders") or [])
            if isinstance(snapshot, Mapping)
            else list(account.get("orders") or []),
            "testnet_readiness": TestnetSoakReadiness(self.output_root).public_status(now=self.now()),
        }

    def _deterministic_conversation_fallback(
        self,
        text: str,
        *,
        context: Mapping[str, Any],
        active: Mapping[str, Any] | None,
        update_id: Any,
    ) -> dict[str, Any]:
        if self._looks_like_read_query(text):
            market = context.get("market") if isinstance(context.get("market"), Mapping) else {}
            strategy = context.get("strategy") if isinstance(context.get("strategy"), Mapping) else {}
            account = context.get("account") if isinstance(context.get("account"), Mapping) else {}
            readiness = context.get("testnet_readiness") if isinstance(context.get("testnet_readiness"), Mapping) else {}
            price = market.get("price")
            if strategy.get("status") == "pending_confirmation":
                strategy_status = "有一张策略提案正在等待你的确认，但还没有执行"
            elif strategy.get("status") == "active":
                strategy_status = "有一张已确认策略处于运行状态"
            else:
                strategy_status = "当前没有已确认运行策略"
            market_authoritative = market.get("trusted") is True and market.get("fresh") is True
            price_text = str(price) if market_authoritative and price not in (None, "") else "暂时无法读取（行情信任或新鲜度闸未通过）"
            if account.get("status") == "unavailable":
                exposure_text = "挂单和持仓事实暂时无法读取"
            else:
                exposure_text = f"挂单 {account.get('open_or_accepted_orders', 0)}，持仓 {account.get('open_positions', 0)}"
            if account.get("nav_is_current") is True and account.get("equity") not in (None, ""):
                nav_text = f"当前 Paper NAV：{float(account['equity']):.2f} USDT（按可信行情标记）"
            else:
                nav_text = "当前 Paper NAV：暂不可计算（当前行情信任/新鲜度闸未通过）"
            reply = (
                f"{strategy_status}。当前 Paper 价格：{price_text}。\n"
                f"{exposure_text}。\n{nav_text}。\n"
                f"Testnet soak readiness：{readiness.get('status', 'missing')}，窗口 {readiness.get('window_count', 0)}/{readiness.get('required_window_count', 14)}；Live 仍关闭。"
            )
            return {
                "status": "conversation_replied",
                "mode": "query",
                "message": reply,
                "provider": {"provider": "deterministic_read_only", "status": "fallback"},
                "execution_authorized": False,
            }
        if not has_explicit_execution_intent(text):
            patch = extract_explicit_strategy_patch(text)
            prior_patch = self.conversation_ledger.latest_strategy_patch()
            research_like = bool(
                re.search(
                    r"研究|调研|比较|对比|优缺点|假设|证据|反例|research|compare|pros|cons|hypothesis|evidence",
                    str(text or ""),
                    re.IGNORECASE,
                )
            )
            formation_like = bool(
                re.search(
                    r"我\s*(?:想|要|决定|计划)|我要|我决定|设置|启动|做多|做空|中性网格|open|start",
                    str(text or ""),
                    re.IGNORECASE,
                )
            )
            strategy_like = bool(patch) or bool(
                prior_patch
                and re.search(
                    r"策略|参数|理解|确认|DCA|Grid|dca|grid|strategy|trade|交易",
                    str(text or ""),
                    re.IGNORECASE,
                )
            )
            mode = "research" if research_like and not formation_like else "strategy_forming" if strategy_like else "discuss"
            candidate = {**prior_patch, **patch} if strategy_like else patch
            market = context.get("market") if isinstance(context, Mapping) else None
            preview = build_deterministic_risk_preview(candidate, market=market) if mode == "strategy_forming" else None
            if mode == "strategy_forming":
                message = (
                    preview["assistant_reply"]
                    if preview is not None
                    else "我先把这条保留为可修改的策略草稿；你还可以继续补充或讨论，未收到明确 finalize/执行指令前不会创建 Paper 计划。"
                )
            elif mode == "research":
                message = "我先按研究/讨论处理，不把这个策略想法收敛成执行计划；可以继续比较证据、假设和风险。"
            else:
                message = "我先和你讨论这个交易想法，不会因为提到参数就自动形成或执行 Paper 策略。"
            conversation = {
                "mode": mode,
                "assistant_reply": message,
                "strategy_patch": candidate,
                "missing_fields": [],
                "evidence_used": [],
                "assumptions": ["provider unavailable; deterministic conversation fallback"],
                "conflicts": [],
                "needs_confirmation": False,
                "explicit_execution_intent": False,
                "confidence": "low",
            }
            if preview is not None:
                conversation.update(preview)
            return {
                "status": "conversation_replied",
                "mode": mode,
                "message": message,
                "conversation": conversation,
                "provider": {"provider": "deterministic_conversation_fallback", "status": "provider_unavailable"},
                "execution_authorized": False,
            }
        if not re.search(r"交易|行情|市场|策略|价格|持仓|挂单|订单|中性|网格|杠杆|止损|止盈|做多|做空|趋势|震荡|美联储|联储|fomc|fed|央行|利率|cpi|ppi|非农|就业|数据|会议|dca|grid|trade|market|strategy|position|order|leverage|stop|take profit", str(text or ""), re.IGNORECASE):
            return {
                "status": "conversation_replied",
                "mode": "off_topic",
                "message": "我主要和你讨论交易、市场和 Paper 策略。我们回到交易上吧。",
                "provider": {"provider": "deterministic_scope_guard", "status": "fallback"},
                "execution_authorized": False,
            }
        prior_patch = self.conversation_ledger.latest_strategy_patch()
        explicit_patch = extract_explicit_strategy_patch(text)
        candidate = {**prior_patch, **explicit_patch}
        return self._handle_strategy(
            text,
            active=active,
            update_id=update_id,
            candidate=candidate or None,
            skip_provider=True,
            provider={"provider": "deterministic_strategy_fallback", "status": "provider_unavailable"},
        )

    def _handle_conversation_or_strategy(
        self,
        text: str,
        *,
        active: Mapping[str, Any] | None,
        update_id: Any,
    ) -> dict[str, Any]:
        if str(text or "").strip().lower() in {"/start", "start", "/help", "help"}:
            return self._handle_strategy(text, active=active, update_id=update_id)
        if self.conversation_agent is None:
            return self._handle_strategy(text, active=active, update_id=update_id)
        context = self._conversation_context(active, text=text)
        quick_candidate = {
            **self.conversation_ledger.latest_strategy_patch(),
            **extract_explicit_strategy_patch(text),
        }
        quick_market = context.get("market") if isinstance(context, Mapping) else None
        if build_deterministic_risk_preview(quick_candidate, market=quick_market) is not None:
            # A complete, calculable candidate does not need a 30-second
            # provider round-trip. Record the user turn once, then use the
            # same deterministic conversation seam as provider fallback.
            self.conversation_ledger.record_user(update_id=update_id, text=text)
            fallback = self._deterministic_conversation_fallback(
                text,
                context=context,
                active=active,
                update_id=update_id,
            )
            if fallback.get("status") == "conversation_replied":
                fallback_conversation = fallback.get("conversation")
                if isinstance(fallback_conversation, Mapping):
                    self.conversation_ledger.record_assistant(
                        update_id=update_id,
                        conversation=fallback_conversation,
                        provider=fallback.get("provider"),
                    )
                outbound = self.telegram.queue_outbound(
                    idempotency_key=f"park-conversation:{update_id}",
                    message_type="conversation_reply",
                    text=str(fallback.get("message") or ""),
                    binding=active,
                )
                return {**fallback, "outbound": outbound}
            return fallback
        conversation_result = self.conversation_agent.evaluate(
            text,
            update_id=update_id,
            context=context,
        )
        metadata = dict(conversation_result.get("metadata") or {})
        self._record_provider(update_id=update_id, metadata=metadata)
        if conversation_result.get("status") != "ok":
            fallback = self._deterministic_conversation_fallback(
                text,
                context=context,
                active=active,
                update_id=update_id,
            )
            if fallback.get("status") == "conversation_replied":
                fallback_conversation = fallback.get("conversation")
                if isinstance(fallback_conversation, Mapping):
                    self.conversation_ledger.record_assistant(
                        update_id=update_id,
                        conversation=fallback_conversation,
                        provider=fallback.get("provider"),
                    )
                outbound = self.telegram.queue_outbound(
                    idempotency_key=f"park-conversation:{update_id}",
                    message_type="conversation_reply",
                    text=str(fallback.get("message") or ""),
                    binding=active,
                )
                return {**fallback, "outbound": outbound}
            return fallback
        conversation = dict(conversation_result.get("conversation") or {})
        mode = str(conversation.get("mode") or "discuss")
        candidate = dict(conversation.get("strategy_patch") or {})
        deterministic_finalize = False
        if mode != "ready_for_confirmation" and has_explicit_execution_intent(text) and candidate:
            try:
                normalized_candidate = normalize_park_input(candidate)
            except ParkStrategyPlanError:
                normalized_candidate = None
            deterministic_finalize = normalized_candidate is not None and not (
                normalized_candidate.get("strategy_type") == "dca"
                and (
                    normalized_candidate.get("stop_price") in (None, "")
                    or normalized_candidate.get("take_profit_price") in (None, "")
                )
            )
        if mode != "ready_for_confirmation" and not deterministic_finalize:
            outbound = self.telegram.queue_outbound(
                idempotency_key=f"park-conversation:{update_id}",
                message_type="conversation_reply",
                text=str(conversation.get("assistant_reply") or ""),
                binding=active,
            )
            return {
                "status": "conversation_replied",
                "mode": mode,
                "conversation": conversation,
                "provider": metadata,
                "outbound": outbound,
                "execution_authorized": False,
            }
        result = self._handle_strategy(
            text,
            active=active,
            update_id=update_id,
            candidate=candidate,
            provider=metadata,
        )
        if result.get("status") == "proposal_created":
            result["conversation"] = conversation
            if mode == "ready_for_confirmation":
                outbound = self.telegram.queue_outbound(
                    idempotency_key=f"park-conversation:{update_id}:summary",
                    message_type="conversation_summary",
                    text=str(conversation.get("assistant_reply") or ""),
                    binding=result.get("session"),
                )
                result["conversation_outbound"] = outbound
            else:
                result["provider_mode_overridden"] = mode
        return result

    def _handle_strategy(
        self,
        text: str,
        *,
        active: Mapping[str, Any] | None,
        update_id: Any,
        candidate: Mapping[str, Any] | None = None,
        provider: Mapping[str, Any] | None = None,
        skip_provider: bool = False,
    ) -> dict[str, Any]:
        if text.lower() in {"/start", "start", "/help", "help"}:
            return self._block(
                code="strategy_input_help",
                message=self._help_message(),
                binding=None,
                idempotency_key=f"park-strategy-help:{update_id}",
            )
        if active:
            try:
                active = self._release_expired_unconfirmed_session(active)
            except ParkTelegramRuntimeError as exc:
                return self._block(
                    code=exc.code,
                    message=f"Park strategy is blocked: {str(exc)}",
                    binding=active,
                    idempotency_key=f"park-runtime-blocked:{update_id}:{exc.code}",
                )
        if active:
            return self._block(
                code="strategy_locked",
                message="Park strategy is already active and immutable; wait for terminal closure before a new clean-slate strategy.",
                binding=active,
                idempotency_key=f"park-strategy-locked:{update_id}",
            )
        continuation: Mapping[str, Any] | None = None
        normalized: dict[str, Any] | None = None
        try:
            if candidate is not None:
                normalized = normalize_park_input({**dict(candidate), "source_text": text})
            elif self._looks_like_continuation(text):
                continuation = self.continuations.latest_pending()
                if continuation is not None:
                    merged = dict(continuation.get("normalized_input") or {})
                    merged["source_text"] = text
                    normalized = normalize_park_input(merged)
                    provider = {
                        "provider": "deterministic_continuation",
                        "status": "merged",
                        "draft_id": str(continuation.get("draft_id") or ""),
                    }
                elif skip_provider:
                    normalized = normalize_park_input(text)
                else:
                    parsed = self._parse_intent(text, update_id=update_id)
                    provider = parsed.get("metadata") if isinstance(parsed, Mapping) else None
                    candidate = parsed.get("candidate") if isinstance(parsed, Mapping) else None
                    normalized = normalize_park_input(candidate if isinstance(candidate, Mapping) else text)
            elif skip_provider:
                normalized = normalize_park_input(text)
            else:
                parsed = self._parse_intent(text, update_id=update_id)
                provider = parsed.get("metadata") if isinstance(parsed, Mapping) else None
                candidate = parsed.get("candidate") if isinstance(parsed, Mapping) else None
                if isinstance(candidate, Mapping):
                    direction = str(candidate.get("direction") or "")
                    strategy_type = str(candidate.get("strategy_type") or "")
                    if direction == "neutral" and strategy_type not in {"", "grid"}:
                        return self._block(
                            code="neutral_direction_requires_grid",
                            message="我理解到的是‘中性’，但中性只适用于 Grid；请把策略类型说成 Grid。\n\n例如：中性网格，区间 4450~4100，最大20倍杠杆",
                            binding=None,
                            idempotency_key=f"park-neutral-direction:{update_id}",
                            provider=provider,
                        )
                    normalized = normalize_park_input(candidate)
                else:
                    normalized = normalize_park_input(text)
            if normalized.get("strategy_type") == "dca" and (
                normalized.get("stop_price") in (None, "")
                or normalized.get("take_profit_price") in (None, "")
            ):
                self.continuations.record_pending(
                    update_id=update_id,
                    text_digest=_digest(text),
                    normalized_input={
                        key: normalized.get(key)
                        for key in (
                            "direction",
                            "strategy_type",
                            "upper_price_boundary",
                            "lower_price_boundary",
                            "maximum_leverage",
                            "maximum_acceptable_loss",
                            "order_count",
                        )
                    },
                    missing_fields=[
                        key
                        for key in ("stop_price", "take_profit_price")
                        if normalized.get(key) in (None, "")
                    ],
                    draft_id=str(continuation.get("draft_id") or "") if continuation is not None else None,
                )
                return self._block(
                    code="dca_exit_levels_missing",
                    message=(
                        "DCA 必须明确提供一个策略级止损和一个策略级止盈；系统不会用区间边界猜测。\n\n"
                        "例如：做空 DCA，区间 4444~4200，最大10倍杠杆，止损4450，止盈4210"
                    ),
                    binding=None,
                    idempotency_key=f"park-dca-exits:{update_id}",
                    provider=provider,
                )
            observed_at = self.now()
            cycle_id = self.cycle_id_provider(observed_at)
            market = dict(self._read_market(text))
            facts = dict(self._read_account(cycle_id, market=market))
            admission = admit_clean_slate(facts)
            if not admission.get("admitted"):
                return self._block(
                    code="clean_slate_blocked",
                    message=f"Park strategy not accepted: clean slate blocked by {','.join(admission.get('blockers') or [])}.",
                    binding=None,
                    idempotency_key=f"park-clean-slate:{update_id}",
                )
            session_id = f"session-{uuid.uuid4().hex}"
            revision_id = f"revision-{uuid.uuid4().hex}"
            normalized.update(
                {
                    "strategy_session_id": session_id,
                    "strategy_revision_id": revision_id,
                    # Carry the source-bound instrument through the Park
                    # plan.  The Testnet activation seam must never infer an
                    # asset from a provider default after confirmation.
                    "instrument_id": str(
                        normalized.get("instrument_id")
                        or market.get("instrument_id")
                        or ""
                    ).strip(),
                }
            )
            plan = build_deterministic_risk_plan(
                normalized,
                market=market,
                account_equity=facts.get("equity"),
            )
            plan["strategy_session_id"] = session_id
            plan["strategy_revision_id"] = revision_id
            started = self.identity.start_clean_session(
                observed_at=observed_at,
                plan_digest=str(plan["plan_digest"]),
                reconciliation_healthy=bool(facts.get("reconciliation_healthy")),
                open_positions=int(facts.get("open_positions") or 0),
                open_or_accepted_orders=int(facts.get("open_or_accepted_orders") or 0),
                unresolved_runtime=bool(facts.get("unresolved_runtime")),
                pending_terminal_actions=bool(facts.get("pending_terminal_actions")),
                strategy_session_id=session_id,
                strategy_revision_id=revision_id,
            )
            plan_path = self.output_root / "park_strategy" / "plans.jsonl"
            _append_jsonl(plan_path, {"event": "plan_proposed", **plan, "created_at": observed_at})
            proposal_id = f"park-proposal-{str(plan['plan_digest']).removeprefix('sha256:')[:24]}"
            risk_digest = _digest(plan.get("risk") or {})
            execution_environment = "testnet" if self._is_testnet_text(text) else "paper"
            proposal = self.confirmations.create_proposal(
                proposal_id=proposal_id,
                strategy_session_id=session_id,
                strategy_revision_id=revision_id,
                plan_digest=str(plan["plan_digest"]),
                risk_digest=risk_digest,
                expires_at=time.time() + self.confirmation_ttl_seconds,
                execution_environment=execution_environment,
            )
            self.telegram.queue_outbound(
                idempotency_key=f"park-proposal:{proposal_id}",
                message_type="strategy_proposal",
                text=self._format_plan(plan, proposal),
                binding={"strategy_session_id": session_id, "strategy_revision_id": revision_id},
            )
            if continuation is not None:
                self.continuations.resolve(
                    continuation,
                    update_id=update_id,
                    plan_digest=str(plan["plan_digest"]),
                )
            result: dict[str, Any] = {"status": "proposal_created", "plan": plan, "proposal": proposal, "session": started}
            if provider:
                result["provider"] = dict(provider)
            return result
        except (ParkStrategyPlanError, ParkStrategyIdentityError, ParkTelegramControlError, ParkContinuationError) as exc:
            return self._block(
                code=getattr(exc, "code", "park_strategy_rejected"),
                message=f"Park strategy not accepted: {str(exc)}",
                binding=None,
                idempotency_key=f"park-strategy-rejected:{update_id}",
                provider=provider,
            )
        except ParkTelegramRuntimeError as exc:
            return self._block(
                code=exc.code,
                message=f"Park strategy is blocked: {str(exc)}",
                binding=None,
                idempotency_key=f"park-runtime-blocked:{update_id}:{exc.code}",
                provider=provider,
            )

    def _release_expired_unconfirmed_session(self, active: Mapping[str, Any]) -> Mapping[str, Any] | None:
        pending = self.confirmations.pending_proposals(active)
        if not pending:
            return active
        if len(pending) > 1:
            raise ParkTelegramRuntimeError(
                "multiple_pending_proposals",
                "multiple Park proposals are bound to the active revision",
            )
        proposal = pending[0]
        try:
            expired = float(proposal.get("expires_at") or 0) <= time.time()
        except (TypeError, ValueError):
            raise ParkTelegramRuntimeError(
                "confirmation_expiry_invalid",
                "Park proposal expiry is invalid",
            ) from None
        if not expired:
            return active
        cycle_id = self.cycle_id_provider(self.now())
        try:
            facts = dict(self._read_account(cycle_id))
        except ParkTelegramRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - expiry release is fail closed.
            raise ParkTelegramRuntimeError("paper_account_unavailable", type(exc).__name__) from exc
        blockers: list[str] = []
        if facts.get("reconciliation_healthy") is not True:
            blockers.append("reconciliation_unhealthy")
        if int(facts.get("open_positions") or 0) != 0:
            blockers.append("open_positions")
        if int(facts.get("open_or_accepted_orders") or 0) != 0:
            blockers.append("open_or_accepted_orders")
        if facts.get("unresolved_runtime"):
            blockers.append("unresolved_runtime")
        if facts.get("pending_terminal_actions"):
            blockers.append("pending_terminal_actions")
        if blockers:
            raise ParkTelegramRuntimeError(
                "confirmation_expired_requires_clean_slate",
                f"expired proposal cannot be released: {','.join(blockers)}",
            )
        session = str(active.get("strategy_session_id") or "")
        revision = str(active.get("strategy_revision_id") or "")
        self.identity.close_session(
            strategy_session_id=session,
            strategy_revision_id=revision,
            observed_at=self.now(),
            reason="confirmation_expired",
        )
        self.telegram.queue_outbound(
            idempotency_key=f"park-confirmation-expired:{proposal.get('proposal_id')}",
            message_type="confirmation_expired",
            text="上一个 Paper 计划未确认且已过期；当前仍是 clean slate，系统已释放它。请重新描述策略。",
            binding=None,
        )
        return None

    def _parse_intent(self, text: str, *, update_id: Any) -> dict[str, Any]:
        if self.intent_parser is None:
            return {
                "status": "deterministic_fallback",
                "metadata": {"provider": "deterministic", "status": "not_configured"},
            }
        try:
            parsed = dict(self.intent_parser.parse(text) or {})
        except Exception as exc:  # noqa: BLE001 - NLU failure is a safe fallback.
            parsed = {
                "status": "unavailable",
                "metadata": {
                    "provider": "codex_cli",
                    "status": "adapter_error",
                    "error_type": type(exc).__name__,
                },
            }
        metadata = dict(parsed.get("metadata") or {})
        metadata.setdefault("provider", "codex_cli")
        if parsed.get("status") == "ok" and isinstance(parsed.get("candidate"), Mapping):
            self._record_provider(update_id=update_id, metadata=metadata)
            return {"status": "ok", "candidate": dict(parsed["candidate"]), "metadata": metadata}
        fallback_candidate = deterministic_neutral_grid_candidate(text)
        if fallback_candidate is not None:
            metadata = {**metadata, "fallback": "deterministic_neutral_grid"}
            self._record_provider(update_id=update_id, metadata=metadata)
            return {"status": "deterministic_fallback", "candidate": fallback_candidate, "metadata": metadata}
        self._record_provider(update_id=update_id, metadata=metadata)
        return {"status": "deterministic_fallback", "metadata": metadata}

    def _record_provider(self, *, update_id: Any, metadata: Mapping[str, Any]) -> None:
        safe = {
            "schema_version": PARK_TELEGRAM_RUNTIME_SCHEMA,
            "event": "provider_call",
            "provider": str(metadata.get("provider") or ""),
            "status": str(metadata.get("status") or ""),
            "elapsed_ms": metadata.get("elapsed_ms"),
            "exit_code": metadata.get("exit_code"),
            "timed_out": bool(metadata.get("timed_out")),
            "stderr_digest": metadata.get("stderr_digest"),
            "error_type": metadata.get("error_type"),
            "update_id": int(update_id) if str(update_id or "").isdigit() else None,
            "recorded_at": _utc_now(),
        }
        _append_jsonl(self.provider_path, safe)

    def _handle_confirmation(
        self,
        text: str,
        *,
        active: Mapping[str, Any] | None,
        update_id: Any,
    ) -> dict[str, Any]:
        if not active:
            return self._block(
                code="confirmation_without_active_strategy",
                message="No active Park proposal is waiting for confirmation.",
                binding=None,
                idempotency_key=f"park-confirmation-no-active:{update_id}",
            )
        try:
            mode = "exact_digest"
            try:
                verb, digest = parse_confirmation_command(text)
            except ParkConfirmationError as exact_error:
                try:
                    verb = parse_confirmation_shortcut(text)
                except ParkConfirmationError:
                    raise exact_error
                digest = ""
                mode = "pending_proposal_shortcut"
            if digest:
                proposal = next(
                    (
                        row
                        for row in reversed(self.confirmations.rows())
                        if row.get("event") == "proposal" and str(row.get("plan_digest") or "").lower() == digest
                    ),
                    None,
                )
            else:
                pending = self.confirmations.pending_proposals(active)
                if len(pending) != 1:
                    if pending and all(float(row.get("expires_at") or 0) <= time.time() for row in pending):
                        raise ParkConfirmationError("confirmation_expired", "the current Park proposal has expired; resend the strategy")
                    raise ParkConfirmationError("ambiguous_pending_proposals", "there is not exactly one pending Park proposal")
                proposal = pending[0]
                digest = str(proposal.get("plan_digest") or "").lower()
            if not proposal:
                raise ParkConfirmationError("proposal_missing", "proposal digest is unknown")
            decision = self.confirmations.decide(
                proposal_id=str(proposal["proposal_id"]),
                park_user_id=self.telegram.park_user_id,
                command_text=f"{verb} {digest}",
                current_binding=active,
                now=time.time(),
            )
            event = str(decision.get("event") or "decision")
            confirmation_environment = str(decision.get("execution_environment") or "paper").capitalize()
            self.telegram.queue_outbound(
                idempotency_key=f"park-confirmation:{proposal['proposal_id']}:{event}",
                message_type="confirmation_receipt",
                text=(
                    f"Park {event}: {proposal['plan_digest']}. "
                    + (f"{confirmation_environment} execution is authorized for the next trusted fresh tick; no other environment will be touched." if event == "confirmed" else "No execution will be attempted; send a new clean-slate strategy after closure.")
                ),
                binding=active,
            )
            if event == "confirmed" and str(decision.get("execution_environment") or "paper") == "testnet":
                if self.testnet_start_handler is None:
                    return {
                        "status": "confirmed_pending_testnet_start",
                        "decision": decision,
                        "confirmation_mode": mode,
                        "next_action": "invoke_attended_testnet_start",
                    }
                started = self.testnet_start_handler(
                    {
                        "proposal": proposal,
                        "decision": decision,
                        "plan_digest": digest,
                        "environment": "testnet",
                    }
                )
                return {
                    "status": "testnet_started",
                    "decision": decision,
                    "confirmation_mode": mode,
                    "start": dict(started),
                }
            return {"status": event, "decision": decision, "confirmation_mode": mode}
        except ParkConfirmationError as exc:
            return self._block(
                code=exc.code,
                message=f"Park confirmation rejected: {str(exc)}",
                binding=active,
                idempotency_key=f"park-confirmation-rejected:{update_id}",
            )

    def _block(
        self,
        *,
        code: str,
        message: str,
        binding: Mapping[str, Any] | None,
        idempotency_key: str,
        provider: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_message = self._friendly_block_message(code, message)
        outbound = self.telegram.queue_outbound(
            idempotency_key=idempotency_key,
            message_type="park_blocker",
            text=user_message,
            binding=binding,
        )
        result: dict[str, Any] = {
            "status": "blocked",
            "code": code,
            "next_action": "notify_park_and_wait",
            "outbound": outbound,
        }
        if provider:
            result["provider"] = dict(provider)
        return result

    @staticmethod
    def _help_message() -> str:
        return (
            "直接用自然语言描述策略即可，我会先复述理解并计算 Paper 风险，不会直接下单。\n\n"
            "例如：\n"
            "1) 做空 DCA，价格区间 4444~4200，最大10倍杠杆，止损=……，止盈=……\n"
            "2) 中性网格，区间 4450~4100，最大20倍杠杆\n\n"
            "当前价格由系统读取；只有你确认精确计划后才会执行。"
        )

    @staticmethod
    def _neutral_grid_message(candidate: Mapping[str, Any]) -> str:
        upper = candidate.get("upper_price_boundary")
        lower = candidate.get("lower_price_boundary")
        leverage = candidate.get("maximum_leverage")
        maximum_loss = candidate.get("maximum_acceptable_loss")
        risk_text = (
            f"最大杠杆 {leverage}x"
            if leverage is not None
            else f"最大可接受亏损 {maximum_loss}"
        )
        return (
            "我理解你的意思是：中性网格"
            f"，区间 {lower}~{upper}，{risk_text}。\n"
            "中性 Grid 会拆成明确的买入腿和卖出腿；系统会先读取可信当前价，"
            "计算双边风险并发回规范化计划。确认前不会下单，也不会改动持仓。\n\n"
            "确认格式示例：confirm <plan_digest>；拒绝格式示例：reject <plan_digest>。"
        )

    @staticmethod
    def _friendly_block_message(code: str, message: str) -> str:
        if code == "strategy_input_help":
            return message
        if code == "missing_direction":
            return (
                "我还没读清楚你的方向。你可以直接说：做多、做空，或‘中性网格’。\n\n"
                "例如：\n"
                "1) 做空 DCA，区间 4444~4200，最大10倍杠杆\n"
                "2) 做多 Grid，区间 4450~4100，最大20倍杠杆\n\n"
                "当前价格我会自动读取，确认前不会下单。"
            )
        if code == "missing_strategy_type":
            return "我还没读清楚你要 DCA 还是 Grid。比如：做空 DCA，区间 4444~4200，最大10倍杠杆。"
        if code == "missing_price_boundary":
            return "我还缺价格区间。请像这样说：做空 DCA，区间 4444~4200，最大10倍杠杆。"
        if code == "missing_risk_authority":
            return "我还缺风险上限。请补充最大杠杆或最大可接受亏损，例如：最大10倍杠杆。"
        if code == "market_unavailable":
            return "当前可信行情暂时不可用；策略草稿已经保留，系统不会猜价或下单。行情恢复后再次发送 finalize/执行即可。"
        if code == "testnet_market_unavailable":
            return "Hyperliquid Testnet BTC 行情暂时不可用；策略草稿已经保留，系统不会猜价或切回 Paper 行情。行情恢复后再次发送 finalize/执行即可。"
        if code == "testnet_market_identity_mismatch":
            return "Hyperliquid Testnet 行情与 BTC-USD-PERP 身份不匹配；策略草稿已保留，系统不会混用其他市场。"
        if code == "testnet_market_not_authoritative":
            return "Hyperliquid Testnet 行情目前不满足可信/新鲜条件；策略草稿已保留，系统不会猜价或下单。"
        if code == "testnet_account_unavailable":
            return "已读取 Hyperliquid Testnet 行情，但 Testnet 账户快照尚未接入；系统不会拿 Paper 账户代替，也不会下单。"
        if code == "confirmation_incomplete":
            return "可以直接回复‘确认当前计划’或‘拒绝当前计划’；也可以回复 confirm <plan_digest>。"
        if code == "confirmation_expired":
            return "这个 Paper 计划已经过期，请重新发送策略；过期计划不会执行。"
        if code == "ambiguous_pending_proposals":
            return "当前有多个待确认计划，不能猜测你要确认哪一个；请使用计划摘要确认。"
        if code == "confirmation_expired_requires_clean_slate":
            return "上一个计划已过期，但账户状态不是 clean slate；系统不会自动释放它，请先处理阻塞状态。"
        return message

    @staticmethod
    def _format_plan(plan: Mapping[str, Any], proposal: Mapping[str, Any]) -> str:
        risk = dict(plan.get("risk") or {})
        normalized = dict(plan.get("normalized_input") or {})
        neutral_detail = ""
        if normalized.get("direction") == "neutral":
            legs = dict(risk.get("legs") or {})
            neutral_detail = (
                f"neutral_legs=buy→{legs.get('long', {}).get('boundary')} / "
                f"sell→{legs.get('short', {}).get('boundary')}\n"
            )
        grid_detail = ""
        if normalized.get("strategy_type") == "grid":
            geometry = dict(risk.get("grid_entry_range") or {})
            hard_stop = risk.get("hard_stop")
            grid_detail = (
                f"entry_range={geometry.get('lower')}~{geometry.get('upper')} "
                f"spacing={risk.get('grid_spacing')} rungs={risk.get('order_count')}\n"
                f"grid_hard_stop={hard_stop} tp_geometry=next_rung_then_boundary "
                f"rung_prices={risk.get('grid_rung_prices')}\n"
            )
        return "".join(
            (
                "Park proposal (Paper-only; confirm this plan in plain language or with its digest)\n",
                f"direction={normalized.get('direction')} type={normalized.get('strategy_type')}\n",
                f"range={normalized.get('upper_price_boundary')}~{normalized.get('lower_price_boundary')} current={dict(plan.get('market') or {}).get('price')}\n",
                neutral_detail,
                grid_detail,
                f"max_notional={risk.get('maximum_notional')} effective_leverage={risk.get('effective_leverage')}x\n",
                f"theoretical_max_loss={risk.get('theoretical_max_loss')} order_count={risk.get('order_count')} quantity_each={risk.get('per_order_quantity')}\n",
                f"risk_digest={proposal.get('risk_digest')}\n",
                f"plan_digest={proposal.get('plan_digest')}\n",
                f"Reply: 确认当前计划 / 拒绝当前计划；or confirm {proposal.get('plan_digest')}",
            )
        )

    def drain_outbound(self, transport: TelegramBotTransport) -> dict[str, Any]:
        delivered = 0
        failed = 0
        rows: list[dict[str, Any]] = []
        for message in self.telegram.pending_outbound():
            try:
                result = transport.send_message(str(message.get("text") or ""), chat_id=str(message.get("chat_id") or ""))
            except TelegramBotTransportError as exc:
                result = {"ok": False, "reason": exc.code}
            receipt = self.telegram.record_send_result(
                message_id=str(message.get("message_id") or ""),
                transport_result=result,
            )
            rows.append(receipt)
            if receipt.get("status") == "delivered":
                delivered += 1
            else:
                failed += 1
        return {"delivered": delivered, "failed": failed, "messages": rows}


class ParkTelegramWorker:
    """One bounded polling pass with cursor and single-process lease."""

    def __init__(self, router: ParkTelegramRouter, *, timeout_seconds: int = 20) -> None:
        self.router = router
        self.timeout_seconds = int(timeout_seconds)
        self.cursor = _Cursor(router.output_root)
        self.lock_path = router.output_root / "park_strategy" / "telegram_worker.lock"

    def run_once(self, transport: TelegramBotTransport) -> dict[str, Any]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ParkTelegramRuntimeError("worker_already_running", "another Park Telegram worker holds the lease") from exc
            try:
                offset = self.cursor.read()
                recovered = self.router.recover_pending_legacy_cutovers()
                recovered.extend(self.router.recover_pending_strategy_inputs())
                polling_error: dict[str, Any] | None = None
                try:
                    updates = transport.get_updates(offset=offset, timeout_seconds=self.timeout_seconds)
                except TelegramBotTransportError as exc:
                    # A durable confirmation may still be consumed by the
                    # deterministic Park cleanup/runtime path.  Keep the
                    # transport failure explicit and let the next tick retry
                    # polling and delivery; never infer a new command.
                    updates = []
                    polling_error = {
                        "code": exc.code,
                        "detail": type(exc).__name__,
                        "next_action": "retry_telegram_poll",
                    }
                handled: list[dict[str, Any]] = list(recovered)
                for update in updates:
                    handled.append(self.router.handle_update(update))
                    if update.get("update_id") not in (None, ""):
                        self.cursor.advance(int(update["update_id"]))
                delivery = self.router.drain_outbound(transport)
                return {
                    "schema_version": PARK_TELEGRAM_RUNTIME_SCHEMA,
                    "status": "blocked" if polling_error else "pass",
                    **(polling_error or {}),
                    "updates_received": len(updates),
                    "updates_handled": handled,
                    "next_offset": self.cursor.read(),
                    "delivery": delivery,
                    "control_actions_executed": 0,
                    "orders_created": 0,
                    "positions_created": 0,
                    "paper_only": True,
                }
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        with path.open("ab") as destination:
            destination.write(line)
            destination.flush()
            os.fsync(destination.fileno())
        os.unlink(temp)
    except Exception:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows
