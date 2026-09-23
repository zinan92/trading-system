#!/usr/bin/env bash
# Serve 交易台 (trading-desk) on 127.0.0.1:8790. /trade forwards to the dashboard same-origin.
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/_env.sh   # shell env > .env > built-in defaults
export PYTHONPATH="$PWD/apps/trading-desk/src:$PWD:$PWD/packages/standard-broker/src"
PY="${TRADING_DESK_PYTHON:-$PWD/.venv/bin/python}"
[ -x "$PY" ] || PY=python3
exec "$PY" -m trading_desk serve --port "${TRADING_DESK_PORT:-8790}"
