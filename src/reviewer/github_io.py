"""GitHub I/O via PyGithub: fetch PR data and post reviews (event=COMMENT only).

Fetching converts PyGithub objects into plain dataclasses so the rest of the
pipeline (and its tests) never touches the network.
"""

from dataclasses import dataclass, field

from github import Auth, Github


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


def fetch_pr(gh: Github, repo: str, number: int) -> PRData:
    pull = gh.get_repo(repo).get_pull(number)
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
