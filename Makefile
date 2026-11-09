PYTHON ?= python3

.PHONY: dev test lint format demo chaos example build

dev:
	$(PYTHON) -m pip install -e '.[dev]'

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check src tests examples
	$(PYTHON) -m ruff format --check src tests examples

format:
	$(PYTHON) -m ruff check --fix src tests examples
	$(PYTHON) -m ruff format src tests examples

demo:
	$(PYTHON) -m fairwitness demo

chaos:
	$(PYTHON) -m fairwitness chaos --runs 200 --seed 0

example:
	$(PYTHON) -m examples.inventory_reservation

build:
	$(PYTHON) -m build
