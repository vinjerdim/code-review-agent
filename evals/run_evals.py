"""Run eval cases in evals/cases/ and save results to JSON.

Each case is a small repo snapshot (base files + head changes) plus the findings a
good review must post. The harness builds a real git repo per case, runs the same
pipeline as production (build_context -> review -> plan_review), and scores what
would actually be posted.

    python -m evals.run_evals [--mode single|agentic] [--cases GLOB] [--repeat N]
"""

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from reviewer import agent
from reviewer.agent import ReviewError, review
from reviewer.config import Settings
from reviewer.context import build_context
from reviewer.github_io import PRData, PRFile
from reviewer.postprocess import SEVERITY_RANK, plan_review
from reviewer.schema import Category, Finding, Severity
from reviewer.tools import RepoTools

EVALS_DIR = Path(__file__).resolve().parent
CASES_DIR = EVALS_DIR / "cases"
RESULTS_DIR = EVALS_DIR / "results"
PRICING_PATH = EVALS_DIR / "pricing.json"

Content = str | list[str]

# --- case format -------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Expected(_Strict):
    file: str
    line: int = Field(ge=1)
    tolerance: int = Field(default=3, ge=0)
    category: list[Category] | None = None  # any of these; None = any category
    min_severity: Severity | None = None  # at least this severe
    note: str = ""

    @field_validator("category", mode="before")
    @classmethod
    def _one_or_many(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v


class NotFlag(_Strict):
    file: str
    line: int = Field(ge=1)
    tolerance: int = Field(default=0, ge=0)


class PRMeta(_Strict):
    title: str
    body: str = ""


class EvalCase(_Strict):
    id: str
    description: str = ""
    pr: PRMeta
    base: dict[str, Content] = Field(default_factory=dict)
    head: dict[str, Content | None] = Field(default_factory=dict)  # None deletes the file
    renames: dict[str, str] = Field(default_factory=dict)  # old path -> new path
    expected: list[Expected] = Field(default_factory=list)
    must_not_flag: list[NotFlag] = Field(default_factory=list)


def load_cases(pattern: str = "*.json", cases_dir: Path = CASES_DIR) -> list[EvalCase]:
    cases = [
        EvalCase.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(cases_dir.glob(pattern))
    ]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate case ids in {cases_dir}")
    return cases


# --- materialize a case as a git repo + GitHub-style PR data ---------------


def _text(content: Content) -> str:
    return "\n".join(content) + "\n" if isinstance(content, list) else content


def _write(root: Path, path: str, content: Content) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_text(content), encoding="utf-8", newline="\n")


def _git(root: Path, *args: str) -> str:
    cmd = [
        "git",
        "-C",
        str(root),
        "-c",
        "user.name=eval",
        "-c",
        "user.email=eval@example.com",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.quotepath=false",
        "-c",
        "commit.gpgsign=false",
        *args,
    ]
    return subprocess.run(cmd, check=True, capture_output=True).stdout.decode("utf-8")


def parse_git_diff(diff: str) -> list[PRFile]:
    """Split `git diff -M` output into per-file PRFile records with GitHub-style
    patches (everything from the first @@ header on)."""
    blocks: list[list[str]] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return [_parse_block(block) for block in blocks]


_HEADER_STATUS = {"new file mode": "added", "deleted file mode": "removed"}


def _parse_block(block: list[str]) -> PRFile:
    old, _, new = block[0][len("diff --git a/") :].partition(" b/")
    status = "modified"
    hunk_at = next((i for i, ln in enumerate(block) if ln.startswith("@@")), len(block))
    for line in block[1:hunk_at]:
        for prefix, header_status in _HEADER_STATUS.items():
            if line.startswith(prefix):
                status = header_status
        if line.startswith("rename from "):
            old, status = line[len("rename from ") :], "renamed"
        elif line.startswith("rename to "):
            new = line[len("rename to ") :]
    body = block[hunk_at:]
    return PRFile(
        filename=old if status == "removed" else new,
        status=status,
        patch="\n".join(body) if body else None,
        additions=sum(1 for ln in body if ln.startswith("+")),
        deletions=sum(1 for ln in body if ln.startswith("-")),
        previous_filename=old if status == "renamed" else None,
    )


