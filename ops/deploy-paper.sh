#!/usr/bin/env bash
# Deploy one exact trading-system main commit to Goldbot V5 Paper (#1341).
#
#   ops/deploy-paper.sh <sha>             fast-forward production, wait for the gate, restart the desk
#   ops/deploy-paper.sh <sha> --dashboard also restart the GridMind dashboard (8765)
#   ops/deploy-paper.sh <sha> --dry-run   run every check, change nothing
#
# Refuses (exit 1, nothing changed) when:
#   - <sha> is not origin/main's head, or the production checkout cannot fast-forward to it;
#   - Paper is not idle: an active strategy, an open or unknown order, or an open position;
#   - the pre-deploy gate does not pass at <sha> within GATE_WAIT_SECONDS.
# It never places, cancels or closes an order and never reads exchange credentials. Restarting the
# dashboard is only allowed when Paper is idle, so no order lifecycle is restarted mid-flight.
set -euo pipefail

sha=${1:-}
[[ -n "$sha" && "$sha" != --* ]] || { echo "usage: ops/deploy-paper.sh <main-sha> [--dashboard] [--dry-run]" >&2; exit 2; }
shift
restart_dashboard=0 dry_run=0
for arg in "$@"; do
  case $arg in
    --dashboard) restart_dashboard=1 ;;
    --dry-run) dry_run=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PROD=${TRADING_PLATFORM_CHECKOUT:-$HOME/work/trading-platform}
GATE=${PAPER_PREDEPLOY_RECEIPT:-$HOME/work/park-paper-output/release_gates/paper_predeploy_current.json}
DASHBOARD=${PAPER_DASHBOARD_URL:-http://127.0.0.1:8765}
DESK=${PAPER_DESK_URL:-http://127.0.0.1:8790}
GATE_WAIT_SECONDS=${GATE_WAIT_SECONDS:-1800}
uid=$(id -u)

step() { echo "== $*"; }
fail() { echo "REFUSED: $*" >&2; exit 1; }

step "1/5 commit is origin/main's head"
git -C "$PROD" fetch -q origin
full=$(git -C "$PROD" rev-parse --verify "$sha^{commit}" 2>/dev/null) || fail "$sha is not a commit in $PROD"
main=$(git -C "$PROD" rev-parse origin/main)
[[ "$full" == "$main" ]] || fail "$full is not origin/main ($main); deploy only the exact main commit"
git -C "$PROD" merge-base --is-ancestor HEAD "$full" || fail "production HEAD is not an ancestor of $full; it cannot fast-forward"
[[ -z "$(git -C "$PROD" status --porcelain --untracked-files=no)" ]] || fail "production checkout has tracked changes"

step "2/5 Paper is idle"
model=$(curl -fsS --max-time 15 "$DASHBOARD/api/trading-system/read-model") || fail "read model unreachable at $DASHBOARD"
idle=$(printf '%s' "$model" | python3 -c '
import json, sys
m = json.load(sys.stdin)
active = bool((m.get("current_strategy") or {}).get("active"))
runtime = m.get("runtime") or {}
counts = (m.get("execution") or {}).get("counts") or {}
busy = {
    "active_strategy": active,
    "open_orders": int(runtime.get("open_order_count") or 0),
    "unknown_orders": int(runtime.get("unknown_order_count") or 0),
    "open_positions": int(counts.get("open_position_count") or 0),
}
bad = {k: v for k, v in busy.items() if v}
print("idle" if not bad else "busy " + json.dumps(bad))
')
[[ "$idle" == idle ]] || fail "Paper is not idle: ${idle#busy }"
echo "   idle: no active strategy, no open or unknown orders, no open positions"

if (( dry_run )); then
  services="desk"; (( restart_dashboard )) && services="dashboard and desk"
  step "dry run: would fast-forward $PROD to ${full:0:7}, wait for the gate, restart the $services"
  exit 0
fi

step "3/5 fast-forward production to ${full:0:7}"
git -C "$PROD" merge -q --ff-only "$full"

step "4/5 wait for the pre-deploy gate to pass at ${full:0:7} (up to ${GATE_WAIT_SECONDS}s)"
deadline=$(( $(date +%s) + GATE_WAIT_SECONDS ))
gate_state() {
  python3 - "$GATE" "$full" <<'PY'
import json, sys
try:
    receipt = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    print("unreadable"); raise SystemExit
receipt = receipt[0] if isinstance(receipt, list) and receipt else receipt
print("pass" if receipt.get("status") == "pass" and receipt.get("source_sha") == sys.argv[2] else f"{receipt.get('status')}@{str(receipt.get('source_sha'))[:7]}")
PY
}
until [[ "$(gate_state)" == pass ]]; do
  (( $(date +%s) < deadline )) || fail "gate is $(gate_state), not pass at ${full:0:7}; production checkout is at ${full:0:7} but nothing was restarted"
  sleep "${GATE_POLL_SECONDS:-30}"
done
echo "   gate pass at ${full:0:7}"

step "5/5 restart and verify"
if (( restart_dashboard )); then
  launchctl kickstart -k "gui/$uid/com.wendy.trading-orchestrator.dashboard"
fi
launchctl kickstart -k "gui/$uid/com.wendy.trading-desk"
for _ in $(seq 1 "${DESK_WAIT_TRIES:-40}"); do
  [[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$DESK/trade")" == 200 ]] && break
  sleep 3
done
for path in /trade /static/web/web.js /api/watch; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$DESK$path")
  echo "   $path $code"
  [[ "$code" == 200 ]] || fail "$DESK$path returned $code after restart"
done
echo "deployed ${full:0:7}: gate pass, desk serving. Check the changed page in a browser before calling it done."
