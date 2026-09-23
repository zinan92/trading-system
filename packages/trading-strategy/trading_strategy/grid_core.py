from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .market import Bar
from .costs import dualtrack_order_cost


GRID_LINE_STATES = {
    "armed",
    "rearmed",
    "entry_partially_filled",
    "open",
    "exit_partially_filled",
    "entry_cancel_pending",
    "open_cancelled",
    "cancelled",
    "closed",
    "stopped",
    "finalized",
}


@dataclass(frozen=True)
class GridStop:
    side: str
    price: float
    confirm: str = "touch"


@dataclass(frozen=True)
class GridResult:
    armed: bool
    traded: bool
    fills: list[dict[str, Any]]
    gross_pnl: float
    net_pnl: float
    side_notional: float
    sides: int
    round_trips: int
    stop_hit: bool
    rearms: int
    max_inventory: int
    lifecycle: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GridLineLifecycle:
    """Fail-closed state for one explicit grid line.

    Fill identifiers make execution-report replay idempotent. A line becomes
    eligible for a fresh entry only after its confirmed close has flattened the
    exposure (and any partially-filled entry remainder has been cancelled).
    """

    line_id: str
    armed_at: str
    requested_quantity: float | None = None
    state: str = "armed"
    generation: int = 1
    entry_filled_quantity: float = 0.0
    close_filled_quantity: float = 0.0
    entry_order_open: bool = True
    active: bool = True
    processed_fill_ids: set[str] = field(default_factory=set)
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not str(self.line_id).strip():
            raise ValueError("grid line_id is required")
        if int(self.generation) < 1:
            raise ValueError("grid line generation must be positive")
        if self.requested_quantity is not None and float(self.requested_quantity) <= 0:
            raise ValueError("grid line requested_quantity must be positive")
        if self.entry_filled_quantity < 0 or self.close_filled_quantity < 0:
            raise ValueError("grid line fill quantities cannot be negative")
        if self.close_filled_quantity > self.entry_filled_quantity + 1e-9:
            raise ValueError("grid line close quantity exceeds entry quantity")
        if self.state not in GRID_LINE_STATES:
            raise ValueError(f"unknown grid line state: {self.state}")
        if self.state in {"armed", "rearmed"} and self.open_quantity > 1e-9:
            raise ValueError("entry-eligible grid line cannot retain exposure")
        if (
            self.state in {"entry_partially_filled", "open", "exit_partially_filled", "open_cancelled"}
            and self.open_quantity <= 1e-9
        ):
            raise ValueError(f"grid line state {self.state} requires open exposure")
        if (
            self.state in {"cancelled", "closed", "stopped", "finalized"}
            and (self.active or self.open_quantity > 1e-9)
        ):
            raise ValueError(f"terminal grid line state {self.state} must be inactive and flat")
        if not self.transitions and self.state != "armed":
            raise ValueError("non-armed grid line snapshot requires transition history")
        if not self.transitions:
            self._record("armed", from_state="", at=self.armed_at)

    @property
    def open_quantity(self) -> float:
        return max(0.0, float(self.entry_filled_quantity) - float(self.close_filled_quantity))

    @property
    def can_enter(self) -> bool:
        return bool(self.active and self.state in {"armed", "rearmed"} and self.open_quantity <= 1e-9)

    def apply_entry_fill(self, *, fill_id: str, quantity: float, at: str) -> dict[str, Any]:
        replay = self._fill_replay(fill_id)
        if replay:
            return replay
        fill_quantity = _positive_lifecycle_quantity(quantity)
        if self.state not in {"armed", "rearmed", "entry_partially_filled"} or not self.active:
            raise ValueError(f"grid line entry fill is illegal in state {self.state}")
        if self.state in {"armed", "rearmed"} and not self.can_enter:
            raise ValueError("grid line entry would stack existing exposure")
        if self.requested_quantity is None:
            self.requested_quantity = fill_quantity
        requested = float(self.requested_quantity)
        new_total = float(self.entry_filled_quantity) + fill_quantity
        if new_total > requested + 1e-9:
            raise ValueError("grid line entry fill exceeds requested quantity")

        from_state = self.state
        self.entry_filled_quantity = min(requested, new_total)
        self.entry_order_open = self.entry_filled_quantity < requested - 1e-9
        self.state = "entry_partially_filled" if self.entry_order_open else "open"
        self.processed_fill_ids.add(str(fill_id))
        return self._record(
            "entry_partial_fill_confirmed" if self.entry_order_open else "entry_fill_confirmed",
            from_state=from_state,
            at=at,
            fill_id=fill_id,
            fill_quantity=fill_quantity,
        )

    def apply_close_fill(
        self,
        *,
        fill_id: str,
        quantity: float,
        at: str,
        rearm: bool,
        terminal_state: str = "closed",
    ) -> dict[str, Any]:
        replay = self._fill_replay(fill_id)
        if replay:
            return replay
        fill_quantity = _positive_lifecycle_quantity(quantity)
        if self.state not in {
            "entry_partially_filled",
            "open",
            "exit_partially_filled",
            "open_cancelled",
        }:
            raise ValueError(f"grid line close fill is illegal in state {self.state}")
        if fill_quantity > self.open_quantity + 1e-9:
            raise ValueError("grid line close fill exceeds open quantity")

        from_state = self.state
        closing_generation = int(self.generation)
        self.close_filled_quantity += fill_quantity
        remaining = self.open_quantity
        self.processed_fill_ids.add(str(fill_id))
        if remaining > 1e-9:
            self.state = "exit_partially_filled"
            return self._record(
                "close_partial_fill_confirmed",
                from_state=from_state,
                at=at,
                fill_id=fill_id,
                fill_quantity=fill_quantity,
            )

        if rearm and self.active and not self.entry_order_open:
            self.state = "rearmed"
            self.entry_order_open = True
            transition = self._record(
                "close_fill_confirmed_rearm",
                from_state=from_state,
                at=at,
                fill_id=fill_id,
                fill_quantity=fill_quantity,
                extra={"next_generation": closing_generation + 1},
            )
            self.generation = closing_generation + 1
            self.entry_filled_quantity = 0.0
            self.close_filled_quantity = 0.0
            return transition

        if rearm and self.active and self.entry_order_open:
            self.state = "entry_cancel_pending"
            return self._record(
                "close_fill_confirmed_waiting_entry_cancel",
                from_state=from_state,
                at=at,
                fill_id=fill_id,
                fill_quantity=fill_quantity,
            )

        self.active = False
        self.entry_order_open = False
        self.state = str(terminal_state or "closed")
        return self._record(
            "close_fill_confirmed_terminal",
            from_state=from_state,
            at=at,
            fill_id=fill_id,
            fill_quantity=fill_quantity,
        )

    def confirm_entry_cancelled(self, *, at: str, reason: str) -> dict[str, Any]:
        if not self.entry_order_open:
            raise ValueError("grid line has no open entry remainder to cancel")
        from_state = self.state
        self.entry_order_open = False
        if self.open_quantity > 1e-9:
            self.state = "exit_partially_filled" if self.close_filled_quantity > 0 else "open"
            return self._record(
                "entry_remainder_cancel_confirmed",
                from_state=from_state,
                at=at,
                extra={"reason": str(reason or "")},
            )
        if self.close_filled_quantity > 0 and self.active:
            closing_generation = int(self.generation)
            self.state = "rearmed"
            self.entry_order_open = True
            transition = self._record(
                "entry_remainder_cancel_confirmed_rearm",
                from_state=from_state,
                at=at,
                extra={"reason": str(reason or ""), "next_generation": closing_generation + 1},
            )
            self.generation = closing_generation + 1
            self.entry_filled_quantity = 0.0
            self.close_filled_quantity = 0.0
            return transition
        self.active = False
        self.state = "cancelled"
        return self._record(
            "entry_cancel_confirmed",
            from_state=from_state,
            at=at,
            extra={"reason": str(reason or "")},
        )

    def cancel(self, *, at: str, reason: str) -> dict[str, Any]:
        from_state = self.state
        self.active = False
        self.entry_order_open = False
        self.state = "open_cancelled" if self.open_quantity > 1e-9 else "cancelled"
        return self._record(
            "line_cancel_confirmed",
            from_state=from_state,
            at=at,
            extra={"reason": str(reason or "")},
        )

    def terminate(self, *, at: str, reason: str, state: str) -> dict[str, Any]:
        if self.open_quantity > 1e-9:
            raise ValueError("cannot terminate a grid line with open exposure")
        from_state = self.state
        self.active = False
        self.entry_order_open = False
        self.state = str(state or "closed")
        return self._record(
            "line_terminated",
            from_state=from_state,
            at=at,
            extra={"reason": str(reason or "")},
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "armed_at": self.armed_at,
            "requested_quantity": self.requested_quantity,
            "state": self.state,
            "generation": self.generation,
            "entry_filled_quantity": self.entry_filled_quantity,
            "close_filled_quantity": self.close_filled_quantity,
            "entry_order_open": self.entry_order_open,
            "active": self.active,
            "processed_fill_ids": sorted(self.processed_fill_ids),
            "transitions": [dict(row) for row in self.transitions],
        }

    @classmethod
    def from_snapshot(cls, payload: dict[str, Any]) -> "GridLineLifecycle":
        if not isinstance(payload, dict):
            raise ValueError("grid line lifecycle snapshot must be an object")
        return cls(
            line_id=str(payload.get("line_id") or ""),
            armed_at=str(payload.get("armed_at") or ""),
            requested_quantity=(
                None if payload.get("requested_quantity") in (None, "") else float(payload["requested_quantity"])
            ),
            state=str(payload.get("state") or ""),
            generation=int(payload.get("generation") or 0),
            entry_filled_quantity=float(payload.get("entry_filled_quantity") or 0.0),
            close_filled_quantity=float(payload.get("close_filled_quantity") or 0.0),
            entry_order_open=bool(payload.get("entry_order_open", False)),
            active=bool(payload.get("active", False)),
            processed_fill_ids={str(value) for value in payload.get("processed_fill_ids") or []},
            transitions=[dict(row) for row in payload.get("transitions") or []],
        )

    def _fill_replay(self, fill_id: str) -> dict[str, Any] | None:
        normalized = str(fill_id or "")
        if not normalized:
            raise ValueError("grid line fill_id is required")
        if normalized not in self.processed_fill_ids:
            return None
        return {
            "event": "fill_replay_ignored",
            "line_id": self.line_id,
            "generation": self.generation,
            "fill_id": normalized,
            "idempotent": True,
            "state": self.state,
            "open_quantity": round(self.open_quantity, 12),
        }

    def _record(
        self,
        event: str,
        *,
        from_state: str,
        at: str,
        fill_id: str = "",
        fill_quantity: float = 0.0,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "sequence": len(self.transitions) + 1,
            "line_id": self.line_id,
            "generation": int(self.generation),
            "event": str(event),
            "from": str(from_state),
            "to": self.state,
            "at": str(at),
            "fill_id": str(fill_id or ""),
            "fill_quantity": round(float(fill_quantity or 0.0), 12),
            "requested_quantity": (
                None if self.requested_quantity is None else round(float(self.requested_quantity), 12)
            ),
            "entry_filled_quantity": round(float(self.entry_filled_quantity), 12),
            "close_filled_quantity": round(float(self.close_filled_quantity), 12),
            "open_quantity": round(self.open_quantity, 12),
            "entry_order_open": bool(self.entry_order_open),
            "active": bool(self.active),
            **dict(extra or {}),
        }
        self.transitions.append(row)
        return row


