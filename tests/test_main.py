import json

import pytest

from reviewer import github_io
from reviewer import main as cli
from reviewer.agent import ReviewOutcome
from reviewer.schema import Finding, ReviewResult
from tests.conftest import make_pr


@pytest.fixture
def wired(monkeypatch):
    """Stub GitHub and the model; record what would be posted."""
    state = {"posted": [], "model_calls": 0, "existing": False}
    pr = make_pr()

    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(github_io, "make_github", lambda token: object())
    monkeypatch.setattr(github_io, "get_pull", lambda gh, repo, n: "PULL")
    monkeypatch.setattr(github_io, "pr_data_from_pull", lambda pull, repo, n: pr)
    monkeypatch.setattr(
        github_io, "has_existing_review", lambda pull, sha, marker: state["existing"]
    )
    monkeypatch.setattr(
        github_io,
        "post_review",
        lambda pull, sha, body, comments: state["posted"].append((sha, body, comments)),
    )
    monkeypatch.setattr(cli, "make_anthropic", lambda: object())

    def fake_review(ctx, settings, client):
        state["model_calls"] += 1
        state["mode"] = settings.mode
        finding = Finding(
            file="src/app.py",
            line=22,
            severity="major",
            category="bug",
            comment="Doubles the value.",
            confidence=0.9,
        )
        return ReviewOutcome(
            result=ReviewResult(summary="One issue.", findings=[finding]), model="m"
        )

    monkeypatch.setattr(cli, "review", fake_review)
    return state


def test_dry_run_prints_plan_and_never_posts(wired, capsys):
    assert cli.main(["--pr", "7", "--repo", "octo/demo", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert wired["posted"] == []
    assert out["comments"][0]["position"] == 10
    assert out["review_body"].startswith("<!-- pr-review-agent -->")


def test_posts_one_review_with_inline_comments(wired):
    assert cli.main(["--pr", "7", "--repo", "octo/demo"]) == 0
    ((sha, body, comments),) = wired["posted"]
    assert sha == "head456"
    assert comments == [{"path": "src/app.py", "position": 10, "body": comments[0]["body"]}]
    assert "Doubles the value." in comments[0]["body"]


def test_already_reviewed_commit_skips_model_and_post(wired):
    wired["existing"] = True
    assert cli.main(["--pr", "7", "--repo", "octo/demo"]) == 0
    assert wired["model_calls"] == 0
    assert wired["posted"] == []


def test_mode_flag_overrides_env(wired, monkeypatch, capsys):
    monkeypatch.setenv("REVIEWER_MODE", "single")
    assert cli.main(["--pr", "7", "--repo", "octo/demo", "--dry-run", "--mode", "agentic"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert wired["mode"] == "agentic"
    assert out["mode"] == "agentic"
    assert out["tool_calls"] == []


def test_mode_defaults_to_env(wired, monkeypatch):
    monkeypatch.setenv("REVIEWER_MODE", "agentic")
    cli.main(["--pr", "7", "--repo", "octo/demo", "--dry-run"])
    assert wired["mode"] == "agentic"
