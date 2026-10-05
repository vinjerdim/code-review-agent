import json
from types import SimpleNamespace as NS

import pytest
from evals import report
from evals.run_evals import (
    CaseResult,
    EvalCase,
    Expected,
    aggregate,
    cost_usd,
    load_cases,
    load_pricing,
    match,
    materialize,
    run_evals,
    save_run,
)

from reviewer.config import Settings
from reviewer.context import build_context
from reviewer.postprocess import diff_positions
from reviewer.schema import Finding

ALL_CASES = load_cases()


def finding(file="a.py", line=10, category="bug", severity="major", confidence=0.9):
    return Finding(
        file=file,
        line=line,
        category=category,
        severity=severity,
        comment=f"issue at {line}",
        confidence=confidence,
    )


# --- case validity ---------------------------------------------------------


def test_seed_cases_load_and_cover_clean_and_buggy():
    assert len(ALL_CASES) >= 8
    assert any(not c.expected for c in ALL_CASES)
    assert any(c.expected for c in ALL_CASES)


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.id)
def test_every_expected_finding_is_postable(case, tmp_path):
    """An expected line outside the reviewed diff could never be matched."""
    ctx = build_context(materialize(case, tmp_path), Settings())
    positions = {f.filename: diff_positions(f.patch) for f in ctx.files}
    for e in case.expected:
        assert e.line in positions.get(e.file, {}), f"{e.file}:{e.line} not in diff"


# --- materialize / diff parsing ------------------------------------------------


def test_materialize_statuses_and_patches(tmp_path):
    case = EvalCase(
        id="mix",
        pr={"title": "t"},
        base={"keep.py": ["a", "b"], "gone.py": ["x"], "old.py": ["1", "2", "3", "4"]},
        renames={"old.py": "pkg/new.py"},
        head={
            "keep.py": ["a", "B"],
            "gone.py": None,
            "add.py": ["n"],
            "pkg/new.py": ["1", "2", "3", "four"],
        },  # fmt: skip
    )
    pr = materialize(case, tmp_path / "repo")
    by_name = {f.filename: f for f in pr.files}
    assert by_name["keep.py"].status == "modified"
    assert by_name["keep.py"].patch.startswith("@@ -1,2 +1,2 @@")
    assert (by_name["keep.py"].additions, by_name["keep.py"].deletions) == (1, 1)
    assert by_name["gone.py"].status == "removed"
    assert by_name["add.py"].status == "added"
    renamed = by_name["pkg/new.py"]
    assert (renamed.status, renamed.previous_filename) == ("renamed", "old.py")
    assert "+four" in renamed.patch
    # Head is checked out, so agentic tools see the PR's version.
    assert (tmp_path / "repo" / "pkg" / "new.py").read_text().endswith("four\n")


def test_case_schema_rejects_unknown_fields():
    with pytest.raises(ValueError):
        EvalCase.model_validate({"id": "x", "pr": {"title": "t"}, "expect": []})


def test_category_accepts_string_or_list():
    assert Expected(file="a", line=1, category="bug").category == ["bug"]
    assert Expected(file="a", line=1, category=["bug", "security"]).category == ["bug", "security"]


# --- matching + metrics ---------------------------------------------------------


def test_match_tolerance_category_severity():
    exp = [Expected(file="a.py", line=10, tolerance=2, category="security", min_severity="major")]
    assert match(exp, [finding(line=12, category="security")]) == [(0, 0)]
    assert match(exp, [finding(line=13, category="security")]) == []
    assert match(exp, [finding(category="bug")]) == []
    assert match(exp, [finding(category="security", severity="minor")]) == []
    assert match(exp, [finding(file="b.py", category="security")]) == []


def test_match_is_one_to_one_and_prefers_closest():
    exp = [Expected(file="a.py", line=10), Expected(file="a.py", line=11)]
    posted = [finding(line=13), finding(line=10)]
    pairs = match(exp, posted)
    assert pairs == [(0, 1), (1, 0)]
    assert match(exp, [finding(line=10)]) == [(0, 0)]  # one comment can't satisfy two


def test_cost_usd():
    pricing = {"m": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.2}}
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 100_000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 500_000,
    }
    assert cost_usd(usage, "m", pricing) == pytest.approx(4.0 + 2.0 + 0.1)
    assert cost_usd(usage, "unknown", pricing) is None


def test_pricing_file_covers_default_model():
    assert Settings().model in load_pricing()