def _positive_lifecycle_quantity(value: float) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise ValueError("grid line fill quantity must be positive")
    return parsed


def simulate_conditional_grid(
    *,
    cycle_id: str,
    bars: Iterable[Bar],
    direction: int,
    prev_range: float,
    spacing_bp: float,
    range_k: float,
    rung_notional: float,
    max_rungs: int,
    cost_per_side_bp: float,
    tp_mult: float = 1.0,
    re_arm_max: int = 0,
    budget_sizing: bool = False,
    layer: str = "grid",
    stop: GridStop | None = None,
    execution_cost_model: dict[str, Any] | None = None,
    cost_rules: dict[str, Any] | None = None,
) -> GridResult:
    rows = tuple(bars)
    if direction == 0 or not rows:
        return _idle()
    anchor = float(rows[0].open)
    spacing = anchor * float(spacing_bp) / 10_000.0
    sign = 1 if direction > 0 else -1
    plan_stop = _normalize_plan_stop(stop, sign)
    half_width = abs(anchor - plan_stop.price) if plan_stop else float(range_k) * float(prev_range)
    n_rungs = min(int(max_rungs), int(half_width / spacing)) if spacing > 0 else 0
    if n_rungs < 1:
        return _idle()
    effective_notional = (float(max_rungs) * float(rung_notional) / n_rungs) if budget_sizing else float(rung_notional)

    active_stop = plan_stop or GridStop(side="below" if sign > 0 else "above", price=anchor - sign * half_width)
    levels = (
        _levels_to_stop(anchor, sign, spacing, n_rungs, active_stop.price)
        if plan_stop
        else _levels(anchor, sign, spacing, n_rungs)
    )
    cost_config = {"cost_per_side_bp": cost_per_side_bp}
    if execution_cost_model:
        cost_config["execution_cost_model"] = execution_cost_model
    holding: dict[int, dict[str, Any]] = {}
    fills: list[dict[str, Any]] = []
    gross_pnl = 0.0
    total_cost = 0.0
    side_notional = 0.0
    sides = 0
    round_trips = 0
    stop_hit = False
    rearms = 0
    max_inventory = 0

    for i, bar in enumerate(rows):
        low, high = float(bar.low), float(bar.high)
        breached = _breached(bar, active_stop)
        # OHLC does not tell us the intrabar path.  When a bar touches a hard
        # stop and one or more entry levels, resolve the ambiguity
        # conservatively: the hard invalidation wins, so no new position may
        # be opened on an already-invalid bar.
        if breached and plan_stop is not None:
            exit_price = _stop_exit_price(bar, active_stop)
            for rung in list(holding):
                entry = holding[rung]
                exit_notional = _matched_exit_notional(entry, entry_price=levels[rung], exit_price=exit_price)
                exit_cost = dualtrack_order_cost(
                    config=cost_config,
                    price=exit_price,
                    notional=exit_notional,
                    contracts=entry.get("contracts"),
                    cost_rules=cost_rules,
                )
                pnl = sign * (exit_price - levels[rung]) * _units(levels[rung], float(entry["notional"]))
                gross_pnl += pnl
                sides += 1
                side_notional += exit_cost.notional
                total_cost += exit_cost.cost
                exit_fill = _fill(
                    cycle_id, fills, bar, side="sell" if sign > 0 else "buy",
                    price=exit_price, order_cost=exit_cost, layer=layer,
                    rung=rung, event="stop", realized_pnl=pnl - exit_cost.cost,
                    sl=active_stop.price, tp=None,
                )
                exit_fill["matched_entries"] = [_matched_entry(entry, levels[rung], pnl, exit_cost.cost)]
                fills.append(exit_fill)
            holding.clear()
            stop_hit = True
            # A plan invalidation is terminal for the cycle.  Only synthetic
            # range stops may re-arm, and only from the next bar.
            if plan_stop is not None:
                break
            if rearms < int(re_arm_max):
                rearms += 1
                re_anchor = float(bar.close)
                active_stop = GridStop(side="below" if sign > 0 else "above", price=re_anchor - sign * half_width)
                levels = _levels(re_anchor, sign, spacing, n_rungs)
                continue
            break
        for rung, level in enumerate(levels):
            if rung in holding:
                continue
            hits = low <= level if sign > 0 else high >= level
            if hits:
                entry_cost = dualtrack_order_cost(
                    config=cost_config,
                    price=level,
                    notional=effective_notional,
                    contracts=_contracts_per_rung(execution_cost_model),
                    cost_rules=cost_rules,
                )
                entry_fill = _fill(
                    cycle_id, fills, bar, side="buy" if sign > 0 else "sell",
                    price=level, order_cost=entry_cost, layer=layer,
                    rung=rung, event="entry", realized_pnl=-entry_cost.cost,
                    sl=active_stop.price, tp=level + sign * spacing * tp_mult,
                )
                holding[rung] = {
                    "bar_index": i,
                    "notional": entry_cost.notional,
                    "contracts": entry_cost.contracts,
                    "trade_id": entry_fill["fill_id"],
                }
                sides += 1
                side_notional += entry_cost.notional
                total_cost += entry_cost.cost
                max_inventory = max(max_inventory, len(holding))
                fills.append(entry_fill)
        if breached:
            exit_price = _stop_exit_price(bar, active_stop)
            for rung in list(holding):
                entry = holding[rung]
                exit_notional = _matched_exit_notional(entry, entry_price=levels[rung], exit_price=exit_price)
                exit_cost = dualtrack_order_cost(
                    config=cost_config,
                    price=exit_price,
                    notional=exit_notional,
                    contracts=entry.get("contracts"),
                    cost_rules=cost_rules,
                )
                pnl = sign * (exit_price - levels[rung]) * _units(levels[rung], float(entry["notional"]))
                gross_pnl += pnl
                sides += 1
                side_notional += exit_cost.notional
                total_cost += exit_cost.cost
                exit_fill = _fill(
                    cycle_id, fills, bar, side="sell" if sign > 0 else "buy",
                    price=exit_price, order_cost=exit_cost, layer=layer,
                    rung=rung, event="stop", realized_pnl=pnl - exit_cost.cost,
                    sl=active_stop.price, tp=None,
                )
                exit_fill["matched_entries"] = [_matched_entry(entry, levels[rung], pnl, exit_cost.cost)]
                fills.append(exit_fill)
            holding.clear()
            stop_hit = True
            if rearms < int(re_arm_max):
                rearms += 1
                re_anchor = float(bar.close)
                active_stop = GridStop(side="below" if sign > 0 else "above", price=re_anchor - sign * half_width)
                levels = _levels(re_anchor, sign, spacing, n_rungs)
                continue
            break
        for rung in list(holding):
            if int(holding[rung]["bar_index"]) >= i:
                continue
            target = levels[rung] + sign * spacing * float(tp_mult)
            done = high >= target if sign > 0 else low <= target
            if done:
                entry = holding[rung]
                exit_notional = _matched_exit_notional(entry, entry_price=levels[rung], exit_price=target)
                exit_cost = dualtrack_order_cost(
                    config=cost_config,
                    price=target,
                    notional=exit_notional,
                    contracts=entry.get("contracts"),
                    cost_rules=cost_rules,
                )
                pnl = sign * (target - levels[rung]) * _units(levels[rung], float(entry["notional"]))
                gross_pnl += pnl
                sides += 1
                side_notional += exit_cost.notional
                total_cost += exit_cost.cost
                round_trips += 1
                exit_fill = _fill(
                    cycle_id, fills, bar, side="sell" if sign > 0 else "buy",
                    price=target, order_cost=exit_cost, layer=layer,
                    rung=rung, event="target", realized_pnl=pnl - exit_cost.cost,
                    sl=active_stop.price, tp=target,
                )
                exit_fill["matched_entries"] = [_matched_entry(entry, levels[rung], pnl, exit_cost.cost)]
                fills.append(exit_fill)
                del holding[rung]

    if holding:
        last = rows[-1]
        last_close = float(last.close)
        for rung in list(holding):
            entry = holding[rung]
            exit_notional = _matched_exit_notional(entry, entry_price=levels[rung], exit_price=last_close)
            exit_cost = dualtrack_order_cost(
                config=cost_config,
                price=last_close,
                notional=exit_notional,
                contracts=entry.get("contracts"),
                cost_rules=cost_rules,
            )
            pnl = sign * (last_close - levels[rung]) * _units(levels[rung], float(entry["notional"]))
            gross_pnl += pnl
            sides += 1
            side_notional += exit_cost.notional
            total_cost += exit_cost.cost
            exit_fill = _fill(
                cycle_id, fills, last, side="sell" if sign > 0 else "buy",
                price=last_close, order_cost=exit_cost, layer=layer,
                rung=rung, event="flatten", realized_pnl=pnl - exit_cost.cost,
                sl=active_stop.price, tp=None,
            )
            exit_fill["matched_entries"] = [_matched_entry(entry, levels[rung], pnl, exit_cost.cost)]
            fills.append(exit_fill)
        holding.clear()

    return GridResult(
        armed=True,
        traded=bool(fills),
        fills=fills,
        gross_pnl=gross_pnl,
        net_pnl=gross_pnl - total_cost,
        side_notional=side_notional,
        sides=sides,
        round_trips=round_trips,
        stop_hit=stop_hit,
        rearms=rearms,
        max_inventory=max_inventory,
    )


