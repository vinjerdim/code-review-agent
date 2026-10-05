import os
import subprocess

import pytest

from reviewer.tools import MAX_LINES, TOOL_NAMES, RepoTools


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef double(x):\n    return add(x, x)\n"
    )
    (root / "src" / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(1000)))
    (root / "tests" / "test_app.py").write_text("from src.app import add\n")
    (root / "src" / "util_test.go").write_text("package src\n")
    (root / ".env").write_text("API_KEY=sekrit\n")
    (root / "uv.lock").write_text("lock\n")
    (root / "blob.bin").write_bytes(b"\0\1\2")
    git(root, "init", "-q")
    git(root, "-c", "user.name=Ada", "-c", "user.email=ada@example.com", "add", "-A")
    git(
        root,
        "-c",
        "user.name=Ada",
        "-c",
        "user.email=ada@example.com",
        "commit",
        "-q",
        "-m",
        "initial import",
    )
    return root


@pytest.fixture
def tools(repo):
    return RepoTools(repo)


def test_tool_set_is_exactly_read_only(tools):
    assert {"read_file", "grep", "git_blame", "list_tests"} == TOOL_NAMES
    assert {d["name"] for d in tools.definitions()} == TOOL_NAMES
    for d in tools.definitions():
        assert d["input_schema"]["type"] == "object"
        assert d["input_schema"]["additionalProperties"] is False


def test_read_file_with_range(tools):
    out, err = tools.run("read_file", {"path": "src/app.py", "start_line": 5, "end_line": 6})
    assert not err
    assert out.splitlines() == [
        "src/app.py (6 lines total)",
        "5: def double(x):",
        "6:     return add(x, x)",
    ]


def test_read_file_is_capped(tools):
    out, err = tools.run("read_file", {"path": "src/big.py"})
    assert not err
    assert f"{MAX_LINES}: x{MAX_LINES - 1} = {MAX_LINES - 1}" in out
    assert f"{MAX_LINES + 1}:" not in out


@pytest.mark.parametrize(
    ("path", "fragment"),
    [
        ("../outside.txt", ".."),
        ("src/../../outside.txt", ".."),
        ("/etc/passwd", "relative"),
        ("C:\\Windows\\win.ini", "relative"),
        (".env", "secret"),
        ("uv.lock", "lockfile"),
        (".git/config", ".git"),
        ("src/missing.py", "No such file"),
        ("blob.bin", "binary"),
    ],
)
def test_read_file_refusals(tools, path, fragment):
    out, err = tools.run("read_file", {"path": path})
    assert err
    assert fragment in out


def test_symlink_escaping_repo_is_refused(tools, repo, tmp_path):
    (tmp_path / "outside.txt").write_text("secret outside\n")
    try:
        os.symlink(tmp_path / "outside.txt", repo / "link.txt")
        os.symlink(repo / ".env", repo / "innocent.txt")
    except OSError:
        pytest.skip("symlinks not permitted on this platform")
    out, err = tools.run("read_file", {"path": "link.txt"})
    assert err and "outside the repository" in out
    out, err = tools.run("read_file", {"path": "innocent.txt"})
    assert err and "secret" in out


def test_grep_finds_callers_and_skips_secret_files(tools):
    out, err = tools.run("grep", {"pattern": r"add\("})
    assert not err
    assert "src/app.py:6:    return add(x, x)" in out
    out, _ = tools.run("grep", {"pattern": "sekrit"})
    assert out == "(no matches)"


def test_grep_path_filter_and_bad_input(tools):
    out, err = tools.run("grep", {"pattern": "import", "path": "tests/"})
    assert not err and out.startswith("tests/test_app.py:1:")
    _, err = tools.run("grep", {"pattern": "x", "path": "../"})
    assert err
    _, err = tools.run("grep", {"pattern": "("})
    assert err  # invalid regex surfaces as a tool error, not an exception


def test_git_blame_line_range(tools):
    out, err = tools.run("git_blame", {"path": "src/app.py", "start_line": 1, "end_line": 2})
    assert not err
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("1 ")
    assert "Ada" in lines[0] and "initial import: def add(a, b):" in lines[0]


def test_git_blame_refuses_secret_and_bad_range(tools):
    _, err = tools.run("git_blame", {"path": ".env", "start_line": 1, "end_line": 1})
    assert err
    _, err = tools.run("git_blame", {"path": "src/app.py", "start_line": 3, "end_line": 1})
    assert err


def test_list_tests(tools):
    out, err = tools.run("list_tests", {})
    assert not err
    assert out.splitlines() == ["src/util_test.go", "tests/test_app.py"]
    out, _ = tools.run("list_tests", {"path": "tests"})
    assert out.splitlines() == ["tests/test_app.py"]


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("write_file", {"path": "x"}),
        ("read_file", {"path": "src/app.py", "mode": "w"}),
        ("read_file", {}),
        ("grep", {"pattern": ""}),
    ],
)
def test_unknown_tools_and_bad_inputs_are_errors(tools, name, args):
    _, err = tools.run(name, args)
    assert err


def test_tools_never_modify_the_repo(tools, repo):
    before = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    for name, args in [
        ("read_file", {"path": "src/app.py"}),
        ("grep", {"pattern": "def"}),
        ("git_blame", {"path": "src/app.py", "start_line": 1, "end_line": 6}),
        ("list_tests", {}),
    ]:
        tools.run(name, args)
    after = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert before == after
