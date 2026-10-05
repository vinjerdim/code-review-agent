.PHONY: install test lint fmt eval review

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

eval:
	uv run python evals/run_evals.py

review:
ifndef PR
	$(error PR is required: make review PR=<number> REPO=<owner/name>)
endif
ifndef REPO
	$(error REPO is required: make review PR=<number> REPO=<owner/name>)
endif
	uv run reviewer --pr $(PR) --repo $(REPO)
