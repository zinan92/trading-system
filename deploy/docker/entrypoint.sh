#!/usr/bin/env bash
# Container start (#1343): boot receipt, then GridMind and 交易台 side by side. If either exits, the
# container exits, and compose's restart policy brings both back together.
set -euo pipefail
cd /app

export PYTHONPATH=/app:/app/src:/app/packages/standard-broker/src:/app/packages/trading-strategy/src:/app/apps/trading-desk/src
export TRADING_ORCHESTRATOR_OUTPUT_ROOT="${TRADING_ORCHESTRATOR_OUTPUT_ROOT:-/data/outputs}"
export TRADING_ORCHESTRATOR_MARKET_DB="${TRADING_ORCHESTRATOR_MARKET_DB:-/data/market_data.db}"
export TRADING_DESK_PAPER_OUTPUT="${TRADING_DESK_PAPER_OUTPUT:-$TRADING_ORCHESTRATOR_OUTPUT_ROOT}"
export TRADING_DESK_DB="${TRADING_DESK_DB:-/data/desk/desk.db}"
export TRADING_DESK_WATCH="${TRADING_DESK_WATCH:-/data/desk/watch}"
export TRADING_DESK_NAUTILUS_PY="${TRADING_DESK_NAUTILUS_PY:-$(command -v python)}"
mkdir -p "$TRADING_ORCHESTRATOR_OUTPUT_ROOT" "$(dirname "$TRADING_DESK_DB")" "$TRADING_DESK_WATCH"

# The dashboard refuses to boot without a passing receipt for this exact commit; write it first.
# A failing receipt is printed and the dashboard then shows why, rather than starting anyway.
python -m pipelines.paper_predeploy_gate --python "$(command -v python)" --json | tail -c 400 || true
echo

# Default: bind every interface of the container; compose publishes them on the host's 127.0.0.1
# only. With host networking (compose.host-network.yaml) BIND_HOST is 127.0.0.1.
bind="${BIND_HOST:-0.0.0.0}"
python -m pipelines.dashboard_server --host "$bind" --port 8765 &
dashboard=$!
python -m trading_desk serve --host "$bind" --port 8790 &
desk=$!
trap 'kill "$dashboard" "$desk" 2>/dev/null || true' TERM INT
set +e
wait -n "$dashboard" "$desk"
status=$?
kill "$dashboard" "$desk" 2>/dev/null
wait
exit "$status"
