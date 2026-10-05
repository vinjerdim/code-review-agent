"""Build review context from a PR: file filtering, size budget, and prompt rendering.

Nothing reaches the model unless it passes `filter_files` here first.
"""

import fnmatch
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from reviewer.config import Settings
from reviewer.github_io import PRData, PRFile

LOCKFILES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
}
LOCKFILE_GLOBS = ("*.lock",)
GENERATED_GLOBS = (
    "*.min.js",
    "*.min.css",
    "*.map",
    "*_pb2.py",
    "*_pb2.pyi",
    "*_pb2_grpc.py",
    "*.pb.go",
    "*.generated.*",
)
GENERATED_ROOT_DIRS = ("dist", "build")
VENDORED_DIRS = ("vendor", "third_party", "node_modules")
SECRET_GLOBS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*")

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class SkippedFile:
    filename: str
    reason: str


@dataclass(frozen=True)
class ReviewContext:
    pr: PRData
    files: list[PRFile]
    skipped: list[SkippedFile] = field(default_factory=list)


def path_skip_reason(path: str) -> str | None:
    p = PurePosixPath(path)
    name = p.name
    parts = p.parts
    if any(fnmatch.fnmatch(name, g) for g in SECRET_GLOBS):
        return "secret"
    if name in LOCKFILES or any(fnmatch.fnmatch(name, g) for g in LOCKFILE_GLOBS):
        return "lockfile"
    if any(d in parts[:-1] for d in VENDORED_DIRS):
        return "vendored"
    if len(parts) > 1 and parts[0] in GENERATED_ROOT_DIRS:
        return "generated"
    if any(fnmatch.fnmatch(name, g) for g in GENERATED_GLOBS):
        return "generated"
    return None


def skip_reason(f: PRFile) -> str | None:
    """Why `f` must not be sent to the model, or None if it may be reviewed."""
    for path in (f.filename, f.previous_filename):
        if path and (reason := path_skip_reason(path)):
            return reason
    if f.status == "removed":
        return "removed"
    if not f.patch:
        return "no_patch"
    return None


def filter_files(files: list[PRFile]) -> tuple[list[PRFile], list[SkippedFile]]:
    kept: list[PRFile] = []
    skipped: list[SkippedFile] = []
    for f in files:
        reason = skip_reason(f)
        if reason:
            skipped.append(SkippedFile(f.filename, reason))
        else:
            kept.append(f)
    return kept, skipped


def build_context(pr: PRData, settings: Settings) -> ReviewContext:
    """Filter files, then apply size limits. Oversized files are omitted, never truncated."""
    kept, skipped = filter_files(pr.files)
    files: list[PRFile] = []
    total = 0
    for f in kept:
        size = len(f.patch or "")
        if size > settings.max_file_patch_chars:
            skipped.append(SkippedFile(f.filename, "file_too_large"))
        elif total + size > settings.max_total_patch_chars:
            skipped.append(SkippedFile(f.filename, "pr_budget_exceeded"))
        else:
            files.append(f)
            total += size
    return ReviewContext(pr=pr, files=files, skipped=skipped)


def annotate_patch(patch: str) -> str:
    """Prefix each diff line with its line number in the new file.

    Added and context lines get the new-file number; removed lines get blanks
    because they cannot carry an inline comment on the new side.
    """
    out: list[str] = []
    for pl in iter_patch_lines(patch):
        if pl.kind == "hunk":
            out.append(pl.text)
        elif pl.new_line is None:
            out.append(f"{'':>6} {pl.text}")
        else:
            out.append(f"{pl.new_line:>6} {pl.text}")
    return "\n".join(out)


@dataclass(frozen=True)
class PatchLine:
    kind: str  # "hunk" | "add" | "del" | "ctx" | "meta"
    text: str
    new_line: int | None  # line number in the new file; only for "add" and "ctx"
    position: int | None  # GitHub diff position; None for the first hunk header


def iter_patch_lines(patch: str) -> Iterator[PatchLine]:
    """Walk a GitHub file patch, tracking new-file line numbers and diff positions.

    GitHub defines position as the number of lines below the first "@@" header:
    the line right after it is position 1, and every later line (including later
    hunk headers, removed lines, and "\\ No newline" markers) advances it by one.
    """
    new_line: int | None = None
    first_hunk: int | None = None
    for i, raw in enumerate(patch.splitlines()):
        m = _HUNK_RE.match(raw)
        if m:
            new_line = int(m.group(1))
            if first_hunk is None:
                first_hunk = i
        position = i - first_hunk if first_hunk is not None and i > first_hunk else None
        kind = _line_kind(raw, m is not None, in_hunk=new_line is not None)
        if kind in ("add", "ctx"):
            yield PatchLine(kind, raw, new_line, position)
            new_line += 1  # type: ignore[operator]  # in_hunk guarantees an int
        else:
            yield PatchLine(kind, raw, None, position)


def _line_kind(raw: str, is_header: bool, in_hunk: bool) -> str:
    if is_header:
        return "hunk"
    if not in_hunk or raw.startswith("\\"):
        return "meta"
    if raw.startswith("-"):
        return "del"
    # "+" or " " (context); a bare empty line is context too
    return "add" if raw.startswith("+") else "ctx"


def escape_untrusted(text: str) -> str:
    # Stop untrusted text from closing our delimiter tags.
    return text.replace("</untrusted_", "<\\/untrusted_")


def render_user_prompt(ctx: ReviewContext) -> str:
    pr = ctx.pr
    parts = [
        "Review the pull request below. Report only real problems introduced or exposed by "
        "the change, using line numbers from the left-hand column (new-file lines).",
        "",
        "<untrusted_pr_metadata>",
        f"Repository: {escape_untrusted(pr.repo)}",
        f"PR #{pr.number} by {escape_untrusted(pr.author)}",
        f"Title: {escape_untrusted(pr.title)}",
        "Description:",
        escape_untrusted(pr.body) or "(empty)",
        "</untrusted_pr_metadata>",
        "",
    ]
    for f in ctx.files:
        header = f"File: {escape_untrusted(f.filename)} (status: {f.status}"
        if f.previous_filename and f.previous_filename != f.filename:
            header += f", renamed from {escape_untrusted(f.previous_filename)}"
        header += ")"
        parts += [
            f'<untrusted_diff file="{escape_untrusted(f.filename)}">',
            header,
            escape_untrusted(annotate_patch(f.patch or "")),
            "</untrusted_diff>",
            "",
        ]
    if ctx.skipped:
        parts.append("Files changed in this PR but not shown to you (do not comment on them):")
        parts += [f"- {escape_untrusted(s.filename)} ({s.reason})" for s in ctx.skipped]
    return "\n".join(parts).rstrip() + "\n"
