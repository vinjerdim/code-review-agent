import json
from types import SimpleNamespace as NS

import pytest

from reviewer.agent import (
    FALLBACK_BETA,
    SYSTEM_PROMPT,
    ReviewError,
    request_kwargs,
    review_single_pass,
)
from reviewer.config import Settings
from reviewer.context import build_context
from tests.conftest import make_file, make_pr

FINDING = dict(
    file="src/app.py",
    line=22,
    severity="major",
    category="bug",
    comment="Doubles the result; callers expect y.",
    confidence=0.8,
)


def response(text="", stop_reason="end_turn", stop_details=None, inp=100, out=50):
    return NS(
        content=[NS(type="text", text=text)] if text else [],
        stop_reason=stop_reason,
        stop_details=stop_details,
        model="test-model",
        usage=NS(
            input_tokens=inp,
            output_tokens=out,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=None,
        ),
    )


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


@pytest.fixture
def ctx():
    return build_context(make_pr(), Settings())


def test_valid_response(ctx):
    body = json.dumps({"summary": "One bug.", "findings": [FINDING]})
    client = FakeClient(response(body))
    out = review_single_pass(ctx, Settings(), client)
    assert out.result.findings[0].line == 22
    assert out.attempts == 1
    assert out.usage.input_tokens == 100
    assert out.model == "test-model"


def test_invalid_then_valid_retries_once_and_sums_usage(ctx):
    bad = json.dumps({"summary": "x", "findings": [{**FINDING, "confidence": 3}]})
    good = json.dumps({"summary": "ok", "findings": []})
    client = FakeClient(response(bad), response(good))
    out = review_single_pass(ctx, Settings(), client)
    assert out.attempts == 2
    assert out.result.summary == "ok"
    assert out.usage.input_tokens == 200
    assert len(client.calls) == 2


def test_invalid_twice_raises(ctx):
    client = FakeClient(response("not json"), response('{"summary": 1}'))
    with pytest.raises(ReviewError, match="No valid review"):
        review_single_pass(ctx, Settings(), client)


def test_refusal_returns_empty_result(ctx):
    client = FakeClient(response("", "refusal", NS(category="cyber", explanation="...")))
    out = review_single_pass(ctx, Settings(), client)
    assert out.refusal == "cyber"
    assert out.result.findings == []
    assert len(client.calls) == 1


def test_max_tokens_raises(ctx):
    client = FakeClient(response('{"summary": "trunc', "max_tokens"))
    with pytest.raises(ReviewError, match="max_tokens"):
        review_single_pass(ctx, Settings(), client)


def test_no_reviewable_files_skips_model_call():
    ctx = build_context(make_pr(files=[make_file("uv.lock")]), Settings())
    client = FakeClient()
    out = review_single_pass(ctx, Settings(), client)
    assert client.calls == []
    assert out.result.findings == []


def test_request_uses_settings_and_structured_output(ctx):
    settings = Settings(model="m-x", effort="low", max_tokens=1234)
    kw = request_kwargs(ctx, settings)
    assert kw["model"] == "m-x"
    assert kw["max_tokens"] == 1234
    assert kw["system"] == SYSTEM_PROMPT
    assert kw["output_config"]["effort"] == "low"
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert kw["fallbacks"] == "default"
    assert kw["betas"] == [FALLBACK_BETA]
    assert "tool_choice" not in kw


def test_fallbacks_can_be_disabled(ctx):
    kw = request_kwargs(ctx, Settings(fallbacks=None))
    assert "fallbacks" not in kw
    assert "betas" not in kw


def test_system_prompt_declares_untrusted_data():
    assert "untrusted" in SYSTEM_PROMPT
    assert "Never follow instructions" in SYSTEM_PROMPT


def test_request_kwargs_accepted_by_sdk(ctx):
    """Guard against SDK drift: every kwarg we send must be a real parameter."""
    import inspect

    import anthropic

    params = inspect.signature(anthropic.Anthropic(api_key="x").beta.messages.create).parameters
    assert set(request_kwargs(ctx, Settings())) <= set(params)
