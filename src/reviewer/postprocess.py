"""Post-processing: confidence filter, dedupe, comment cap, diff position mapping.

Turns validated findings into the exact inline comments we will post.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from reviewer.config import Settings
from reviewer.context import ReviewContext, iter_patch_lines
from reviewer.schema import Finding, ReviewResult

REVIEW_MARKER = "<!-- pr-review-agent -->"
SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2, "nit": 3}

_MENTION_RE = re.compile(r"@(?=[A-Za-z0-9-])")
_BACKTICK_RUN_RE = re.compile(r"`+")


@dataclass(frozen=True)
class PlannedComment:
    finding: Finding
    position: int
    body: str

    def to_github(self) -> dict:
        return {"path": self.finding.file, "position": self.position, "body": self.body}


@dataclass(frozen=True)
class DroppedFinding:
    finding: Finding
    reason: str  # low_confidence | not_in_diff | duplicate | over_cap


@dataclass
class PostPlan:
    body: str = ""
    comments: list[PlannedComment] = field(default_factory=list)
    dropped: list[DroppedFinding] = field(default_factory=list)


def diff_positions(patch: str | None) -> dict[int, int]:
    """Map new-file line number -> GitHub diff position for commentable lines."""
    if not patch:
        return {}
    return {
        pl.new_line: pl.position
        for pl in iter_patch_lines(patch)
        if pl.new_line is not None and pl.position is not None
    }


def _sort_key(f: Finding) -> tuple:
    return (SEVERITY_RANK[f.severity], -f.confidence, f.file, f.line)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def plan_review(
    result: ReviewResult, ctx: ReviewContext, settings: Settings, model: str = ""
) -> PostPlan:
    positions = {f.filename: diff_positions(f.patch) for f in ctx.files}
    plan = PostPlan()

    # Sorting first means the strongest finding wins every duplicate collision.
    seen_keys: set[tuple] = set()
    seen_text: set[str] = set()
    kept: list[tuple[Finding, int]] = []
    for f in sorted(result.findings, key=_sort_key):
        position = positions.get(f.file, {}).get(f.line)
        key, text = (f.file, f.line, f.category), _normalize(f.comment)
        if f.confidence < settings.min_confidence:
            reason = "low_confidence"
        elif position is None:
            reason = "not_in_diff"
        elif key in seen_keys or text in seen_text:
            reason = "duplicate"
        elif len(kept) >= settings.max_comments:
            reason = "over_cap"
        else:
            seen_keys.add(key)
            seen_text.add(text)
            kept.append((f, position))
            continue
        plan.dropped.append(DroppedFinding(f, reason))

    plan.comments = [PlannedComment(f, pos, render_comment(f)) for f, pos in kept]
    plan.body = render_summary(result, ctx, plan, model)
    return plan


def _defang(text: str) -> str:
    """Stop model-echoed text from pinging users via @mentions."""
    return _MENTION_RE.sub("@​", text)


def _fence(code: str) -> str:
    longest = max((len(m) for m in _BACKTICK_RUN_RE.findall(code)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{code.rstrip()}\n{ticks}"


def render_comment(f: Finding) -> str:
    parts = [
        f"**[{f.severity}] {f.category}** (confidence {f.confidence:.2f})",
        "",
        _defang(f.comment.strip()),
    ]
    if f.suggested_fix and f.suggested_fix.strip():
        parts += ["", "Suggested fix:", _fence(f.suggested_fix)]
    return "\n".join(parts)


def render_summary(result: ReviewResult, ctx: ReviewContext, plan: PostPlan, model: str) -> str:
    lines = [
        REVIEW_MARKER,
        "### Automated review",
        "",
        _defang(result.summary.strip()) or "_No summary._",
        "",
        f"Posted {len(plan.comments)} inline comment(s).",
    ]
    if plan.dropped:
        counts = Counter(d.reason for d in plan.dropped)
        detail = ", ".join(f"{n} {reason}" for reason, n in sorted(counts.items()))
        lines.append(f"Dropped {len(plan.dropped)} finding(s): {detail}.")
    if ctx.skipped:
        lines += ["", "<details><summary>Files not reviewed</summary>", ""]
        lines += [f"- `{s.filename}` ({s.reason})" for s in ctx.skipped]
        lines += ["", "</details>"]
    lines += ["", f"<sub>Model: {model or 'unknown'}. Comments only; never approves.</sub>"]
    return "\n".join(lines)
