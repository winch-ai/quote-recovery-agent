"""Spec for configuration loading. The load-bearing assertion is that no secret
appears in a repr — dataclass reprs end up in logs and tracebacks.
"""
import dataclasses

import pytest

from winch.config import ConfigError, Settings

MINIMAL = {
    "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
    "AZURE_OPENAI_API_KEY": "super-secret-key-value",
    "LLM_MODEL": "azure_openai:gpt-4-1-mini",
}


@pytest.fixture
def env(monkeypatch):
    for k, v in MINIMAL.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


def test_loads_from_environment(env):
    s = Settings.from_env()
    assert s.azure_deployment == "gpt-4-1-mini"
    assert s.azure_endpoint == "https://example.openai.azure.com"  # trailing slash stripped


def test_secrets_never_appear_in_repr(env):
    """Reprs land in logs and tracebacks. Do not weaken this."""
    env.setenv("META_ACCESS_TOKEN", "meta-token-secret")
    env.setenv("META_APP_SECRET", "app-secret-value")
    env.setenv("DATABASE_URL", "postgresql://user:dbpassword@host/db")
    text = repr(Settings.from_env())
    for secret in ("super-secret-key-value", "meta-token-secret",
                   "app-secret-value", "dbpassword"):
        assert secret not in text


def test_missing_endpoint_raises_at_boot(env):
    env.delenv("AZURE_OPENAI_ENDPOINT")
    with pytest.raises(ConfigError, match="AZURE_OPENAI_ENDPOINT"):
        Settings.from_env()


def test_missing_model_raises(env):
    env.delenv("LLM_MODEL")
    with pytest.raises(ConfigError, match="LLM_MODEL"):
        Settings.from_env()


def test_provider_prefix_is_stripped(env):
    env.setenv("LLM_MODEL", "azure_openai:gpt-5-mini")
    assert Settings.from_env().azure_deployment == "gpt-5-mini"


def test_bare_model_name_without_prefix_works(env):
    env.setenv("LLM_MODEL", "gpt-4-1-mini")
    assert Settings.from_env().azure_deployment == "gpt-4-1-mini"


def test_settings_is_frozen(env):
    s = Settings.from_env()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.azure_api_key = "changed"  # type: ignore[misc]