def simulate_explicit_grid(
    *,
    cycle_id: str,
    bars: Iterable[Bar],
    direction: int,
    orders: list[dict[str, Any]],
    stop: GridStop,
    rung_notional: float,
    cost_per_side_bp: float,
    finalize: bool,
    layer: str = "ai_grid",
    entry_cutoff_bar_index: int | None = None,
    execution_cost_model: dict[str, Any] | None = None,
    cost_rules: dict[str, Any] | None = None,
) -> GridResult:
    """Replay exact AI-authored grid orders without deriving hidden levels."""
    rows = tuple(bars)
    sign = 1 if direction > 0 else -1 if direction < 0 else 0
    if not rows or sign == 0 or not orders:
        return _idle()
    active_stop = _normalize_plan_stop(stop, sign)
    if active_stop is None:
        raise ValueError("explicit grid requires a directionally valid stop")
    cost_config: dict[str, Any] = {"cost_per_side_bp": cost_per_side_bp}
    if execution_cost_model:
        cost_config["execution_cost_model"] = execution_cost_model

    holdings: dict[int, dict[str, Any]] = {}
    lifecycles = {
        rung: GridLineLifecycle(
            line_id=f"{layer}_{_plan_rung(order, rung)}",
            armed_at=rows[0].timestamp,
        )
        for rung, order in enumerate(orders)
    }
    fills: list[dict[str, Any]] = []
    gross_pnl = 0.0
    total_cost = 0.0
    side_notional = 0.0
    sides = 0
    round_trips = 0
    stop_hit = False
    rearms = 0
    max_inventory = 0

    for bar_index, bar in enumerate(rows):
        if _breached(bar, active_stop):
            stop_hit = True
            exit_price = _stop_exit_price(bar, active_stop)
            for rung, holding in list(holdings.items()):
                exit_fill, pnl, exit_cost = _close_explicit_holding(
                    cycle_id,
                    fills,
                    bar,
                    sign=sign,
                    rung=rung,
                    order=orders[rung],
                    holding=holding,
                    price=exit_price,
                    event="stop",
                    line_cycle=int(holding["line_cycle"]),
                    stop=active_stop,
                    layer=layer,
                    cost_config=cost_config,
                    cost_rules=cost_rules,
                )
                transition = lifecycles[rung].apply_close_fill(
                    fill_id=str(exit_fill["fill_id"]),
                    quantity=float(exit_fill["matched_entries"][0]["units"]),
                    at=bar.timestamp,
                    rearm=False,
                    terminal_state="stopped",
                )
                _annotate_grid_lifecycle_fill(
                    exit_fill,
                    line=lifecycles[rung],
                    line_cycle=int(holding["line_cycle"]),
                    transition=transition,
                )
                fills.append(exit_fill)
                _mark_entry_closed(fills, holding, exit_fill)
                gross_pnl += pnl
                total_cost += exit_cost.cost
                side_notional += exit_cost.notional
                sides += 1
            holdings.clear()
            for lifecycle in lifecycles.values():
                if lifecycle.active and lifecycle.open_quantity <= 1e-9:
                    lifecycle.terminate(at=bar.timestamp, reason="hard_stop", state="stopped")
            break

        low, high = float(bar.low), float(bar.high)
        entries_allowed = entry_cutoff_bar_index is None or bar_index < entry_cutoff_bar_index
        if not entries_allowed:
            for rung, lifecycle in lifecycles.items():
                if rung not in holdings and lifecycle.active and lifecycle.open_quantity <= 1e-9:
                    lifecycle.terminate(at=bar.timestamp, reason="entry_cutoff", state="cancelled")
        if entries_allowed:
            for rung, order in enumerate(orders):
                lifecycle = lifecycles[rung]
                if rung in holdings or not lifecycle.can_enter:
                    continue
                entry = float(order["entry"])
                touched = low <= entry if sign > 0 else high >= entry
                if not touched:
                    continue
                weight = float(order.get("weight", 1.0))
                requested_notional = float(order.get("notional") or (float(rung_notional) * weight))
                contracts = _weighted_contracts(execution_cost_model, weight)
                entry_cost = dualtrack_order_cost(
                    config=cost_config,
                    price=entry,
                    notional=requested_notional,
                    contracts=contracts,
                    cost_rules=cost_rules,
                )
                entry_fill = _explicit_fill(
                    cycle_id,
                    fills,
                    bar,
                    side="buy" if sign > 0 else "sell",
                    price=entry,
                    order_cost=entry_cost,
                    layer=layer,
                    rung=_plan_rung(order, rung),
                    event="entry",
                    realized_pnl=-entry_cost.cost,
                    sl=active_stop.price,
                    tp=float(order["take_profit"]),
                    line_cycle=int(lifecycle.generation),
                )
                line_cycle = int(lifecycle.generation)
                transition = lifecycle.apply_entry_fill(
                    fill_id=str(entry_fill["fill_id"]),
                    quantity=_units(entry, entry_cost.notional),
                    at=bar.timestamp,
                )
                _annotate_grid_lifecycle_fill(
                    entry_fill,
                    line=lifecycle,
                    line_cycle=line_cycle,
                    transition=transition,
                )
                holdings[rung] = {
                    "bar_index": bar_index,
                    "notional": entry_cost.notional,
                    "contracts": entry_cost.contracts,
                    "trade_id": entry_fill["trade_id"],
                    "fill_id": entry_fill["fill_id"],
                    "units": _units(entry, entry_cost.notional) if entry_cost.contracts is None else None,
                    "line_cycle": line_cycle,
                }
                fills.append(entry_fill)
                total_cost += entry_cost.cost
                side_notional += entry_cost.notional
                sides += 1
                max_inventory = max(max_inventory, len(holdings))

        for rung, holding in list(holdings.items()):
            if int(holding["bar_index"]) >= bar_index:
                continue
            target = float(orders[rung]["take_profit"])
            touched = high >= target if sign > 0 else low <= target
            if not touched:
                continue
            exit_fill, pnl, exit_cost = _close_explicit_holding(
                cycle_id,
                fills,
                bar,
                sign=sign,
                rung=rung,
                order=orders[rung],
                holding=holding,
                price=target,
                event="target",
                line_cycle=int(holding["line_cycle"]),
                stop=active_stop,
                layer=layer,
                cost_config=cost_config,
                cost_rules=cost_rules,
            )
            can_rearm = (
                (not finalize or bar_index + 1 < len(rows))
                and (entry_cutoff_bar_index is None or bar_index + 1 < entry_cutoff_bar_index)
            )
            transition = lifecycles[rung].apply_close_fill(
                fill_id=str(exit_fill["fill_id"]),
                quantity=float(exit_fill["matched_entries"][0]["units"]),
                at=bar.timestamp,
                rearm=can_rearm,
                terminal_state="cancelled",
            )
            _annotate_grid_lifecycle_fill(
                exit_fill,
                line=lifecycles[rung],
                line_cycle=int(holding["line_cycle"]),
                transition=transition,
            )
            fills.append(exit_fill)
            _mark_entry_closed(fills, holding, exit_fill)
            gross_pnl += pnl
            total_cost += exit_cost.cost
            side_notional += exit_cost.notional
            sides += 1
            round_trips += 1
            if transition.get("to") == "rearmed":
                rearms += 1
            del holdings[rung]

    if finalize:
        last = rows[-1]
        for rung, holding in list(holdings.items()):
            exit_fill, pnl, exit_cost = _close_explicit_holding(
                cycle_id,
                fills,
                last,
                sign=sign,
                rung=rung,
                order=orders[rung],
                holding=holding,
                price=float(last.close),
                event="flatten",
                line_cycle=int(holding["line_cycle"]),
                stop=active_stop,
                layer=layer,
                cost_config=cost_config,
                cost_rules=cost_rules,
            )
            transition = lifecycles[rung].apply_close_fill(
                fill_id=str(exit_fill["fill_id"]),
                quantity=float(exit_fill["matched_entries"][0]["units"]),
                at=last.timestamp,
                rearm=False,
                terminal_state="finalized",
            )
            _annotate_grid_lifecycle_fill(
                exit_fill,
                line=lifecycles[rung],
                line_cycle=int(holding["line_cycle"]),
                transition=transition,
            )
            fills.append(exit_fill)
            _mark_entry_closed(fills, holding, exit_fill)
            gross_pnl += pnl
            total_cost += exit_cost.cost
            side_notional += exit_cost.notional
            sides += 1
        holdings.clear()
        for lifecycle in lifecycles.values():
            if lifecycle.active and lifecycle.open_quantity <= 1e-9:
                lifecycle.terminate(at=last.timestamp, reason="cycle_finalized", state="finalized")

    lifecycle_rows = sorted(
        (row for lifecycle in lifecycles.values() for row in lifecycle.transitions),
        key=lambda row: (str(row.get("at") or ""), str(row.get("line_id") or ""), int(row.get("sequence") or 0)),
    )

    return GridResult(
        armed=True,
        traded=bool(fills),
        fills=fills,
        gross_pnl=round(gross_pnl, 8),
        net_pnl=round(gross_pnl - total_cost, 8),
        side_notional=round(side_notional, 8),
        sides=sides,
        round_trips=round_trips,
        stop_hit=stop_hit,
        rearms=rearms,
        max_inventory=max_inventory,
        lifecycle=lifecycle_rows,
    )


