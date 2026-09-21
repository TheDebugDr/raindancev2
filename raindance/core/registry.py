"""The tool plugin registry.

A tool is a self-contained unit that renders its own panel. It registers itself
by decorating a `ToolPlugin` subclass with `@register_tool`. The app discovers
tools by importing every module in the `plugins` package — so adding a new tool
is literally "drop a .py file in plugins/". The core never changes.
"""
from __future__ import annotations

import importlib
import pkgutil


class ToolPlugin:
    """Base class for a tool. Subclass, set the metadata, implement render()."""
    id: str = ""                 # unique, stable slug
    name: str = "Untitled"       # sidebar label
    icon: str = "extension"      # Quasar/Material icon name
    description: str = ""
    order: int = 100             # lower sorts higher in the sidebar
    stage: bool = False          # True → a lifecycle stage on the top rail

    def render(self, ctx) -> None:
        """Build this tool's UI. Called inside a NiceGUI layout context."""
        raise NotImplementedError


TOOL_REGISTRY: dict[str, ToolPlugin] = {}


def register_tool(cls):
    inst = cls()
    if not inst.id:
        raise ValueError(f"{cls.__name__} must set a unique .id")
    if inst.id in TOOL_REGISTRY:
        raise ValueError(f"duplicate tool id: {inst.id!r}")
    TOOL_REGISTRY[inst.id] = inst
    return cls


def tools_sorted() -> list[ToolPlugin]:
    return sorted(TOOL_REGISTRY.values(), key=lambda t: (t.order, t.name))


def stages_sorted() -> list[ToolPlugin]:
    """The lifecycle stages, in drop order — what the top rail renders."""
    return sorted((t for t in TOOL_REGISTRY.values() if t.stage),
                  key=lambda t: t.order)


def tools_only() -> list[ToolPlugin]:
    """Everything that is not a lifecycle stage (the legacy tool pages)."""
    return sorted((t for t in TOOL_REGISTRY.values() if not t.stage),
                  key=lambda t: (t.order, t.name))


def discover(package) -> list[str]:
    """Import every submodule of `package` so its decorators run.

    Used for both plugins and notification providers. A module that fails to
    import (e.g. a broken third-party tool) is skipped, not fatal — its error
    is returned so the app can surface it.
    """
    errors: list[str] = []
    for mod in pkgutil.iter_modules(package.__path__):
        if mod.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{package.__name__}.{mod.name}")
        except Exception as e:  # noqa: BLE001 - isolate one bad plugin
            errors.append(f"{mod.name}: {e}")
    return errors
