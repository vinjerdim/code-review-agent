# PR Review Agent

An LLM-powered code reviewer that runs as a GitHub Action and posts inline review comments on pull requests. It only ever comments and never approves or requests changes.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you)
- `git` and `make`
- An Anthropic API key (`ANTHROPIC_API_KEY`)
- A GitHub token with pull-request read/write access (`GITHUB_TOKEN`) for local runs

## Setup

```sh
make install      # uv sync: creates .venv and installs dependencies
make test         # unit tests (no network)
make lint         # ruff + actionlint
```

## Review a PR locally

```sh
export ANTHROPIC_API_KEY=...
export GITHUB_TOKEN=...

# Preview: prints the planned comments as JSON, posts nothing
make review PR=123 REPO=owner/name ARGS=--dry-run

# Post the review to the PR
make review PR=123 REPO=owner/name
```

A commit that already has a review from the agent is skipped, so re-running costs nothing.

### Modes

- `single` (default): one model call over the filtered diff.
- `agentic`: the model can use read-only tools (`read_file`, `grep`, `git_blame`, `list_tests`) to check suspected issues before reporting them. It runs in a bounded loop. Run it from a checkout of the PR's head commit so the tools read the right code.

```sh
make review PR=123 REPO=owner/name ARGS="--mode agentic --dry-run"
```

## Run it on every PR (GitHub Action)

The workflow is in [.github/workflows/review.yml](.github/workflows/review.yml). To enable it:

1. Add the `ANTHROPIC_API_KEY` repository secret (Settings → Secrets and variables → Actions).
2. Optionally set repository **variables** to change the defaults. See [Configuration](#configuration).
3. Open a PR. The agent posts one review per commit.

It runs on non-draft PRs from branches in this repository. PRs from forks don't receive secrets, so they're skipped. If the secret is missing, the job prints a notice and exits without failing. To skip the review on a PR, add the `skip-ai-review` label.

## Configuration

All settings are environment variables locally, or repository variables in the Action. Leave them unset to use the defaults.

| Variable | Default | Meaning |
|---|---|---|
| `REVIEWER_MODEL` | `claude-opus-5-5` | Model to use |
| `REVIEWER_MODE` | `single` | `single` or `agentic` (the `--mode` flag overrides it) |
| `REVIEWER_EFFORT` | `high` | `low`, `medium`, `high`, `xhigh` or `max` |
| `REVIEWER_MIN_CONFIDENCE` | `0.7` | Findings below this confidence are dropped |
| `REVIEWER_MAX_COMMENTS` | `8` | Maximum inline comments per PR |
| `REVIEWER_MAX_STEPS` | `8` | Agentic mode: maximum number of tool-using turns |
| `REVIEWER_MAX_AGENT_TOKENS` | `300000` | Agentic mode: token budget before the final answer is forced |
| `REVIEWER_FETCH_DEPTH` | `1` | Action only: checkout depth (`0` = full history, for useful `git_blame`) |

Lockfiles, generated files, vendored code and secrets (`.env`, `*.pem`, keys) are never sent to the model.

## Evals

Eval cases in [evals/cases/](evals/cases/) are small repo snapshots plus the findings a good review should post. Run the suite after any change to the prompt, model, or tools:

```sh
make eval                                   # all cases, default settings
make eval ARGS="--mode agentic --repeat 3"  # agentic mode, 3 runs per case
make eval-report ARGS="--latest 2"          # compare the two newest runs
```

Each run reports recall, precision, false-positive rate, cost and latency. Results are saved to `evals/results/<timestamp>_<mode>_<model>.json`. Pass one file to `make eval-report` to summarise it, or two files to compare them. Evals call the real API and cost money.

To add a case, drop a JSON file in `evals/cases/` (see the existing ones for the format) and run `make test`. A test checks that every expected finding falls on a line a comment can be posted on.

## Project layout

```
src/reviewer/   context.py (filtering, prompt), agent.py (model calls), tools.py (read-only tools),
                schema.py (finding schema), postprocess.py (filters, diff positions), github_io.py, main.py
evals/          cases/, run_evals.py, report.py, pricing.json
tests/          unit tests
.github/        ci.yml (lint + tests), review.yml (the reviewer)
```
