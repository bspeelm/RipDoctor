# RipDoctor.
#
# `make check` is the gate. It is what CI runs and what must pass before a
# commit. Everything else here is a component of it or a convenience.

PY ?= .venv/bin/python

.PHONY: help venv lint types test cov budgets check wheel release clean

help:
	@echo "make check     lint, types, tests, budgets - the gate"
	@echo "make venv      create .venv and install the package editable"
	@echo "make lint      ruff format --check and ruff check"
	@echo "make types     mypy (strict on core/)"
	@echo "make test      pytest"
	@echo "make cov       pytest with coverage, gated on core/ only"
	@echo "make budgets   size budgets; failing one means retire, not raise"
	@echo "make wheel     build the wheel and check its size"
	@echo "make release VERSION=x.y.z   tag it; Actions builds and publishes"

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

# There is no version to bump: hatch-vcs writes it from the tag, so the tag is
# the release. What is left is the checking that the tag is being put somewhere
# it can be reproduced from - a green tree, on main, level with the remote.
release:
	@test -n "$(VERSION)" || { echo "usage: make release VERSION=x.y.z"; exit 1; }
	@echo "$(VERSION)" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$$' || \
	    { echo "VERSION must be x.y.z"; exit 1; }
	@test "$$(git rev-parse --abbrev-ref HEAD)" = main || \
	    { echo "release from main"; exit 1; }
	@git diff --quiet && git diff --cached --quiet || \
	    { echo "working tree is dirty; commit first"; exit 1; }
	@! git rev-parse -q --verify "refs/tags/v$(VERSION)" >/dev/null || \
	    { echo "v$(VERSION) already exists"; exit 1; }
	git fetch --quiet origin main
	@test "$$(git rev-parse HEAD)" = "$$(git rev-parse origin/main)" || \
	    { echo "main is not level with origin/main; run 'git pull'"; exit 1; }
	$(MAKE) check
	git tag -a "v$(VERSION)" -m "v$(VERSION)"
	git push origin "v$(VERSION)"
	@echo
	@echo "pushed v$(VERSION). Actions runs the gates again at the tag, builds,"
	@echo "attests, publishes the release and uploads to PyPI."

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
