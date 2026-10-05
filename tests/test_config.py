import pytest

from reviewer.config import Settings


def test_defaults_from_empty_env():
    s = Settings.from_env({})
    assert s == Settings()
    assert s.fallbacks == "default"


def test_env_overrides():
    s = Settings.from_env(
        {
            "REVIEWER_MODEL": "other-model",
            "REVIEWER_EFFORT": "max",
            "REVIEWER_MAX_TOKENS": "999",
            "REVIEWER_FALLBACKS": "",
            "GITHUB_TOKEN": "t",
        }
    )
    assert (s.model, s.effort, s.max_tokens, s.fallbacks, s.github_token) == (
        "other-model",
        "max",
        999,
        None,
        "t",
    )


def test_empty_values_fall_back_to_defaults():
    """Unset GitHub repo variables arrive as empty strings in the workflow."""
    keys = [
        "REVIEWER_MODEL",
        "REVIEWER_EFFORT",
        "REVIEWER_MIN_CONFIDENCE",
        "REVIEWER_MAX_COMMENTS",
        "REVIEWER_MAX_TOKENS",
        "REVIEWER_MODE",
        "REVIEWER_MAX_STEPS",
        "REVIEWER_MAX_AGENT_TOKENS",
        "REVIEWER_REPO_ROOT",
    ]
    assert Settings.from_env(dict.fromkeys(keys, "")) == Settings()


@pytest.mark.parametrize(
    "env",
    [
        {"REVIEWER_EFFORT": "turbo"},
        {"REVIEWER_MAX_TOKENS": "lots"},
        {"REVIEWER_MIN_CONFIDENCE": "high"},
        {"REVIEWER_MODE": "yolo"},
        {"REVIEWER_MAX_STEPS": "many"},
    ],
)
def test_invalid_env_rejected(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)
