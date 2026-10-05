.PHONY: install test lint fmt eval eval-report review

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run actionlint

fmt:
	uv run ruff check --fix .
	uv run ruff format .

eval:
	uv run python -m evals.run_evals $(ARGS)

eval-report:
	uv run python -m evals.report $(ARGS)

review:
ifndef PR
	$(error PR is required: make review PR=<number> REPO=<owner/name>)
endif
ifndef REPO
	$(error REPO is required: make review PR=<number> REPO=<owner/name>)
endif
	uv run reviewer --pr $(PR) --repo $(REPO) $(ARGS)