def _close_explicit_holding(
    cycle_id: str,
    fills: list[dict[str, Any]],
    bar: Bar,
    *,
    sign: int,
    rung: int,
    order: dict[str, Any],
    holding: dict[str, Any],
    price: float,
    event: str,
    line_cycle: int,
    stop: GridStop,
    layer: str,
    cost_config: dict[str, Any],
    cost_rules: dict[str, Any] | None,
) -> tuple[dict[str, Any], float, Any]:
    entry = float(order["entry"])
    exit_notional = _matched_exit_notional(holding, entry_price=entry, exit_price=price)
    exit_cost = dualtrack_order_cost(
        config=cost_config,
        price=price,
        notional=exit_notional,
        contracts=holding.get("contracts"),
        cost_rules=cost_rules,
    )
    units = _units(entry, float(holding["notional"]))
    pnl = sign * (float(price) - entry) * units
    fill = _explicit_fill(
        cycle_id,
        fills,
        bar,
        side="sell" if sign > 0 else "buy",
        price=price,
        order_cost=exit_cost,
        layer=layer,
        rung=_plan_rung(order, rung),
        event=event,
        realized_pnl=pnl - exit_cost.cost,
        sl=stop.price,
        tp=float(order["take_profit"]) if event != "stop" else None,
        gross_pnl=pnl,
        line_cycle=line_cycle,
    )
    fill["matched_entries"] = [{
        "fill_id": holding["fill_id"],
        "trade_id": holding["trade_id"],
        "units": units,
        "entry_price": entry,
        "gross_pnl": round(pnl, 8),
        "realized_pnl": round(pnl - exit_cost.cost, 8),
    }]
    return fill, pnl, exit_cost


