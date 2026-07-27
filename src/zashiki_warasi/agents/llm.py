"""Chat model factory.

Wraps LangChain's `BaseChatModel` ABC; selecting a provider is just an
env var. llama.cpp is reached via its OpenAI-compatible HTTP server,
so we reuse `langchain-openai` with a custom `base_url`.
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from zashiki_warasi.core.config import LLMSettings


def get_chat_model(
    settings: LLMSettings | None = None,
    *,
    max_tokens: int | None = None,
) -> BaseChatModel:
    """Build a chat model.

    `max_tokens` (when non-None) caps generation length per call.
    Pass it to bound-scope degenerate loops in structured-output
    nodes; leave it None on nodes that legitimately need long
    completions (e.g. the expense extraction JSON).
    """
    settings = settings or LLMSettings()

    if settings.provider in ("llamacpp", "openai"):
        kwargs = dict(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
            temperature=settings.temperature,
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return ChatOpenAI(**kwargs)

    if settings.provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:
            raise RuntimeError(
                "LLM_PROVIDER=anthropic requires `langchain-anthropic`. "
                "Install with: uv add langchain-anthropic"
            ) from exc
        kwargs = dict(
            api_key=settings.api_key,
            model=settings.model,
            temperature=settings.temperature,
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return ChatAnthropic(**kwargs)

    raise ValueError(f"Unknown LLM provider: {settings.provider}")
