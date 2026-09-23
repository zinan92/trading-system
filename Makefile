# Trading platform — one repo behind trade.park-ai-intel.com/trade
PY ?= python3
VENV ?= .venv
VPY := $(VENV)/bin/python
ROOT := $(CURDIR)
export PYTHONPATH := $(ROOT):$(ROOT)/src:$(ROOT)/packages/standard-broker/src:$(ROOT)/packages/trading-strategy/src:$(ROOT)/apps/trading-desk/src

.PHONY: help setup setup-nautilus test test-root test-desk test-broker test-strategy test-kline gate dashboard desk lint gitleaks clean

help:
	@echo "make setup          create $(VENV) and install requirements.txt"
	@echo "make setup-nautilus create .venv-nautilus with nautilus_trader (Testnet execution only)"
	@echo "make test           run every suite (root, desk, broker, strategy, kline)"
	@echo "make gate           write the paper pre-deploy receipt the dashboard needs to boot"
	@echo "make dashboard      serve GridMind on http://127.0.0.1:8765"
	@echo "make desk           serve 交易台 on http://127.0.0.1:8790 (/trade, /desk)"
	@echo "make gitleaks       scan the tree for secrets"

setup:
	$(PY) -m venv $(VENV)
	$(VPY) -m pip install --upgrade pip
	$(VPY) -m pip install -r requirements.txt

setup-nautilus:
	$(PY) -m venv .venv-nautilus
	.venv-nautilus/bin/python -m pip install --upgrade pip
	.venv-nautilus/bin/python -m pip install nautilus_trader==1.230.0

test: test-root test-desk test-broker test-strategy test-kline

test-root:
	$(VPY) -m pytest tests -q -p no:cacheprovider

test-desk:
	cd apps/trading-desk && $(ROOT)/$(VPY) -m pytest tests -q -p no:cacheprovider

test-broker:
	cd packages/standard-broker && $(ROOT)/$(VPY) -m pytest tests -q -p no:cacheprovider

test-strategy:
	cd packages/trading-strategy && $(ROOT)/$(VPY) -m pytest tests -q -p no:cacheprovider

test-kline:
	@command -v node >/dev/null && (cd packages/standard-kline && node --test standard-kline.test.js) || echo "node not found; skipping standard-kline tests"

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
