"""Runtime settings, read from environment variables.

The model name lives here and nowhere else in the code.
"""

import os
from dataclasses import dataclass
from typing import Literal

Effort = Literal["low", "medium", "high", "xhigh", "max"]
_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _int_env(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    model: str = "claude-opus-5-5"
    effort: Effort = "high"
    max_tokens: int = 16000
    # Server-side refusal fallback ("default" or None to disable).
    fallbacks: str | None = "default"
    max_file_patch_chars: int = 20_000
    max_total_patch_chars: int = 200_000
    github_token: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ) if env is None else env
        effort = env.get("REVIEWER_EFFORT", cls.effort)
        if effort not in _EFFORTS:
            raise ValueError(f"REVIEWER_EFFORT must be one of {_EFFORTS}, got {effort!r}")
        fallbacks = env.get("REVIEWER_FALLBACKS", cls.fallbacks or "")
        return cls(
            model=env.get("REVIEWER_MODEL") or cls.model,
            effort=effort,
            max_tokens=_int_env(env, "REVIEWER_MAX_TOKENS", cls.max_tokens),
            fallbacks=fallbacks or None,
            max_file_patch_chars=_int_env(
                env, "REVIEWER_MAX_FILE_PATCH_CHARS", cls.max_file_patch_chars
            ),
            max_total_patch_chars=_int_env(
                env, "REVIEWER_MAX_TOTAL_PATCH_CHARS", cls.max_total_patch_chars
            ),
            github_token=env.get("GITHUB_TOKEN") or None,
        )
