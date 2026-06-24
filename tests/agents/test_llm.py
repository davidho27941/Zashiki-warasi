"""Chat model factory: provider switching and error handling."""

from __future__ import annotations

import builtins
import sys

import pytest
from langchain_openai import ChatOpenAI

from zashiki_warasi.agents.llm import get_chat_model
from zashiki_warasi.core.config import LLMSettings


def _settings(**overrides) -> LLMSettings:
    base = dict(
        provider="llamacpp",
        base_url="http://example.test/v1",
        api_key="test-key",
        model="test-model",
        temperature=0.5,
    )
    base.update(overrides)
    return LLMSettings(**base)


# --- llamacpp / openai paths ---


class TestOpenAICompatibleProviders:
    def test_llamacpp_returns_chat_openai_with_base_url(self):
        model = get_chat_model(_settings(provider="llamacpp"))
        assert isinstance(model, ChatOpenAI)
        assert str(model.openai_api_base) == "http://example.test/v1"
        assert model.model_name == "test-model"

    def test_openai_provider_also_uses_chat_openai(self):
        model = get_chat_model(_settings(provider="openai"))
        assert isinstance(model, ChatOpenAI)

    def test_temperature_passed_through(self):
        model = get_chat_model(_settings(temperature=0.75))
        assert model.temperature == 0.75

    def test_api_key_passed_through(self):
        model = get_chat_model(_settings(api_key="my-secret"))
        # ChatOpenAI stores api_key as SecretStr
        assert model.openai_api_key.get_secret_value() == "my-secret"


# --- anthropic path ---


class TestAnthropicProvider:
    def test_raises_friendly_error_when_package_missing(self, monkeypatch):
        # Force the import to fail even if langchain-anthropic happens to be
        # installed in the test environment.
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("langchain_anthropic"):
                raise ImportError("simulated missing package")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.delitem(sys.modules, "langchain_anthropic", raising=False)

        with pytest.raises(RuntimeError, match="langchain-anthropic"):
            get_chat_model(_settings(provider="anthropic"))


# --- unknown provider ---


class TestUnknownProvider:
    def test_pydantic_rejects_unknown_provider_at_construction(self):
        # The Literal in LLMSettings should block invalid values up front,
        # so get_chat_model's ValueError is effectively unreachable from
        # the public API — but the factory still guards it as a safety net.
        with pytest.raises(Exception):
            LLMSettings(provider="mistral")

    def test_factory_raises_value_error_when_bypassing_settings(self):
        class FakeSettings:
            provider = "mistral"
            base_url = "x"
            api_key = "x"
            model = "x"
            temperature = 0.1

        with pytest.raises(ValueError, match="Unknown LLM provider"):
            get_chat_model(FakeSettings())  # type: ignore[arg-type]


# --- default arg (no settings supplied) ---


class TestDefaultSettings:
    def test_uses_llmsettings_from_env_when_none_passed(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "llamacpp")
        monkeypatch.setenv("LLM_BASE_URL", "http://from-env:9000/v1")
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("LLM_API_KEY", "env-key")

        model = get_chat_model()
        assert isinstance(model, ChatOpenAI)
        assert str(model.openai_api_base) == "http://from-env:9000/v1"
        assert model.model_name == "env-model"
