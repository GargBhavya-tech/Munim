ifeq ($(OS),Windows_NT)
PY := .venv/Scripts/python
PYTHON := py -3.12
# read_text() without an encoding would decode Hindi/Kannada sources as cp1252
export PYTHONUTF8 := 1
else
PY := .venv/bin/python
PYTHON := python3
endif
PIP := $(PY) -m pip

.PHONY: setup setup-ml test dashboard seed

# One command from a fresh clone to green pytest and a built dashboard.
# Windows without make: scripts/setup_windows.ps1 does the same.
setup:
	$(PYTHON) -m venv .venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements.txt
	bash scripts/install_hooks.sh
	cd dashboard && npm ci && npm run build
	$(PY) -m pytest

# Model dependencies for the grounding gate (large download).
setup-ml:
	$(PIP) install -q -r requirements-ml.txt

test:
	$(PY) -m pytest

dashboard:
	cd dashboard && npm run build

seed:
	$(PY) data/seed.py --seed 42
