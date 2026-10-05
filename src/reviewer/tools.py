"""Read-only tools exposed to the agent (read_file, grep, git_blame, list_tests).

Nothing here writes to disk, the git index, or the network. Every path is
resolved inside the repository root, and files the context filter would never
send to the model (secrets, lockfiles, vendored, generated) are refused here too.
Do not add write tools.
"""

import fnmatch
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pydantic
from pydantic import BaseModel, ConfigDict, Field

from reviewer.context import path_skip_reason

MAX_LINES = 400
MAX_CHARS = 30_000
GIT_TIMEOUT_S = 10

TEST_GLOBS = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.test.jsx",
    "*.spec.ts",
    "*.spec.js",
    "*Test.java",
    "*Tests.cs",
    "*_spec.rb",
)
TEST_DIRS = ("tests", "test", "__tests__", "spec")
HEX = frozenset("0123456789abcdef")


class ToolError(Exception):
    """A tool failed in a way the model should be told about (is_error result)."""


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFileInput(_Input):
    path: str = Field(description="Repository-relative file path, e.g. src/app.py")
    start_line: int = Field(default=1, ge=1, description="First line to return (1-based).")
    end_line: int | None = Field(
        default=None, ge=1, description=f"Last line to return; at most {MAX_LINES} lines."
    )


class GrepInput(_Input):
    pattern: str = Field(min_length=1, max_length=200, description="Extended regex (git grep -E).")
    path: str | None = Field(
        default=None, description="Optional directory or glob to limit the search, e.g. src/"
    )
    case_insensitive: bool = False


class GitBlameInput(_Input):
    path: str = Field(description="Repository-relative file path.")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1, description=f"At most {MAX_LINES} lines after start_line.")


class ListTestsInput(_Input):
    path: str | None = Field(default=None, description="Optional directory prefix to filter by.")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[_Input]


SPECS = (
    ToolSpec(
        "read_file",
        "Read lines from a file in the PR head checkout. Output lines are prefixed "
        "with their line numbers.",
        ReadFileInput,
    ),
    ToolSpec(
        "grep",
        "Search tracked files for a regex. Returns path:line:text matches. Use it to "
        "find callers, definitions, and other uses of changed code.",
        GrepInput,
    ),
    ToolSpec(
        "git_blame",
        "Show who last changed a line range and in which commit (short sha, author, "
        "date, commit summary). History may be shallow in CI.",
        GitBlameInput,
    ),
    ToolSpec(
        "list_tests",
        "List test files in the repository, optionally under a directory prefix.",
        ListTestsInput,
    ),
)
TOOL_NAMES = frozenset(s.name for s in SPECS)


def _cap(lines: list[str], what: str) -> str:
    out: list[str] = []
    size = 0
    for i, line in enumerate(lines):
        if i >= MAX_LINES or size + len(line) > MAX_CHARS:
            out.append(f"[output cut: showing {i} of {len(lines)} {what}]")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out) if out else f"(no {what})"


