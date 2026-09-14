#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${repo_root}:${repo_root}/src:${repo_root}/packages/standard-broker/src"
exec "/Users/wendy/.local/share/trading-orchestrator/nautilus-1.230.0/bin/python" -m pytest tests/testnet_replay -q
