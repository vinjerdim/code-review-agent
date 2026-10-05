from types import SimpleNamespace as NS

from reviewer.github_io import PRFile, fetch_pr


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
