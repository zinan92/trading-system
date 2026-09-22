#!/usr/bin/env bash
# Serve GridMind (the trading console) on 127.0.0.1:8765.
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/_env.sh   # shell env > .env > built-in defaults
export PYTHONPATH="$PWD:$PWD/src:$PWD/packages/standard-broker/src:$PWD/packages/trading-strategy/src"
PY="${TRADING_ORCHESTRATOR_PYTHON:-$PWD/.venv/bin/python}"
[ -x "$PY" ] || PY=python3
exec "$PY" -m pipelines.dashboard_server --host "${DASHBOARD_HOST:-127.0.0.1}" --port "${DASHBOARD_PORT:-8765}"
