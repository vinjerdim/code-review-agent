from types import SimpleNamespace as NS

from reviewer.github_io import PRFile, fetch_pr, has_existing_review, post_review


class FakeGithub:
    def __init__(self, pull):
        self.pull = pull
        self.calls = []

    def get_repo(self, name):
        self.calls.append(("repo", name))
        return NS(get_pull=lambda n: self.calls.append(("pull", n)) or self.pull)


def test_fetch_pr_converts_to_plain_data():
    files = [
        NS(
            filename="a.py",
            status="modified",
            patch="@@ -1 +1 @@\n-x\n+y",
            additions=1,
            deletions=1,
            previous_filename=None,
        ),
        NS(
            filename="new.py",
            status="renamed",
            patch=None,
            additions=0,
            deletions=0,
            previous_filename="old.py",
        ),
    ]
    pull = NS(
        title="T",
        body=None,
        user=NS(login="dev"),
        base=NS(sha="b1"),
        head=NS(sha="h1"),
        get_files=lambda: iter(files),
    )
    gh = FakeGithub(pull)

    pr = fetch_pr(gh, "octo/demo", 7)

    assert gh.calls == [("repo", "octo/demo"), ("pull", 7)]
    assert (pr.title, pr.body, pr.author, pr.base_sha, pr.head_sha) == ("T", "", "dev", "b1", "h1")
    assert pr.files == [
        PRFile("a.py", "modified", "@@ -1 +1 @@\n-x\n+y", 1, 1, None),
        PRFile("new.py", "renamed", None, 0, 0, "old.py"),
    ]


class FakePull:
    def __init__(self, reviews=()):
        self.reviews = list(reviews)
        self.created = []
        self.base = NS(repo=NS(get_commit=lambda sha: NS(sha=sha)))

    def get_reviews(self):
        return iter(self.reviews)

    def create_review(self, **kwargs):
        self.created.append(kwargs)
        return NS(id=1)


def test_post_review_always_comment_event_pinned_to_head():
    pull = FakePull()
    comments = [{"path": "a.py", "position": 3, "body": "x"}]
    post_review(pull, "h1", "summary", comments)
    (call,) = pull.created
    assert call["event"] == "COMMENT"
    assert call["commit"].sha == "h1"
    assert call["body"] == "summary"
    assert call["comments"] == comments


def test_post_review_has_no_event_override():
    import inspect

    assert "event" not in inspect.signature(post_review).parameters


def test_existing_review_detection():
    m = "<!-- marker -->"
    pull = FakePull(
        reviews=[
            NS(commit_id="old", body=m),
            NS(commit_id="h1", body="human review"),
            NS(commit_id="h1", body=None),
        ]
    )
    assert not has_existing_review(pull, "h1", m)
    pull.reviews.append(NS(commit_id="h1", body=f"{m}\nsummary"))
    assert has_existing_review(pull, "h1", m)
