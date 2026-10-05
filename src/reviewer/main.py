"""CLI entry point: review one pull request."""

import argparse
import json
import logging
import sys
from dataclasses import asdict, replace
from typing import Any

from reviewer import github_io
from reviewer.agent import ReviewError, ReviewOutcome, review
from reviewer.config import Settings
from reviewer.context import ReviewContext, build_context
from reviewer.postprocess import REVIEW_MARKER, PostPlan, plan_review

log = logging.getLogger("reviewer")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reviewer", description=__doc__)
    parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    parser.add_argument("--repo", required=True, help="Repository as owner/name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned review as JSON instead of posting it",
    )
    parser.add_argument(
        "--mode",
        choices=["single", "agentic"],
        help="single: one model call; agentic: read-only tool loop (default: REVIEWER_MODE)",
    )
    return parser


def make_anthropic() -> Any:
    import anthropic

    return anthropic.Anthropic()


def report(ctx: ReviewContext, outcome: ReviewOutcome, plan: PostPlan, mode: str) -> dict:
    return {
        "repo": ctx.pr.repo,
        "pr": ctx.pr.number,
        "head_sha": ctx.pr.head_sha,
        "model": outcome.model,
        "stop_reason": outcome.stop_reason,
        "refusal": outcome.refusal,
        "mode": mode,
        "attempts": outcome.attempts,
        "steps": outcome.steps,
        "tool_calls": [asdict(t) for t in outcome.tool_calls],
        "usage": asdict(outcome.usage),
        "reviewed_files": [f.filename for f in ctx.files],
        "skipped_files": [asdict(s) for s in ctx.skipped],
        "summary": outcome.result.summary,
        "comments": [{**c.finding.model_dump(), "position": c.position} for c in plan.comments],
        "dropped": [{**d.finding.model_dump(), "reason": d.reason} for d in plan.dropped],
        "review_body": plan.body,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = Settings.from_env()
    if args.mode:
        settings = replace(settings, mode=args.mode)
    if not settings.github_token:
        print("reviewer: GITHUB_TOKEN is not set", file=sys.stderr)
        return 2

    gh = github_io.make_github(settings.github_token)
    pull = github_io.get_pull(gh, args.repo, args.pr)
    pr = github_io.pr_data_from_pull(pull, args.repo, args.pr)

    # Check before calling the model so re-runs on the same commit cost nothing.
    if not args.dry_run and github_io.has_existing_review(pull, pr.head_sha, REVIEW_MARKER):
        log.info("already reviewed %s; skipping", pr.head_sha)
        return 0

    ctx = build_context(pr, settings)
    try:
        outcome = review(ctx, settings, make_anthropic())
    except ReviewError as exc:
        print(f"reviewer: {exc}", file=sys.stderr)
        return 1

    plan = plan_review(outcome.result, ctx, settings, outcome.model)
    if args.dry_run:
        print(json.dumps(report(ctx, outcome, plan, settings.mode), indent=2, default=str))
        return 0

    github_io.post_review(pull, pr.head_sha, plan.body, [c.to_github() for c in plan.comments])
    log.info(
        "posted review with %d inline comment(s) [%s mode, %d tool calls]",
        len(plan.comments),
        settings.mode,
        len(outcome.tool_calls),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
