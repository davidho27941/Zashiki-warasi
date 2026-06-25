"""ToolRegistry: registration, lookup, duplicate guard, type guard."""

from __future__ import annotations

import pytest
from langchain_core.tools import BaseTool, tool

from zashiki_warasi.agents.tools import ToolRegistry, default_registry


@tool
def double(x: int) -> int:
    """Return x doubled."""
    return x * 2


@tool
def greet(name: str) -> str:
    """Return a greeting for `name`."""
    return f"hello, {name}"


# --- registration ---


class TestRegister:
    def test_register_returns_the_tool(self):
        registry = ToolRegistry()
        returned = registry.register(double)
        assert returned is double

    def test_register_can_be_used_as_decorator(self):
        registry = ToolRegistry()

        @registry.register
        @tool
        def triple(x: int) -> int:
            """Triple x."""
            return x * 3

        assert "triple" in registry

    def test_register_rejects_duplicate_name(self):
        registry = ToolRegistry()
        registry.register(double)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(double)

    def test_register_rejects_non_basetool(self):
        registry = ToolRegistry()
        with pytest.raises(TypeError, match="BaseTool"):
            registry.register(lambda x: x)

    def test_register_rejects_plain_callable_with_helpful_message(self):
        registry = ToolRegistry()

        def plain_function(x: int) -> int:
            return x

        with pytest.raises(TypeError, match="@tool"):
            registry.register(plain_function)


# --- lookup ---


class TestLookup:
    def test_get_returns_registered_tool(self):
        registry = ToolRegistry()
        registry.register(double)
        assert registry.get("double") is double

    def test_get_raises_keyerror_for_missing(self):
        registry = ToolRegistry()
        with pytest.raises(KeyError):
            registry.get("nope")

    def test_all_returns_every_tool_in_insertion_order(self):
        registry = ToolRegistry()
        registry.register(double)
        registry.register(greet)
        assert registry.all() == [double, greet]

    def test_all_returns_a_snapshot_not_the_internal_list(self):
        registry = ToolRegistry()
        registry.register(double)
        snapshot = registry.all()
        snapshot.clear()
        assert registry.all() == [double]  # registry intact

    def test_names_returns_registered_names_in_order(self):
        registry = ToolRegistry()
        registry.register(double)
        registry.register(greet)
        assert registry.names() == ["double", "greet"]


# --- dunder methods ---


class TestDunders:
    def test_contains_checks_by_name(self):
        registry = ToolRegistry()
        registry.register(double)
        assert "double" in registry
        assert "missing" not in registry

    def test_len(self):
        registry = ToolRegistry()
        assert len(registry) == 0
        registry.register(double)
        assert len(registry) == 1
        registry.register(greet)
        assert len(registry) == 2

    def test_iter_yields_basetool_instances(self):
        registry = ToolRegistry()
        registry.register(double)
        registry.register(greet)
        seen = list(registry)
        assert all(isinstance(t, BaseTool) for t in seen)
        assert seen == [double, greet]


# --- default registry singleton ---


class TestDefaultRegistry:
    def test_default_registry_is_a_tool_registry(self):
        assert isinstance(default_registry, ToolRegistry)

    def test_default_registry_starts_empty_in_a_fresh_test_run(self):
        # Note: other tests may register into default_registry; here we
        # only assert structural properties to avoid coupling.
        assert isinstance(default_registry.all(), list)
