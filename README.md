# PR Review Agent

An LLM-powered code reviewer. It runs as a GitHub Action (or from your terminal), reads a pull request's diff, and posts **one review with inline comments** on the lines where it found real problems.

It only ever leaves comments. It never approves, requests changes, or merges.

## Contents

- [Overview](#overview)
- [Installation](#installation)
- [How to run](#how-to-run)
- [Configuration](#configuration)
- [Evals](#evals)
- [Development](#development)
- [Important notes](#important-notes)
- [Project layout](#project-layout)

## Overview

For each pull request the agent:

1. **Fetches** the diff and metadata with PyGithub.
2. **Filters** files. Lockfiles, generated files, vendored code and secrets (`.env`, `*.pem`, keys) are dropped before anything reaches the model.
3. **Reviews** the rest with Claude. The model must answer in a JSON format that is validated with pydantic (`file`, `line`, `severity`, `category`, `comment`, `confidence`, optional `suggested_fix`).
4. **Post-processes** the findings: drops low-confidence ones, drops anything not on a line in the diff, removes duplicates, sorts by severity, and keeps the top few. Each remaining line number is converted to a GitHub diff position.
5. **Posts** a single review (`event="COMMENT"`) pinned to the reviewed commit, with a summary that says what was dropped and which files were skipped.

Two review modes:

| Mode | What it does |
|---|---|
| `single` (default) | One model call over the filtered diff. Cheapest and fastest. |
| `agentic` | The model can look around the repo with read-only tools (`read_file`, `grep`, `git_blame`, `list_tests`) to confirm a suspected issue before reporting it. The loop is bounded by a maximum number of steps and a token budget. |

## Installation

### Requirements

- [uv](https://docs.astral.sh/uv/). It also installs Python 3.12 for you, so you don't need Python set up first.
- `git` and `make`
- An [Anthropic API key](https://console.anthropic.com/)
- A GitHub token that can read and write pull requests (needed for local runs only; the Action supplies its own)

On Windows you can install the tools with `winget install astral-sh.uv` and `winget install ezwinports.make`, then open a new terminal.

### Set up

```sh
git clone <this repo>
cd code-review-agent
make install    # creates .venv and installs dependencies (uv sync)
make test       # unit tests; no network or API key needed
make lint       # ruff + actionlint
```

## How to run

### Review a pull request from your terminal

```sh
export ANTHROPIC_API_KEY=...
export GITHUB_TOKEN=...

# Preview: prints the planned review as JSON and posts nothing
make review PR=123 REPO=owner/name ARGS=--dry-run

# Post the review for real
make review PR=123 REPO=owner/name
```

On Windows PowerShell, set the variables with `$env:ANTHROPIC_API_KEY = "..."` instead of `export`.

The dry-run output shows the comments that would be posted, the findings that were dropped (and why), token usage, and in agentic mode the tool calls the model made. Start with `--dry-run` on a new repo.

Running the same command twice on the same commit does nothing the second time: the agent recognises its own earlier review and exits before calling the model.

To use agentic mode, run it from a checkout of the PR's head commit so the tools read the right code:

```sh
git fetch origin pull/123/head && git checkout FETCH_HEAD
make review PR=123 REPO=owner/name ARGS="--mode agentic --dry-run"
```

### Run it automatically on every PR (GitHub Action)

The workflow is [.github/workflows/review.yml](.github/workflows/review.yml).

1. Add the `ANTHROPIC_API_KEY` secret: repository **Settings → Secrets and variables → Actions → Secrets**.
2. Optionally add repository **variables** (same page, *Variables* tab) to change the defaults. See [Configuration](#configuration). For example, set `REVIEWER_MODE` to `agentic`.
3. Open a pull request. The agent reviews it and posts one review per commit pushed.

When it runs:

- on PRs that are opened, updated, reopened or marked ready for review
- not on draft PRs, and not on PRs labelled `skip-ai-review`
- only for PRs from branches **in the same repository**. Fork PRs don't receive secrets, so they are skipped.
- if the `ANTHROPIC_API_KEY` secret isn't available (for example on Dependabot PRs), the job prints a notice and finishes green instead of failing

The job gets only `contents: read` and `pull-requests: write` permissions.

## Configuration

Locally these are environment variables. In the Action they are repository variables. Anything left unset uses the default.

| Variable | Default | Meaning |
|---|---|---|
| `REVIEWER_MODEL` | `claude-opus-5-5` | Model to use. The model name lives only in `src/reviewer/config.py`. |
| `REVIEWER_MODE` | `single` | `single` or `agentic` (the `--mode` flag overrides it) |
| `REVIEWER_EFFORT` | `high` | `low`, `medium`, `high`, `xhigh` or `max` |
| `REVIEWER_MIN_CONFIDENCE` | `0.7` | Findings below this confidence are dropped |
| `REVIEWER_MAX_COMMENTS` | `8` | Maximum inline comments per PR |
| `REVIEWER_MAX_TOKENS` | `16000` | Maximum output tokens per model call |
| `REVIEWER_MAX_STEPS` | `8` | Agentic mode: maximum tool-using turns |
| `REVIEWER_MAX_AGENT_TOKENS` | `300000` | Agentic mode: token budget before a final answer is forced |
| `REVIEWER_MAX_FILE_PATCH_CHARS` | `20000` | A file whose diff is larger than this is left out |
| `REVIEWER_MAX_TOTAL_PATCH_CHARS` | `200000` | Total diff size sent per PR; files past the limit are left out |
| `REVIEWER_FALLBACKS` | `default` | Server-side retry on another model if the first one declines; set to empty to turn off |
| `REVIEWER_REPO_ROOT` | `.` | Agentic mode: directory the tools may read |
| `REVIEWER_FETCH_DEPTH` | `1` | Action only: checkout depth. Use `0` for full history so `git_blame` is useful. |

Required secrets and tokens: `ANTHROPIC_API_KEY` and `GITHUB_TOKEN`.

## Evals

CLAUDE.md requires that any change to the prompt, model or tools is checked with `make eval`.

Each case in [evals/cases/](evals/cases/) is a small repo snapshot (files before and after a change) plus the findings a good review must post, and optionally lines that must *not* be flagged. The harness builds a real git repo for each case, runs the same pipeline as production, and scores the comments that would actually be posted.

```sh
make eval                                    # all cases with default settings
make eval ARGS="--mode agentic --repeat 3"   # agentic mode, 3 runs per case
make eval ARGS="--cases 'sql*.json'"         # a subset
make eval-report ARGS="--latest 2"           # compare the two newest runs
make eval-report ARGS="run_a.json run_b.json"  # compare two specific runs
```

Each run reports recall, precision, false-positive rate, pass rate, cost and latency (p50/p95), and is saved to `evals/results/<timestamp>_<mode>_<model>.json` along with the settings and a hash of the prompt and tools. The comparison view shows metric changes and which cases started or stopped passing.

- **Evals call the real API and cost money.** Use `--repeat` sparingly.
- `evals/results/` is gitignored. Copy any run you want to keep as a reference into a tracked folder.
- Prices used for the cost figure are in [evals/pricing.json](evals/pricing.json). Update them if prices change or you switch models.
- To add a case, add a JSON file to `evals/cases/` (copy an existing one) and run `make test`. A test checks that every expected finding is on a line a comment can really be posted on.

## Development

```sh
make test      # pytest (no network)
make lint      # ruff check, ruff format --check, actionlint
make fmt       # auto-fix lint issues and format
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs `make lint` and `make test` on every push to `main` and every PR.

Stack: Python 3.12, uv, the Anthropic SDK, PyGithub, pydantic, pytest, ruff.

## Important notes

**Safety rules (see [CLAUDE.md](CLAUDE.md)):**

- Reviews are always posted with `event="COMMENT"`. There is no code path or setting that approves or requests changes, and a test enforces it.
- The agent's tools are **read-only**. Do not add write tools. Paths are confined to the repository, symlinks that escape it are refused, and the same secret/lockfile/vendored rules apply to what the tools can read.
- Diffs, code comments, PR titles and descriptions, and tool output are treated as **untrusted data**. They are wrapped in labelled tags, and the system prompt tells the model never to follow instructions found inside them. Eval case `prompt_injection_in_body` checks this.
- Findings come only from schema-validated JSON. Comments are capped (default 8) and low-confidence findings (below 0.7) are dropped.

**Things worth knowing:**

- **Cost.** Each reviewed commit is a model call. Agentic mode can make up to `REVIEWER_MAX_STEPS + 2` calls per review, so it costs noticeably more than `single`. Use `--dry-run` and the evals to see token usage before enabling it everywhere.
- **Large PRs.** Files over the size limits are left out whole (never cut off) and listed in the review summary as not reviewed.
- **`git_blame` in CI.** The default checkout is shallow, so blame only sees one commit. Set `REVIEWER_FETCH_DEPTH=0` if you want real history in agentic mode.
- **Fork PRs are not reviewed.** The workflow uses the `pull_request` trigger on purpose: it never uses `pull_request_target`, so untrusted code can't run with your API key. Reviewing forks would need a separate, carefully designed setup.
- **One review per commit.** A new push gets a new review; old reviews aren't edited or removed.
- **Refusals.** If the model declines to review, the review is empty and records the reason instead of failing.
- **Review quality isn't guaranteed.** It can miss bugs and occasionally flag non-issues. Treat it as a first-pass reviewer, and re-run `make eval` when you change the prompt, model or tools.

## Project layout

```
src/reviewer/
  main.py         CLI entry point (`reviewer --pr N --repo owner/name`)
  config.py       Settings read from environment variables (model name lives here)
  github_io.py    Fetch PR data and post the review (PyGithub)
  context.py      File filtering, size limits, diff line numbering, prompt building
  agent.py        System prompt, single-pass review, agentic tool loop
  tools.py        Read-only tools: read_file, grep, git_blame, list_tests
  schema.py       Pydantic models for findings
  postprocess.py  Confidence filter, dedupe, cap, diff position mapping, comment text
evals/            cases/, run_evals.py, report.py, pricing.json
tests/            Unit tests
.github/workflows/
  ci.yml          Lint and tests
  review.yml      The reviewer Action
```