def _explicit_fill(
    cycle_id: str,
    fills: list[dict[str, Any]],
    bar: Bar,
    *,
    side: str,
    price: float,
    order_cost: Any,
    layer: str,
    rung: int,
    event: str,
    realized_pnl: float,
    sl: float | None,
    tp: float | None,
    gross_pnl: float = 0.0,
    line_cycle: int = 1,
) -> dict[str, Any]:
    trade_id = _explicit_trade_id(cycle_id, layer=layer, rung=rung, line_cycle=line_cycle)
    units = _units(float(price), float(order_cost.notional))
    return {
        "fill_id": f"{cycle_id}_{layer}_{len(fills) + 1:04d}",
        "trade_id": trade_id,
        "ts": bar.timestamp,
        "side": side,
        "price": round(float(price), 8),
        "sl": None if sl is None else round(float(sl), 8),
        "tp": None if tp is None else round(float(tp), 8),
        "layer": layer,
        "position_id": f"{layer}_{rung}",
        "order_type": "limit" if event in {"entry", "target"} else "market",
        "out_of_plan": False,
        "gross_pnl": round(float(gross_pnl), 8),
        "realized_pnl": round(float(realized_pnl), 8),
        "event": event,
        "rung": rung,
        "pnl_units": round(units, 10),
        "remaining_units": round(units, 10) if event == "entry" else 0.0,
        "position_status": "open" if event == "entry" else "closed",
        **order_cost.fill_fields(),
    }


