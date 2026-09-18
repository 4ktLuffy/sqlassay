# sqlassay. Every target runs against the project venv, never system python.
PY := .venv/bin/python
DB := run/sqlassay.db

.PHONY: install verify lint types test test-model selftest artifacts readme-check number-audit docs clean

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

# Regenerate the derived artifacts that need no model and no BIRD download.
artifacts:
	$(PY) tools/make_revision_diff.py
	$(PY) tools/derive_bands.py reports/floors_bird_dev_20251106.json

# Every number in README.md and LIMITATIONS.md, regenerated from reports/ and
# diffed. Reads no database, no model, no network.
readme-check:
	$(PY) tools/readme_check.py

# Fails if a number in those documents is neither regenerated above nor listed
# in NUMBERS.md with the command or source behind it.
number-audit:
	$(PY) tools/number_audit.py

docs: readme-check number-audit

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache run/*.db
