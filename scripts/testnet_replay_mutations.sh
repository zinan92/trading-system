#!/usr/bin/env bash
set -euo pipefail

# Issue #1251: mutation gate for the historical Testnet replay failures.
# Each mutation is applied only to a detached temporary copy of this commit.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="/Users/wendy/.local/share/trading-orchestrator/nautilus-1.230.0/bin/python"
tmp_root="$(mktemp -d "${TMPDIR:-/tmp}/testnet-replay-mutations.XXXXXX")"
trap 'rm -rf "$tmp_root"' EXIT

git -C "$repo_root" archive HEAD | tar -x -C "$tmp_root"

mutate() {
  local name="$1" file="$2" old="$3" new="$4"
  python3 - "$tmp_root/$file" "$old" "$new" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
old = sys.argv[2].encode().decode("unicode_escape")
new = sys.argv[3].encode().decode("unicode_escape")
if text.count(old) != 1:
    raise SystemExit(f"mutation pattern count {text.count(old)} != 1: {path}")
path.write_text(text.replace(old, new, 1))
PY
  if (cd "$tmp_root" && PYTHONPATH="$tmp_root:$tmp_root/src:/Users/wendy/work/standard-broker/src" "$python_bin" -m pytest tests/testnet_replay -q >/dev/null 2>&1); then
    printf '%-46s survived\n' "$name"
    return 1
  fi
  printf '%-46s caught\n' "$name"
  git -C "$repo_root" archive HEAD | tar -x -C "$tmp_root"
}