def _explicit_trade_id(cycle_id: str, *, layer: str, rung: int, line_cycle: int) -> str:
    base = f"{cycle_id}_{layer}_trade_{rung + 1:04d}"
    return base if int(line_cycle) == 1 else f"{base}_cycle_{int(line_cycle):04d}"


def _annotate_grid_lifecycle_fill(
    fill: dict[str, Any],
    *,
    line: GridLineLifecycle,
    line_cycle: int,
    transition: dict[str, Any],
) -> None:
    fill["grid_line_id"] = line.line_id
    fill["grid_line_cycle"] = int(line_cycle)
    fill["grid_line_transition"] = {
        key: transition[key]
        for key in ("sequence", "generation", "event", "from", "to", "next_generation")
        if key in transition
    }


def _mark_entry_closed(fills: list[dict[str, Any]], holding: dict[str, Any], exit_fill: dict[str, Any]) -> None:
    units = float((exit_fill.get("matched_entries") or [{}])[0].get("units") or 0.0)
    for fill in fills:
        if fill.get("fill_id") != holding.get("fill_id"):
            continue
        fill["remaining_units"] = 0.0
        fill["closed_units"] = round(units, 10)
        fill["position_status"] = "closed"
        return


def _weighted_contracts(execution_cost_model: dict[str, Any] | None, weight: float) -> float | None:
    base = _contracts_per_rung(execution_cost_model)
    if base is None:
        return None
    contracts = float(base) * float(weight)
    if contracts <= 0 or not contracts.is_integer():
        raise ValueError("explicit grid contract weights must produce a positive whole contract count")
    return contracts


