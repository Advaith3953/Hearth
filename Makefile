PY ?= python3
.PHONY: install lint typecheck test cov eval-selftest check clean
install:           ## dev tools (the program itself needs nothing)
	$(PY) -m pip install -e ".[dev]"
lint:
	ruff check .
typecheck:
	mypy
test:
	$(PY) -m pytest
cov:               ## tests with a coverage floor
	$(PY) -m pytest --cov --cov-fail-under=90
eval-selftest:     ## checks the eval tasks themselves (no model needed)
	$(PY) -m evals.run --selftest
check: lint typecheck cov eval-selftest
clean:
	rm -rf build dist *.egg-info .coverage .mypy_cache .ruff_cache .pytest_cache evals/results
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
