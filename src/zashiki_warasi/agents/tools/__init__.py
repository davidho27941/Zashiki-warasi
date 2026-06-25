"""LangChain BaseTool registry.

Re-exports the ToolRegistry class and a module-level `default_registry`
for code that wants a single shared registry without DI plumbing.
Subgraph-as-tool patterns (compiled LangGraph graphs invoked from a
parent agent) are NOT managed here — those live with the agent that
composes them.
"""

from zashiki_warasi.agents.tools.registry import ToolRegistry, default_registry

__all__ = ["ToolRegistry", "default_registry"]
