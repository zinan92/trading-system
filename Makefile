# Trading platform — one repo behind trade.park-ai-intel.com/trade
PY ?= python3
VENV ?= .venv
VPY := $(VENV)/bin/python
ROOT := $(CURDIR)
export PYTHONPATH := $(ROOT):$(ROOT)/src:$(ROOT)/packages/standard-broker/src:$(ROOT)/packages/trading-strategy/src:$(ROOT)/apps/trading-desk/src

.PHONY: help setup setup-nautilus test test-root test-desk test-broker test-strategy test-kline test-web web coverage coverage-report gate dashboard desk lint gitleaks clean

help:
	@echo "make setup          create $(VENV) and install requirements-dev.txt (runtime + test deps)"
	@echo "make setup-nautilus create .venv-nautilus with nautilus_trader (Testnet execution only)"
	@echo "make test           run every suite (root, desk, broker, strategy, kline, web)"
	@echo "make coverage       root + desk suites with line coverage; per-package summary + coverage.xml"
	@echo "make web            rebuild the React islands into apps/trading-desk/.../static/web/web.js"
	@echo "make gate           write the paper pre-deploy receipt the dashboard needs to boot"
	@echo "make dashboard      serve GridMind on http://127.0.0.1:8765"
	@echo "make desk           serve 交易台 on http://127.0.0.1:8790 (/trade, /desk)"
	@echo "make gitleaks       scan the tree for secrets"

setup:
	$(PY) -m venv $(VENV)
	$(VPY) -m pip install --upgrade pip
	$(VPY) -m pip install -r requirements-dev.txt

setup-nautilus:
	$(PY) -m venv .venv-nautilus
	.venv-nautilus/bin/python -m pip install --upgrade pip
	.venv-nautilus/bin/python -m pip install nautilus_trader==1.230.0

test: test-root test-desk test-broker test-strategy test-kline test-web

# COV=1 measures line coverage while the suites run (no second run); coverage-report sums it up.
# pytest-cov starts each data file fresh, so a stale run never leaks into the numbers.
ifeq ($(COV),1)
ROOT_COV := --cov=services --cov=pipelines --cov=schemas --cov-report=
DESK_COV := --cov=trading_desk --cov-report=
endif

test-root:
	COVERAGE_FILE=$(ROOT)/.coverage.root $(VPY) -m pytest tests -q -p no:cacheprovider $(ROOT_COV)

test-desk:
	cd apps/trading-desk && COVERAGE_FILE=$(ROOT)/.coverage.desk $(ROOT)/$(VPY) -m pytest tests -q -p no:cacheprovider $(DESK_COV)

coverage:
	$(MAKE) test-root test-desk COV=1
	$(MAKE) coverage-report

coverage-report:
	$(VPY) -m coverage combine --keep .coverage.root .coverage.desk
	@for pkg in services pipelines schemas trading_desk; do \
		printf '%-14s %s%%\n' $$pkg "$$($(VPY) -m coverage report --include="*/$$pkg/*" --format=total)"; \
	done
	@printf '%-14s %s%%\n' total "$$($(VPY) -m coverage report --format=total)"
	$(VPY) -m coverage xml -q -o coverage.xml

test-broker:
	cd packages/standard-broker && $(ROOT)/$(VPY) -m pytest tests -q -p no:cacheprovider

test-strategy:
	cd packages/trading-strategy && $(ROOT)/$(VPY) -m pytest tests -q -p no:cacheprovider

# A failing widget test must fail the target; only a missing node is a skip.
test-kline:
	@if command -v node >/dev/null; then cd packages/standard-kline && node --test standard-kline.test.js; else echo "node not found; skipping standard-kline tests"; fi

# React islands (apps/web, #1325). The built web.js is committed; test-web fails when it is stale.
WEB_OUT := apps/trading-desk/src/trading_desk/static/web

web:
	cd apps/web && (test -d node_modules || npm ci --no-audit --no-fund) && npm run build

test-web:
	@command -v npm >/dev/null || { echo "npm not found; skipping web tests"; exit 0; }; \
	set -e; cd apps/web; test -d node_modules || npm ci --no-audit --no-fund; \
	npm run typecheck; npm test; npm run build; cd $(ROOT); \
	stale=$$(git status --porcelain -- $(WEB_OUT)); \
	if [ -n "$$stale" ]; then echo "$(WEB_OUT) is stale: run make web and commit it"; echo "$$stale"; exit 1; fi

gate:
	$(VPY) -m pipelines.paper_predeploy_gate --python $(ROOT)/$(VPY) --json | tail -c 400

dashboard:
	scripts/run-dashboard.sh

desk:
	scripts/run-desk.sh

gitleaks:
	gitleaks dir . --no-banner --redact

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
