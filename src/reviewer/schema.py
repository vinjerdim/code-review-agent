"""Pydantic schema for structured review findings.

Every review comment the agent posts must come from a validated `Finding`.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["critical", "major", "minor", "nit"]
Category = Literal[
    "bug",
    "security",
    "performance",
    "correctness",
    "maintainability",
    "testing",
    "style",
]


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1, description="Path of the file in the PR head, as shown.")
    line: int = Field(ge=1, description="Line number in the NEW version of the file.")
    severity: Severity
    category: Category
    comment: str = Field(min_length=1, description="What is wrong and why it matters.")
    confidence: float = Field(ge=0.0, le=1.0, description="0..1 confidence the issue is real.")
    suggested_fix: str | None = Field(
        default=None, description="Optional replacement code or concrete fix."
    )


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="One-paragraph overall assessment of the change.")
    findings: list[Finding] = Field(default_factory=list)
