"""CLI entry point: review one pull request."""

import argparse
import json
import logging
import sys
from dataclasses import asdict

from reviewer.agent import ReviewError, ReviewOutcome, review_single_pass
from reviewer.config import Settings
from reviewer.context import ReviewContext, build_context


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reviewer", description=__doc__)
    parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    parser.add_argument("--repo", required=True, help="Repository as owner/name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print findings as JSON instead of posting (the only mode until posting lands)",
    )
    return parser


def report(ctx: ReviewContext, outcome: ReviewOutcome) -> dict:
    return {
        "repo": ctx.pr.repo,
        "pr": ctx.pr.number,
        "head_sha": ctx.pr.head_sha,
        "model": outcome.model,
        "stop_reason": outcome.stop_reason,
        "refusal": outcome.refusal,
        "attempts": outcome.attempts,
        "usage": asdict(outcome.usage),
        "reviewed_files": [f.filename for f in ctx.files],
        "skipped_files": [asdict(s) for s in ctx.skipped],
        **outcome.result.model_dump(),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = Settings.from_env()
    if not settings.github_token:
        print("reviewer: GITHUB_TOKEN is not set", file=sys.stderr)
        return 2

    # Imported lazily so `--help` and argument errors need no SDK setup.
    import anthropic

    from reviewer.github_io import fetch_pr, make_github

    pr = fetch_pr(make_github(settings.github_token), args.repo, args.pr)
    ctx = build_context(pr, settings)
    try:
        outcome = review_single_pass(ctx, settings, anthropic.Anthropic())
    except ReviewError as exc:
        print(f"reviewer: {exc}", file=sys.stderr)
        return 1

    if not args.dry_run:
        print("reviewer: posting is not implemented yet; printing findings", file=sys.stderr)
    print(json.dumps(report(ctx, outcome), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
