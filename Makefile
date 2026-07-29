.PHONY: install test lint typecheck clean

install:
	pip install -e ".[dev]"

test:
	python -m pytest tests/ -v

test-unit:
	python -m pytest tests/unit/ -v

test-integration:
	python -m pytest tests/integration/ -v

lint:
	ruff check .

lint-fix:
	ruff check --fix .

typecheck:
	mypy patchpilot/ --ignore-missing-imports

clean:
	rm -rf *.egg-info .pytest_cache __pycache__
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete
