# One image for /trade: GridMind (8765) + 交易台 (8790). `docker compose up` builds and runs it (#1343).
# Paper/Testnet only; there is no real-money path in this repository. No key, passcode or .env is copied
# in: secrets stay on the host and are mounted read-only when wanted (see README "Docker").
FROM python:3.13.16-slim-bookworm

# 1 adds nautilus_trader (Testnet orders, Paper ledger, grid backtests). Without it the product is read-only.
ARG WITH_NAUTILUS=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

# git: the boot receipt binds the exact source commit and refuses a changed tree.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-optional.txt ./
RUN pip install -r requirements.txt \
 && if [ "$WITH_NAUTILUS" = "1" ]; then pip install "$(grep -E '^nautilus_trader==' requirements-optional.txt)"; fi

COPY . .
# A build from a tree with uncommitted tracked changes would boot into "source tree dirty"; say so now.
RUN git config --system --add safe.directory /app \
 && if [ -n "$(git status --porcelain --untracked-files=no)" ]; then \
      echo "build context has uncommitted tracked changes; commit or stash them first:"; git status --short --untracked-files=no; exit 1; \
    fi

# Runtime state lives in the /data volume. outputs/ is untracked, so linking it there keeps the tree
# clean; the gate and dashboard write <repo>/outputs whatever TRADING_ORCHESTRATOR_OUTPUT_ROOT says.
RUN useradd --create-home --uid 10001 park \
 && mkdir -p /data/outputs /data/desk \
 && ln -s /data/outputs /app/outputs \
 && chown -R park /data /app/data
USER park
# /app stays read-only for the runtime user; the boot receipt's compile probe writes bytecode under
# PYTHONPYCACHEPREFIX instead of next to the sources.
ENV PYTHONPYCACHEPREFIX=/tmp/pycache

EXPOSE 8765 8790
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD curl -fsS http://127.0.0.1:8790/api/health || exit 1
ENTRYPOINT ["deploy/docker/entrypoint.sh"]