def _plan_rung(order: dict[str, Any], fallback: int) -> int:
    return int(order.get("_plan_rung", fallback))


def _fill(
    cycle_id: str,
    fills: list[dict[str, Any]],
    bar: Bar,
    *,
    side: str,
    price: float,
    order_cost: Any,
    layer: str,
    rung: int,
    event: str,
    realized_pnl: float,
    sl: float | None,
    tp: float | None,
) -> dict[str, Any]:
    fill = {
        "fill_id": f"{cycle_id}_{layer}_{len(fills) + 1:04d}",
        "ts": bar.timestamp,
        "side": side,
        "price": round(float(price), 8),
        "sl": None if sl is None else round(float(sl), 8),
        "tp": None if tp is None else round(float(tp), 8),
        "layer": layer,
        "order_type": "limit",
        "out_of_plan": False,
        "realized_pnl": float(realized_pnl),
        "event": event,
        "rung": rung,
    }
    fill.update(order_cost.fill_fields())
    return fill


def _levels(anchor: float, sign: int, spacing: float, n_rungs: int) -> list[float]:
    return [anchor - sign * spacing * (i + 1) for i in range(n_rungs)]


def _levels_to_stop(anchor: float, sign: int, spacing: float, n_rungs: int, stop_price: float) -> list[float]:
    levels = [anchor - sign * spacing * (i + 1) for i in range(max(0, n_rungs - 1))]
    levels.append(float(stop_price))
    if sign > 0:
        return [level for level in levels if level >= float(stop_price)]
    return [level for level in levels if level <= float(stop_price)]


