# sqlassay. Every target runs against the project venv, never system python.
PY := .venv/bin/python
DB := run/sqlassay.db

.PHONY: install verify lint types test test-model selftest clean

install:
	uv venv --python 3.12 .venv
	$(PY) -m ensurepip --upgrade
	$(PY) -m pip install -e ".[dev]"

verify: lint types test

lint:
	$(PY) -m ruff check src tests

types:
	$(PY) -m mypy src

# Excludes anything needing a live model, so this gate stays hermetic.
test:
	$(PY) -m pytest -q -m "not model" --cov=sqlassay --cov-report=term-missing

# The live half: needs Ollama up with the configured models.
test-model:
	$(PY) -m pytest -q -m model

# Every gate's planted-defect probes. Proves each gate can still go red.
selftest:
	$(PY) -m sqlassay.cli selftest

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache run/*.db
