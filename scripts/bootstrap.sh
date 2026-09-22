#!/usr/bin/env bash
# Fresh clone -> runnable /trade in one command.
#   scripts/bootstrap.sh                 # venv + deps + tests + boot receipt
#   scripts/bootstrap.sh --with-nautilus # also build the isolated nautilus venv (Testnet execution)
#   scripts/bootstrap.sh --skip-tests
set -euo pipefail
cd "$(dirname "$0")/.."

WITH_NAUTILUS=0; SKIP_TESTS=0
for arg in "$@"; do
  case "$arg" in
    --with-nautilus) WITH_NAUTILUS=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

PY="${PY:-python3}"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)'; then
  echo "python 3.13+ required (found: $("$PY" --version 2>&1)). Set PY=/path/to/python3.13" >&2
  exit 1
fi

echo "[1/5] venv + requirements"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

if [ "$WITH_NAUTILUS" = 1 ]; then
  echo "[2/5] nautilus venv (this one is large)"
  [ -d .venv-nautilus ] || "$PY" -m venv .venv-nautilus
  .venv-nautilus/bin/python -m pip install --quiet --upgrade pip
  .venv-nautilus/bin/python -m pip install --quiet nautilus_trader==1.230.0
else
  echo "[2/5] nautilus venv skipped (pass --with-nautilus for Testnet execution)"
fi

echo "[3/5] .env"
[ -f .env ] || cp .env.example .env
mkdir -p outputs data

if [ "$SKIP_TESTS" = 1 ]; then
  echo "[4/5] tests skipped"
else
  echo "[4/5] tests"
  make VENV=.venv test
fi

echo "[5/5] paper pre-deploy receipt (the dashboard refuses to boot without it)"
. scripts/_env.sh
PYTHONPATH="$PWD:$PWD/src:$PWD/packages/standard-broker/src:$PWD/packages/trading-strategy/src" \
  .venv/bin/python -m pipelines.paper_predeploy_gate --python "$PWD/.venv/bin/python" --json | tail -1

cat <<MSG

Ready. In two terminals:
  make dashboard     # http://127.0.0.1:8765  (GridMind)
  make desk          # http://127.0.0.1:8790/trade
MSG