def _normalize_plan_stop(stop: GridStop | None, sign: int) -> GridStop | None:
    if stop is None:
        return None
    expected_side = "below" if sign > 0 else "above"
    if stop.side != expected_side:
        return None
    return stop


def _breached(bar: Bar, stop: GridStop) -> bool:
    price = float(stop.price)
    if stop.side == "below":
        return float(bar.close if stop.confirm == "close_1m" else bar.low) <= price
    if stop.side == "above":
        return float(bar.close if stop.confirm == "close_1m" else bar.high) >= price
    raise ValueError("stop.side must be below or above")


def _stop_exit_price(bar: Bar, stop: GridStop) -> float:
    price = float(stop.price)
    return min(float(bar.open), price) if stop.side == "below" else max(float(bar.open), price)


def _units(level: float, notional: float) -> float:
    return notional / level if level > 0 else 0.0


def _matched_exit_notional(entry: dict[str, Any], *, entry_price: float, exit_price: float) -> float:
    if entry.get("contracts") is not None:
        return float(entry["notional"])
    return _units(entry_price, float(entry["notional"])) * float(exit_price)


def _matched_entry(entry: dict[str, Any], entry_price: float, gross_pnl: float, exit_cost: float) -> dict[str, Any]:
    return {
        "trade_id": str(entry["trade_id"]),
        "units": _units(entry_price, float(entry["notional"])),
        "gross_pnl": round(float(gross_pnl), 8),
        "realized_pnl": round(float(gross_pnl) - float(exit_cost), 8),
    }


def _contracts_per_rung(execution_cost_model: dict[str, Any] | None) -> float | None:
    if not execution_cost_model or not execution_cost_model.get("venue"):
        return None
    value = execution_cost_model.get("contracts_per_rung", execution_cost_model.get("min_contracts", 1))
    contracts = float(value)
    if contracts <= 0:
        raise ValueError("contracts_per_rung must be positive")
    if not contracts.is_integer():
        raise ValueError("contracts_per_rung must be a whole number")
    return contracts


def _cost(notional: float, cost_per_side_bp: float) -> float:
    return float(notional) * float(cost_per_side_bp) / 10_000.0


def _idle() -> GridResult:
    return GridResult(
        armed=False,
        traded=False,
        fills=[],
        gross_pnl=0.0,
        net_pnl=0.0,
        side_notional=0.0,
        sides=0,
        round_trips=0,
        stop_hit=False,
        rearms=0,
        max_inventory=0,
    )
