"""Summarize and compare saved eval runs.

python -m evals.report RUN.json              # summarize one run
python -m evals.report BASELINE.json NEW.json  # compare two runs
python -m evals.report --latest 2            # compare the two newest runs
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from evals.run_evals import RESULTS_DIR

# (key, label, kind, higher_is_better)
METRICS = [
    ("pass_rate", "pass rate", "pct", True),
    ("recall", "recall", "pct", True),
    ("precision", "precision", "pct", True),
    ("false_positive_rate", "FP rate (clean cases)", "pct", False),
    ("false_positives_per_case", "FPs / case", "num", False),
    ("forbidden_hits", "must-not-flag hits", "int", False),
    ("errors", "errors", "int", False),
    ("cost_usd_total", "cost total", "usd", False),
    ("cost_usd_per_case", "cost / case", "usd", False),
    ("latency_s_p50", "latency p50", "sec", False),
    ("latency_s_p95", "latency p95", "sec", False),
    ("tool_calls_per_case", "tool calls / case", "num", None),
]


def load_run(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fmt(value: Any, kind: str) -> str:
    if value is None:
        return "n/a"
    template = {"pct": "{:.1%}", "usd": "${:.4f}", "sec": "{:.1f}s", "num": "{:.2f}", "int": "{:d}"}
    return template[kind].format(int(value) if kind == "int" else value)


def _delta(old: Any, new: Any, kind: str, higher_better: bool | None) -> str:
    if old is None or new is None:
        return ""
    diff = new - old
    if diff == 0:
        return "="
    text = f"{diff * 100:+.1f}pp" if kind == "pct" else f"{diff:+.4g}"
    if higher_better is None:
        return text
    return f"{text} ({'better' if (diff > 0) == higher_better else 'worse'})"


def _header(run: dict[str, Any]) -> str:
    s, fp = run["settings"], run["fingerprint"]
    dirty = " (dirty)" if fp.get("git_dirty") else ""
    return (
        f"{run['run_id']}\n"
        f"  mode={s['mode']} model={s['model']} effort={s['effort']} "
        f"min_conf={s['min_confidence']} max_comments={s['max_comments']}\n"
        f"  prompt_hash={fp['prompt_hash']} git={str(fp.get('git_sha'))[:10]}{dirty} "
        f"cases={len(run['cases'])} repeat={run['repeat']}"
    )


def _per_case(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Collapse repeats: pass fraction and mean recall per case."""
    out: dict[str, dict[str, Any]] = {}
    for r in run["results"]:
        empty = {"runs": 0, "passed": 0, "matched": 0, "expected": 0, "fp": 0, "errors": 0}
        c = out.setdefault(r["case_id"], {**empty, "missed": set()})
        c["runs"] += 1
        c["passed"] += int(r["passed"])
        c["matched"] += r["matched"]
        c["expected"] += r["expected"]
        c["fp"] += r["false_positives"]
        c["errors"] += int(bool(r["error"]))
        c["missed"].update(r["missed"])
    return out


def summarize(run: dict[str, Any]) -> str:
    m = run["metrics"]
    lines = [_header(run), ""]
    width = max(len(label) for _, label, _, _ in METRICS)
    for key, label, kind, _ in METRICS:
        lines.append(f"  {label:<{width}}  {_fmt(m.get(key), kind)}")
    lines += ["", "  case                             pass   found  FPs  notes"]
    for case_id, c in sorted(_per_case(run).items()):
        notes = []
        if c["errors"]:
            notes.append(f"{c['errors']} error(s)")
        if c["missed"]:
            notes.append("missed " + ", ".join(sorted(c["missed"])))
        lines.append(
            f"  {case_id:<32} {c['passed']}/{c['runs']:<4} "
            f"{c['matched']:>2}/{c['expected']:<3} {c['fp']:>4}  {'; '.join(notes)}"
        )
    return "\n".join(lines)


def compare(base: dict[str, Any], new: dict[str, Any]) -> str:
    lines = ["BASELINE " + _header(base), "NEW      " + _header(new), ""]
    if base["fingerprint"]["prompt_hash"] != new["fingerprint"]["prompt_hash"]:
        lines.append("  note: prompt/tools/schema differ between runs\n")
    width = max(len(label) for _, label, _, _ in METRICS)
    lines.append(f"  {'metric':<{width}}  {'baseline':>10}  {'new':>10}  delta")
    for key, label, kind, better in METRICS:
        old_v, new_v = base["metrics"].get(key), new["metrics"].get(key)
        lines.append(
            f"  {label:<{width}}  {_fmt(old_v, kind):>10}  {_fmt(new_v, kind):>10}  "
            f"{_delta(old_v, new_v, kind, better)}"
        )

    old_cases, new_cases = _per_case(base), _per_case(new)
    changes = []
    for case_id in sorted(set(old_cases) | set(new_cases)):
        o, n = old_cases.get(case_id), new_cases.get(case_id)
        if o is None or n is None:
            changes.append(f"  {case_id}: only in {'new' if o is None else 'baseline'}")
            continue
        o_rate, n_rate = o["passed"] / o["runs"], n["passed"] / n["runs"]
        if o_rate != n_rate:
            verdict = "FIXED" if n_rate > o_rate else "REGRESSED"
            changes.append(f"  {case_id}: {verdict} pass {o_rate:.0%} -> {n_rate:.0%}")
    lines += ["", "Case changes:"] + (changes or ["  none"])
    return "\n".join(lines)


def latest_runs(n: int, results_dir: Path = RESULTS_DIR) -> list[Path]:
    runs = sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if len(runs) < n:
        raise SystemExit(f"need {n} run(s) in {results_dir}, found {len(runs)}")
    return runs[-n:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="report", description=__doc__.splitlines()[0])
    parser.add_argument(
        "runs", nargs="*", type=Path, help="One run to summarize, or two to compare"
    )
    parser.add_argument("--latest", type=int, choices=[1, 2], help="Use the newest N runs")
    args = parser.parse_args(argv)

    paths = latest_runs(args.latest) if args.latest else args.runs
    if len(paths) == 1:
        print(summarize(load_run(paths[0])))
    elif len(paths) == 2:
        print(compare(load_run(paths[0]), load_run(paths[1])))
    else:
        parser.error("give one run, two runs, or --latest 1|2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