class RepoTools:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        self._handlers: dict[str, Callable[[Any], str]] = {
            "read_file": self.read_file,
            "grep": self.grep,
            "git_blame": self.git_blame,
            "list_tests": self.list_tests,
        }
        assert set(self._handlers) == TOOL_NAMES

    # -- API surface -----------------------------------------------------

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "input_schema": s.input_model.model_json_schema(),
            }
            for s in SPECS
        ]

    def run(self, name: str, raw_input: Any) -> tuple[str, bool]:
        """Run a tool; returns (output, is_error). Never raises for tool failures."""
        spec = next((s for s in SPECS if s.name == name), None)
        if spec is None:
            return f"Unknown tool {name!r}. Available: {sorted(TOOL_NAMES)}", True
        try:
            args = spec.input_model.model_validate(raw_input)
            return self._handlers[name](args), False
        except pydantic.ValidationError as exc:
            return f"Invalid input for {name}: {exc.errors(include_url=False)}", True
        except ToolError as exc:
            return str(exc), True

    # -- path safety -----------------------------------------------------

    def _resolve(self, path: str) -> tuple[Path, str]:
        if not path or PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
            raise ToolError("Path must be relative to the repository root.")
        requested = PurePosixPath(path.replace("\\", "/"))
        if ".." in requested.parts:
            raise ToolError("Path must not contain '..'.")
        resolved = (self.root / requested).resolve()
        if not resolved.is_relative_to(self.root):
            raise ToolError("Path resolves outside the repository.")
        rel = resolved.relative_to(self.root).as_posix()
        for candidate in (requested.as_posix(), rel):
            if candidate == ".git" or candidate.startswith(".git/"):
                raise ToolError("Access to .git is not allowed.")
            if reason := path_skip_reason(candidate):
                raise ToolError(f"Refused: {candidate} is a {reason} file.")
        return resolved, rel

    def _git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
        try:
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                timeout=GIT_TIMEOUT_S,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s.") from exc
        except FileNotFoundError as exc:
            raise ToolError("git is not available.") from exc

    @staticmethod
    def _decode(data: bytes) -> str:
        return data.decode("utf-8", errors="replace")

    # -- tools -----------------------------------------------------------

    def read_file(self, args: ReadFileInput) -> str:
        path, rel = self._resolve(args.path)
        if not path.is_file():
            raise ToolError(f"No such file: {rel}")
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            raise ToolError(f"{rel} looks binary.")
        lines = self._decode(data).splitlines()
        end = args.end_line or args.start_line + MAX_LINES - 1
        if end < args.start_line:
            raise ToolError("end_line must be >= start_line.")
        if args.start_line > len(lines):
            raise ToolError(f"{rel} has only {len(lines)} lines.")
        chosen = lines[args.start_line - 1 : end]
        numbered = [f"{args.start_line + i}: {text}" for i, text in enumerate(chosen)]
        return f"{rel} ({len(lines)} lines total)\n" + _cap(numbered, "lines")

    def grep(self, args: GrepInput) -> str:
        pathspec = "."
        if args.path:
            if args.path.startswith(":") or ".." in PurePosixPath(args.path).parts:
                raise ToolError("Invalid path filter.")
            pathspec = args.path
        flags = ["-n", "-I", "-E", "--no-color"]
        if args.case_insensitive:
            flags.append("-i")
        proc = self._git("grep", *flags, "-e", args.pattern, "--", pathspec)
        if proc.returncode == 1:
            return "(no matches)"
        if proc.returncode != 0:
            raise ToolError(f"grep failed: {self._decode(proc.stderr).strip()}")
        matches = [
            ln
            for ln in self._decode(proc.stdout).splitlines()
            if not path_skip_reason(ln.split(":", 1)[0])
        ]
        return _cap(matches, "matches")

    def git_blame(self, args: GitBlameInput) -> str:
        _, rel = self._resolve(args.path)
        if args.end_line < args.start_line:
            raise ToolError("end_line must be >= start_line.")
        end = min(args.end_line, args.start_line + MAX_LINES - 1)
        line_range = f"{args.start_line},{end}"
        proc = self._git("blame", "--line-porcelain", "--no-textconv", "-L", line_range, "--", rel)
        if proc.returncode != 0:
            raise ToolError(f"blame failed: {self._decode(proc.stderr).strip()}")
        return _cap(_summarize_blame(self._decode(proc.stdout)), "lines")

    def list_tests(self, args: ListTestsInput) -> str:
        proc = self._git("ls-files", "-z")
        if proc.returncode != 0:
            raise ToolError(f"ls-files failed: {self._decode(proc.stderr).strip()}")
        prefix = (args.path or "").strip("/")
        found = []
        for path in self._decode(proc.stdout).split("\0"):
            if not path or (prefix and not path.startswith(prefix + "/")):
                continue
            if path_skip_reason(path):
                continue
            p = PurePosixPath(path)
            if any(fnmatch.fnmatch(p.name, g) for g in TEST_GLOBS) or any(
                d in p.parts[:-1] for d in TEST_DIRS
            ):
                found.append(path)
        return _cap(sorted(found), "test files")


def _summarize_blame(porcelain: str) -> list[str]:
    """Turn `git blame --line-porcelain` into 'line sha author date | summary: text'."""
    out: list[str] = []
    info: dict[str, str] = {}
    for raw in porcelain.splitlines():
        if raw.startswith("\t"):
            out.append(
                f"{info.get('line', '?')} {info.get('sha', '')[:8]} {info.get('author', '?')} "
                f"{info.get('date', '')} | {info.get('summary', '')}: {raw[1:]}"
            )
            info = {}
            continue
        key, _, value = raw.partition(" ")
        if not info and len(key) in (40, 64) and all(c in HEX for c in key):
            parts = value.split()
            info["sha"], info["line"] = key, parts[1] if len(parts) > 1 else "?"
        elif key == "author":
            info["author"] = value
        elif key == "author-time" and value.isdigit():
            info["date"] = datetime.fromtimestamp(int(value), UTC).strftime("%Y-%m-%d")
        elif key == "summary":
            info["summary"] = value
    return out
