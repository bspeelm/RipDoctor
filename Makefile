# RipDoctor.
#
# `make check` is the gate. It is what CI runs and what must pass before a
# commit. Everything else here is a component of it or a convenience.

PY ?= .venv/bin/python

.PHONY: help venv lint types test cov budgets check wheel clean

help:
	@echo "make check     lint, types, tests, budgets - the gate"
	@echo "make venv      create .venv and install the package editable"
	@echo "make lint      ruff format --check and ruff check"
	@echo "make types     mypy (strict on core/)"
	@echo "make test      pytest"
	@echo "make cov       pytest with coverage, gated on core/ only"
	@echo "make budgets   size budgets; failing one means retire, not raise"
	@echo "make wheel     build the wheel and check its size"

venv:
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

lint:
	$(PY) -m ruff format --check .
	$(PY) -m ruff check .

types:
	$(PY) -m mypy

test:
	$(PY) -m pytest

# Scoped to core/ deliberately. A whole-project coverage number lets untested
# application code hide behind a well-tested algorithm, which is the opposite of
# what the number is for.
cov:
	$(PY) -m pytest --cov=ripdoctor/core --cov-report=term-missing --cov-fail-under=95

budgets:
	$(PY) scripts/budgets.py

check: lint types test budgets

wheel:
	$(PY) -m pip install --quiet build
	$(PY) scripts/budgets.py --wheel

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
