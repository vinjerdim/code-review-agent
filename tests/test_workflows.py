"""Structural guarantees for the GitHub Actions workflows."""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(wf: dict) -> dict:
    # PyYAML (YAML 1.1) parses the bare key `on` as the boolean True.
    return wf.get("on", wf.get(True))


@pytest.fixture(scope="module")
def review() -> dict:
    return load("review.yml")


@pytest.fixture(scope="module")
def review_text() -> str:
    return (WORKFLOWS / "review.yml").read_text(encoding="utf-8")


def test_trigger_is_pull_request_only(review):
    on = triggers(review)
    assert set(on) == {"pull_request"}
    assert set(on["pull_request"]["types"]) == {
        "opened",
        "synchronize",
        "reopened",
        "ready_for_review",
    }


def test_never_uses_pull_request_target(review_text):
    code = [ln for ln in review_text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("pull_request_target" in ln for ln in code)


def test_minimal_permissions(review):
    assert review["permissions"] == {}
    (job,) = review["jobs"].values()
    assert job["permissions"] == {"contents": "read", "pull-requests": "write"}


def test_fork_draft_and_label_guards(review):
    cond = review["jobs"]["review"]["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in cond
    assert "!github.event.pull_request.draft" in cond
    assert "skip-ai-review" in cond


def test_missing_secret_skips_instead_of_failing(review):
    steps = review["jobs"]["review"]["steps"]
    guard = steps[0]
    assert guard["id"] == "guard"
    assert "secrets.ANTHROPIC_API_KEY" in guard["env"]["HAS_KEY"]
    assert "::notice::" in guard["run"]
    for step in steps[1:]:
        assert step["if"] == "steps.guard.outputs.run == 'true'"


def test_api_key_only_in_review_step(review):
    steps = review["jobs"]["review"]["steps"]
    holders = [s.get("name") for s in steps if "ANTHROPIC_API_KEY" in str(s.get("env", {}))]
    assert holders == ["Check for API key", "Review"]
    review_step = next(s for s in steps if s.get("name") == "Review")
    assert review_step["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    # The guard only sees a boolean, never the secret value.
    assert steps[0]["env"]["HAS_KEY"] == "${{ secrets.ANTHROPIC_API_KEY != '' }}"


def test_no_untrusted_expressions_in_run_scripts(review):
    for step in review["jobs"]["review"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")


def test_checkout_does_not_persist_credentials(review):
    checkout = next(
        s for s in review["jobs"]["review"]["steps"] if "actions/checkout" in s.get("uses", "")
    )
    assert checkout["with"]["persist-credentials"] is False


def test_never_approves(review_text):
    assert "APPROVE" not in review_text
    assert "REQUEST_CHANGES" not in review_text


def test_ci_workflow_is_read_only():
    ci = load("ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in triggers(ci)
