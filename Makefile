.PHONY: install test lint check

install:
	pip install -e .[dev]

test:
	pytest -q

lint:
	ruff check src tests

check: lint test
