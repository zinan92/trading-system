#!/bin/sh
set -eu

# The Paper service runs as the dedicated gridmind user.  Keep the Codex
# authentication directory on the Cloud host and make the provider identity
# explicit; no Mac file bridge or repository credential is consulted.
export HOME="${GRIDMIND_CODEX_HOME_ROOT:-/opt/gridmind}"
export CODEX_HOME="${GRIDMIND_CODEX_HOME:-/opt/gridmind/.codex}"

exec /opt/gridmind/bin/codex "$@"