printf '%-46s result\n' mutation
printf '%-46s ------\n' ----------------------------------------------
mutate '#1239 IOC TP' services/grid_testnet_lifecycle.py 'time_in_force="gtc",\n            planned_price=float(rung["tp"])' 'time_in_force="ioc",\n            planned_price=float(rung["tp"])'
mutate '#1250 admit every historical fill' pipelines/testnet_automation_proof.py 'if occurred_at >= cutoff:' 'if True:'
mutate '#1255 _snapshot requires reconciliation.passed' pipelines/testnet_automation_proof.py 'if not fresh:' 'if not reconciliation_passed or not fresh:'
mutate '#1257 authoritative snapshot rejects historical fills' pipelines/testnet_automation_proof.py 'if failure_reasons == ("unattributed_fills",):' 'if False:'
mutate '#1241 skip hard-stop flag at recovery' services/grid_testnet_lifecycle.py 'if str(state.get("blocker") or "").startswith("order_cancel_failed"):\n                state["hard_stop_requested"] = True' 'if str(state.get("blocker") or "").startswith("order_cancel_failed"):\n                pass'
mutate '#1241 skip hard-stop flag at emergency flatten' services/grid_testnet_lifecycle.py 'state["status"] = "hard_stop_triggered"\n            state["hard_stop_requested"] = True' 'state["status"] = "hard_stop_triggered"\n            pass'
mutate '#1241 skip shared hard-stop marker' services/grid_testnet_lifecycle.py 'def _mark_hard_stop_requested(state: dict[str, Any], reason: str) -> None:\n        state["hard_stop_requested"] = True' 'def _mark_hard_stop_requested(state: dict[str, Any], reason: str) -> None:\n        pass'
mutate '#1241 skip heartbeat hard-stop marker' services/grid_testnet_lifecycle.py '"""Retry one aggressive flatten per heartbeat using venue position truth."""\n        state["hard_stop_requested"] = True' '"""Retry one aggressive flatten per heartbeat using venue position truth."""\n        pass'
mutate '#1248 strict startup price equality' services/testnet_market_document.py 'tolerance = min(slippage, reader_number * bps / Decimal("10000"))' 'tolerance = Decimal("0")'
mutate '#1253 tick path back to strict equality' services/testnet_market_document.py 'if deviation > tolerance:' 'if deviation > Decimal("0"): '
mutate '#1253 binding must sit inside raw BBO' services/testnet_market_document.py 'or not (bid_number - tolerance <= binding_number <= ask_number + tolerance)' 'or not (bid_number <= binding_number <= ask_number)'
mutate '#1231 remove paused callback state' services/testnet_automation_coordinator.py '        "grid_paused_range",\n        "dca_running",' '        "dca_running",'
mutate '#1231 remove paused advance state' services/testnet_automation_coordinator.py '            "grid_paused_range",\n            "grid_interrupted",' '            "grid_interrupted",'
mutate '#1243 skip heartbeat when facts contain fills' pipelines/park_control.py '        for raw_fill in fills:' '        for raw_fill in []:'
mutate '#1237 skip filled and cancelled hydration rows' pipelines/park_control.py '        recovered_state = recovery_states.get(persisted_state)' '        if persisted_state in {"filled", "cancelled"}:\n            continue\n        recovered_state = recovery_states.get(persisted_state)'
mutate '#1239 protection group missing TP leg' services/grid_testnet_lifecycle.py '            take_profit=ProtectionLeg(protection_type=ProtectionType.TAKE_PROFIT, execution=ProtectionExecution.LIMIT, trigger_price=Decimal(str(tp)), limit_price=Decimal(str(tp))),' '            take_profit=None,'
mutate '#1259 slippage back to abs()' services/grid_testnet_lifecycle.py '        slippage, price_improvement = self._slippage_metrics(\n            order.get("side"), price, planned,\n        )' '        slippage = abs(price - planned)\n        price_improvement = 0.0'
mutate '#1261 flat blocked state stays silent' services/grid_testnet_lifecycle.py 'self._seal_venue_flat(plan, state, facts["fills"] or (), timestamp=timestamp)' 'pass'
mutate '#1261 heartbeat flat waits on local quantity' services/grid_testnet_lifecycle.py 'self._seal_venue_flat(plan, state, facts.get("fills") or (), timestamp=timestamp)' 'self._maybe_finalize_hard_stop(plan, state, timestamp=timestamp)'
mutate '#1261 flatten row not recorded before submit' services/grid_testnet_lifecycle.py '        state["orders"].append(row)\n        try:\n            receipt = submit()' '        try:\n            receipt = submit()'
mutate '#1261 venue cloid not attributed' services/grid_testnet_lifecycle.py 'or (cloid and cloid in {str(item.get("client_order_id") or ""), str(item.get("native_client_order_id") or "")})' 'or False'
mutate '#1261 review: unknown flatten not treated as open' services/grid_testnet_lifecycle.py 'row.get("state") in {"accepted", "submit_pending", "submit_unknown"}\n            and row.get("event") in {"hard_stop", "hard_stop_recovery"}' 'row.get("state") == "accepted"\n            and row.get("event") in {"hard_stop", "hard_stop_recovery"}'
mutate '#1261 review: unapplied exit fill still seals' services/grid_testnet_lifecycle.py 'if self._attribute_venue_exit_fills(state, fact_fills, timestamp=timestamp):' 'if self._attribute_venue_exit_fills(state, fact_fills, timestamp=timestamp) and False:'
mutate '#1261 review: blocker text seals without hard-stop request' services/grid_testnet_lifecycle.py 'if state.get("hard_stop_requested"):\n                self._seal_venue_flat(' 'if state.get("hard_stop_requested") or state.get("blocker") == "position_open_unprotected":\n                self._seal_venue_flat('
mutate '09-11 market gate counts as execution strike' services/testnet_scheduler.py 'if str(getattr(exc, "code", "") or "") in _READ_SIDE_ERROR_CODES:' 'if False:'
mutate '09-11 read-side block never resumes' services/testnet_scheduler.py 'and not (self._read_side_blocked(current) and event is not None and callable(advance)):' ':'
mutate '09-11 read-side block resumes without a probe' services/testnet_scheduler.py 'and not (self._read_side_blocked(current) and event is not None and callable(advance)):' 'and not self._read_side_blocked(current):'
mutate '09-14 reconcile_stop ignores lifecycle orders' services/testnet_automation_coordinator.py 'if self._lifecycle_has_open_exposure(current):' 'if False:'
mutate '09-11 control pass skips read-side block' pipelines/park_control.py 'and not scheduler._read_side_blocked(scheduler_status):' ':'