def test_aggregate_metrics():
    results = [
        CaseResult("bug1", 0, expected=2, matched=1, posted=2, false_positives=1,
                   cost_usd=0.01, latency_s=2.0),
        CaseResult("bug2", 0, expected=1, matched=1, posted=1, cost_usd=0.03, latency_s=4.0),
        CaseResult("clean1", 0, expected=0, posted=1, false_positives=1, latency_s=1.0),
        CaseResult("clean2", 0, expected=0, posted=0, latency_s=3.0),
        CaseResult("err", 0, expected=1, error="boom"),
    ]  # fmt: skip
    m = aggregate(results)
    assert m["recall"] == pytest.approx(2 / 4)
    assert m["precision"] == pytest.approx(2 / 4)
    assert m["false_positive_rate"] == pytest.approx(1 / 2)
    assert m["false_positives_per_case"] == pytest.approx(2 / 5)
    assert m["pass_rate"] == pytest.approx(2 / 5)  # bug2, clean2
    assert m["errors"] == 1
    assert m["cost_usd_total"] == pytest.approx(0.04)
    assert m["cost_unpriced_runs"] == 3
    assert m["latency_s_p50"] == 2.0
    assert m["latency_s_p95"] == 4.0


def test_aggregate_handles_no_posts():
    m = aggregate([CaseResult("c", 0)])
    assert m["precision"] is None
    assert m["recall"] is None


# --- end to end with a fake model ------------------------------------------------


class FakeClient:
    """Returns one finding per call at a fixed location (plus usage)."""

    def __init__(self, findings):
        self.body = json.dumps({"summary": "s", "findings": findings})
        self.calls = 0
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        return NS(
            content=[NS(type="text", text=self.body)],
            stop_reason="end_turn",
            stop_details=None,
            model=kwargs["model"],
            usage=NS(
                input_tokens=1000,
                output_tokens=200,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


def by_id(case_id):
    return next(c for c in ALL_CASES if c.id == case_id)


def test_end_to_end_run_scores_and_persists(tmp_path):
    hit = {
        "file": "src/users.py",
        "line": 5,
        "severity": "critical",
        "category": "security",
        "comment": "SQL injection via f-string.",
        "confidence": 0.95,
    }
    cases = [by_id("sql_injection"), by_id("clean_refactor"), by_id("lockfile_only")]
    client = FakeClient([hit])
    run = run_evals(cases, Settings(), client, repeat=2, concurrency=2)

    m = run["metrics"]
    assert m["runs"] == 6
    assert m["recall"] == 1.0
    # clean_refactor receives the same canned finding, but src/users.py is not in its
    # diff, so post-processing drops it as not_in_diff and nothing is posted.
    assert m["false_positive_rate"] == 0.0
    assert m["pass_rate"] == 1.0
    # lockfile_only is filtered before the model: 2 cases x 2 repeats call the model.
    assert client.calls == 4
    assert m["cost_usd_total"] == pytest.approx(4 * (1000 * 4 + 200 * 20) / 1e6)
    assert run["fingerprint"]["prompt_hash"]
    assert "github_token" not in run["settings"]

    path = save_run(run, tmp_path)
    loaded = report.load_run(path)
    text = report.summarize(loaded)
    assert "recall" in text and "sql_injection" in text


def test_missed_and_false_positive_are_reported(tmp_path):
    off_target = {
        "file": "src/stats.py",
        "line": 4,
        "severity": "minor",
        "category": "style",
        "comment": "Consider statistics.fmean.",
        "confidence": 0.9,
    }
    run = run_evals([by_id("clean_refactor")], Settings(), FakeClient([off_target]))
    (result,) = run["results"]
    assert result["false_positives"] == 1
    assert result["forbidden_hits"] == 1
    assert result["passed"] is False
    assert run["metrics"]["false_positive_rate"] == 1.0


def test_api_errors_are_recorded_not_raised():
    import anthropic

    def create(**_):
        raise anthropic.APIConnectionError(request=NS(method="POST", url="x"))

    boom = NS(beta=NS(messages=NS(create=create)))
    run = run_evals([by_id("sql_injection")], Settings(), boom)
    (result,) = run["results"]
    assert result["error"].startswith("APIConnectionError")
    assert result["missed"] == ["src/users.py:5 SQL injection"]


def test_compare_reports_deltas_and_flips(tmp_path):
    hit = {
        "file": "src/users.py",
        "line": 5,
        "severity": "critical",
        "category": "security",
        "comment": "SQL injection.",
        "confidence": 0.95,
    }
    base = run_evals([by_id("sql_injection")], Settings(), FakeClient([]))
    new = run_evals([by_id("sql_injection")], Settings(), FakeClient([hit]))
    text = report.compare(base, new)
    assert "sql_injection: FIXED pass 0% -> 100%" in text
    assert "+100.0pp (better)" in text


def test_report_cli_latest(tmp_path, monkeypatch, capsys):
    run = run_evals([by_id("lockfile_only")], Settings(), FakeClient([]))
    save_run(run, tmp_path)
    monkeypatch.setattr(report, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(
        report, "latest_runs", lambda n, results_dir=tmp_path: sorted(tmp_path.glob("*.json"))
    )
    assert report.main(["--latest", "1"]) == 0
    assert "lockfile_only" in capsys.readouterr().out
