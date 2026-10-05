import pytest
from pydantic import ValidationError

from reviewer.schema import Finding, ReviewResult

VALID = dict(
    file="src/app.py",
    line=3,
    severity="major",
    category="bug",
    comment="Off by one.",
    confidence=0.9,
)


def test_valid_finding_and_optional_fix_defaults_to_none():
    f = Finding(**VALID)
    assert f.suggested_fix is None


@pytest.mark.parametrize(
    "override",
    [
        {"line": 0},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"severity": "blocker"},
        {"category": "vibes"},
        {"comment": ""},
        {"file": ""},
        {"extra_field": "x"},
    ],
)
def test_invalid_findings_rejected(override):
    with pytest.raises(ValidationError):
        Finding(**{**VALID, **override})


REQUIRED = ["file", "line", "severity", "category", "comment", "confidence"]


@pytest.mark.parametrize("missing", REQUIRED)
def test_required_fields(missing):
    data = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ValidationError):
        Finding(**data)


def test_review_result_parses_json():
    r = ReviewResult.model_validate_json('{"summary": "ok", "findings": []}')
    assert r.findings == []


def test_review_result_rejects_bad_nested_finding():
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({"summary": "x", "findings": [{**VALID, "line": -3}]})
