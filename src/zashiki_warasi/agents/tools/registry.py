"""Central registry for LangChain BaseTool instances.

Agents pull tools from a registry via `.all()` (to bind every
registered tool to their chat model) or `.get(name)` (for direct
invocation outside of an LLM tool-calling loop).
"""

from __future__ import annotations

from typing import Iterator

from langchain_core.tools import BaseTool


class ToolRegistry:
    """A name-indexed collection of BaseTool instances.

    Not thread-safe; register tools at startup (module import / app
    wiring) and treat the registry as read-only thereafter.
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> BaseTool:
        """Add `tool` to the registry. Returns it so this can be used
        as a decorator on `@tool`-decorated functions.

        Raises:
            TypeError: `tool` is not a BaseTool.
            ValueError: A tool with the same name is already registered.
        """
        if not isinstance(tool, BaseTool):
            raise TypeError(
                f"register() expects a langchain BaseTool, got "
                f"{type(tool).__name__}. Wrap plain functions with "
                "`@tool` (from langchain_core.tools) first."
            )
        if tool.name in self._tools:
            raise ValueError(
                f"A tool named {tool.name!r} is already registered."
            )
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> BaseTool:
        """Return the tool registered under `name`. Raises KeyError if
        missing."""
        return self._tools[name]

    def all(self) -> list[BaseTool]:
        """Snapshot of every registered tool, in insertion order.

        Suitable for passing directly to `chat_model.bind_tools(...)`.
        """
        return list(self._tools.values())

    def names(self) -> list[str]:
        """Insertion-ordered list of registered tool names."""
        return list(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[BaseTool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


default_registry = ToolRegistry()