def materialize(case: EvalCase, root: Path) -> PRData:
    """Create a git repo at `root` (left checked out at head) and return the PR."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for path, content in case.base.items():
        _write(root, path, content)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD").strip()

    for old, new in case.renames.items():
        (root / new).parent.mkdir(parents=True, exist_ok=True)
        _git(root, "mv", old, new)
    for path, content in case.head.items():
        if content is None:
            (root / path).unlink()
        else:
            _write(root, path, content)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", case.pr.title)
    head_sha = _git(root, "rev-parse", "HEAD").strip()

    diff = _git(root, "diff", "-M", "--no-color", "--no-ext-diff", "-U3", base_sha, head_sha)
    return PRData(
        repo=f"eval/{case.id}",
        number=1,
        title=case.pr.title,
        body=case.pr.body,
        author="eval",
        base_sha=base_sha,
        head_sha=head_sha,
        files=parse_git_diff(diff),
    )


# --- scoring -------------------------------------------------------------------


def _matches(e: Expected, f: Finding) -> bool:
    return (
        f.file == e.file
        and abs(f.line - e.line) <= e.tolerance
        and (e.category is None or f.category in e.category)
        and (e.min_severity is None or SEVERITY_RANK[f.severity] <= SEVERITY_RANK[e.min_severity])
    )


def match(expected: list[Expected], posted: list[Finding]) -> list[tuple[int, int]]:
    """One-to-one (expected_idx, posted_idx) pairs. Greedy in case order; each
    expected finding takes the closest-by-line unused posted comment."""
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for ei, e in enumerate(expected):
        candidates = [
            (abs(f.line - e.line), fi)
            for fi, f in enumerate(posted)
            if fi not in used and _matches(e, f)
        ]
        if candidates:
            _, fi = min(candidates)
            used.add(fi)
            pairs.append((ei, fi))
    return pairs


def cost_usd(usage: dict[str, int], model: str, pricing: dict[str, Any]) -> float | None:
    price = pricing.get(model)
    if price is None:
        return None
    return (
        usage.get("input_tokens", 0) * price["input"]
        + usage.get("output_tokens", 0) * price["output"]
        + usage.get("cache_creation_input_tokens", 0) * price["cache_write"]
        + usage.get("cache_read_input_tokens", 0) * price["cache_read"]
    ) / 1_000_000


@dataclass
class CaseResult:
    case_id: str
    repeat: int
    expected: int = 0
    matched: int = 0
    posted: int = 0
    false_positives: int = 0
    forbidden_hits: int = 0
    raw_findings: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    missed: list[str] = field(default_factory=list)
    posted_findings: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = None
    latency_s: float = 0.0
    model: str = ""
    steps: int = 0
    tool_calls: int = 0
    refusal: str | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.error is None
            and self.matched == self.expected
            and self.false_positives == 0
            and self.forbidden_hits == 0
        )


def score(case: EvalCase, posted: list[Finding], result: CaseResult) -> None:
    pairs = match(case.expected, posted)
    hit_expected = {ei for ei, _ in pairs}
    result.expected = len(case.expected)
    result.matched = len(pairs)
    result.posted = len(posted)
    result.false_positives = len(posted) - len(pairs)
    result.missed = [
        f"{e.file}:{e.line} {e.note}".strip()
        for i, e in enumerate(case.expected)
        if i not in hit_expected
    ]
    result.forbidden_hits = sum(
        1
        for nf in case.must_not_flag
        for f in posted
        if f.file == nf.file and abs(f.line - nf.line) <= nf.tolerance
    )


def run_case(
    case: EvalCase, settings: Settings, client: Any, pricing: dict[str, Any], repeat: int = 0
) -> CaseResult:
    import anthropic

    result = CaseResult(case_id=case.id, repeat=repeat, expected=len(case.expected))
    with tempfile.TemporaryDirectory(prefix=f"eval-{case.id}-", ignore_cleanup_errors=True) as tmp:
        pr = materialize(case, Path(tmp))
        case_settings = replace(settings, repo_root=tmp)
        ctx = build_context(pr, case_settings)
        start = time.perf_counter()
        try:
            outcome = review(ctx, case_settings, client)
        except (ReviewError, anthropic.APIError) as exc:
            result.latency_s = time.perf_counter() - start
            result.error = f"{type(exc).__name__}: {exc}"
            result.missed = [f"{e.file}:{e.line} {e.note}".strip() for e in case.expected]
            return result
        result.latency_s = time.perf_counter() - start

    plan = plan_review(outcome.result, ctx, case_settings, outcome.model)
    posted = [c.finding for c in plan.comments]
    score(case, posted, result)
    result.raw_findings = len(outcome.result.findings)
    result.dropped = dict(sorted(_count(d.reason for d in plan.dropped).items()))
    result.posted_findings = [f.model_dump() for f in posted]
    result.usage = asdict(outcome.usage)
    result.model = outcome.model
    result.cost_usd = cost_usd(result.usage, outcome.model, pricing)
    result.steps = outcome.steps
    result.tool_calls = len(outcome.tool_calls)
    result.refusal = outcome.refusal
    return result


def _count(items: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def aggregate(results: list[CaseResult]) -> dict[str, Any]:
    n = len(results)
    clean = [r for r in results if r.expected == 0 and r.error is None]
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    latencies = [r.latency_s for r in results if r.error is None]
    expected = sum(r.expected for r in results)
    matched = sum(r.matched for r in results)
    posted = sum(r.posted for r in results)
    return {
        "runs": n,
        "pass_rate": _ratio(sum(r.passed for r in results), n),
        "recall": _ratio(matched, expected),
        "precision": _ratio(matched, posted),
        # Share of bug-free cases where the agent posted anything at all.
        "false_positive_rate": _ratio(sum(1 for r in clean if r.posted), len(clean)),
        "false_positives_per_case": _ratio(sum(r.false_positives for r in results), n),
        "forbidden_hits": sum(r.forbidden_hits for r in results),
        "expected": expected,
        "matched": matched,
        "posted": posted,
        "errors": sum(1 for r in results if r.error),
        "refusals": sum(1 for r in results if r.refusal),
        "cost_usd_total": sum(costs) if costs else None,
        "cost_usd_per_case": _ratio(sum(costs), len(costs)) if costs else None,
        "cost_unpriced_runs": n - len(costs),
        "latency_s_p50": _percentile(latencies, 0.5),
        "latency_s_p95": _percentile(latencies, 0.95),
        "latency_s_mean": _ratio(sum(latencies), len(latencies)),
        "tokens": {
            k: sum(r.usage.get(k, 0) for r in results)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        },
        "tool_calls_per_case": _ratio(sum(r.tool_calls for r in results), n),
    }


# --- run + persist ---------------------------------------------------------------


def fingerprint() -> dict[str, Any]:
    """Identify exactly which prompt/tools/code produced a run."""
    material = json.dumps(
        {
            "system": agent.SYSTEM_PROMPT,
            "agentic": agent.AGENTIC_ADDENDUM,
            "repair": agent.REPAIR_PROMPT,
            "schema": agent.review_output_schema(),
            "tools": RepoTools(".").definitions(),
        },
        sort_keys=True,
    )
    repo = EVALS_DIR.parent

    def git(*args: str) -> str | None:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None

    return {
        "prompt_hash": hashlib.sha256(material.encode()).hexdigest()[:12],
        "git_sha": git("rev-parse", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
    }


def run_evals(
    cases: list[EvalCase],
    settings: Settings,
    client: Any,
    repeat: int = 1,
    concurrency: int = 4,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pricing = pricing if pricing is not None else load_pricing()
    jobs = [(case, i) for i in range(repeat) for case in cases]
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = list(
            pool.map(lambda job: run_case(job[0], settings, client, pricing, job[1]), jobs)
        )
    created = datetime.now(UTC)
    public_settings = {k: v for k, v in asdict(settings).items() if k != "github_token"}
    return {
        "run_id": f"{created:%Y%m%dT%H%M%SZ}_{settings.mode}_{settings.model}",
        "created_at": created.isoformat(),
        "settings": public_settings,
        "fingerprint": fingerprint(),
        "cases": [c.id for c in cases],
        "repeat": repeat,
        "metrics": aggregate(results),
        "results": [{**asdict(r), "passed": r.passed} for r in results],
    }


def load_pricing(path: Path = PRICING_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def save_run(run: dict[str, Any], out_dir: Path = RESULTS_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run['run_id']}.json"
    path.write_text(json.dumps(run, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_evals", description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=["single", "agentic"])
    parser.add_argument("--model", help="Override REVIEWER_MODEL for this run")
    parser.add_argument("--cases", default="*.json", help="Glob within evals/cases/")
    parser.add_argument("--repeat", type=int, default=1, help="Runs per case (variance)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.mode:
        settings = replace(settings, mode=args.mode)
    if args.model:
        settings = replace(settings, model=args.model)
    cases = load_cases(args.cases)
    if not cases:
        print(f"no cases match {args.cases!r}", file=sys.stderr)
        return 2

    import anthropic

    from evals.report import summarize

    print(
        f"running {len(cases)} case(s) x{args.repeat} [{settings.mode}, {settings.model}]",
        file=sys.stderr,
    )
    run = run_evals(cases, settings, anthropic.Anthropic(), args.repeat, args.concurrency)
    path = save_run(run, args.out)
    print(summarize(run))
    print(f"\nsaved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
