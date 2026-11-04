PY ?= .venv/bin/python
.PHONY: venv install lint types test verify selftest clean-caches

venv:
	uv venv --python 3.12 .venv || python3.12 -m venv .venv
install: venv
	uv pip install --python $(PY) -r requirements-dev.txt -e . || ($(PY) -m pip install -r requirements-dev.txt -e .)
lint:
	$(PY) -m ruff check src tests scripts
	$(PY) -m ruff format --check src tests scripts
	PATH="$(dir $(abspath $(PY))):$$PATH" actionlint
types:
	$(PY) -m mypy src
test:
	$(PY) -m pytest -q
verify: lint types test
selftest:
	$(PY) -m odj selftest
clean-caches:
	rm -rf .mypy_cache .ruff_cache .pytest_cache
