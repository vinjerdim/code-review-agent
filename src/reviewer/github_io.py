"""GitHub I/O via PyGithub: fetch PR data and post reviews (event=COMMENT only).

Fetching converts PyGithub objects into plain dataclasses so the rest of the
pipeline (and its tests) never touches the network.
"""

from dataclasses import dataclass, field
from typing import Any

from github import Auth, Github

# The agent never approves or requests changes. Not a parameter on purpose.
REVIEW_EVENT = "COMMENT"


@dataclass(frozen=True)
class PRFile:
    filename: str
    status: str  # added | modified | removed | renamed | copied | changed | unchanged
    patch: str | None  # None for binary files or diffs GitHub declines to render
    additions: int = 0
    deletions: int = 0
    previous_filename: str | None = None


@dataclass(frozen=True)
class PRData:
    repo: str
    number: int
    title: str
    body: str
    author: str
    base_sha: str
    head_sha: str
    files: list[PRFile] = field(default_factory=list)


def make_github(token: str) -> Github:
    return Github(auth=Auth.Token(token))


def get_pull(gh: Github, repo: str, number: int) -> Any:
    return gh.get_repo(repo).get_pull(number)


def pr_data_from_pull(pull: Any, repo: str, number: int) -> PRData:
    files = [
        PRFile(
            filename=f.filename,
            status=f.status,
            patch=f.patch,
            additions=f.additions,
            deletions=f.deletions,
            previous_filename=f.previous_filename,
        )
        for f in pull.get_files()
    ]
    return PRData(
        repo=repo,
        number=number,
        title=pull.title or "",
        body=pull.body or "",
        author=pull.user.login if pull.user else "",
        base_sha=pull.base.sha,
        head_sha=pull.head.sha,
        files=files,
    )


def fetch_pr(gh: Github, repo: str, number: int) -> PRData:
    return pr_data_from_pull(get_pull(gh, repo, number), repo, number)


def has_existing_review(pull: Any, head_sha: str, marker: str) -> bool:
    """True if this agent already reviewed `head_sha` (identified by `marker`)."""
    return any(r.commit_id == head_sha and marker in (r.body or "") for r in pull.get_reviews())


def post_review(pull: Any, head_sha: str, body: str, comments: list[dict]) -> Any:
    """Post one review pinned to `head_sha`, so diff positions match what we reviewed."""
    commit = pull.base.repo.get_commit(head_sha)
    return pull.create_review(commit=commit, body=body, event=REVIEW_EVENT, comments=comments)
