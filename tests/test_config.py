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


@pytest.mark.parametrize("env", [{"REVIEWER_EFFORT": "turbo"}, {"REVIEWER_MAX_TOKENS": "lots"}])
def test_invalid_env_rejected(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)
