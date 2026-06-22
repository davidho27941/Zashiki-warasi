"""Chat model factory.

Wraps LangChain's `BaseChatModel` ABC; selecting a provider is just an
env var. llama.cpp is reached via its OpenAI-compatible HTTP server,
so we reuse `langchain-openai` with a custom `base_url`.
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from zashiki_warasi.core.config import LLMSettings


def get_chat_model(settings: LLMSettings | None = None) -> BaseChatModel:
    settings = settings or LLMSettings()

    if settings.provider in ("llamacpp", "openai"):
        return ChatOpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
            temperature=settings.temperature,
        )

    if settings.provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:
            raise RuntimeError(
                "LLM_PROVIDER=anthropic requires `langchain-anthropic`. "
                "Install with: uv add langchain-anthropic"
            ) from exc
        return ChatAnthropic(
            api_key=settings.api_key,
            model=settings.model,
            temperature=settings.temperature,
        )

    raise ValueError(f"Unknown LLM provider: {settings.provider}")
