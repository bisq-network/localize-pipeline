"""Plugin loading for external localization adapters."""

from __future__ import annotations

import importlib
import os
from importlib import metadata
from typing import Iterable, List, Sequence

ENTRY_POINT_GROUP = "localize.format_adapters"
ENVIRONMENT_MODULES = "LOCALIZE_PLUGIN_MODULES"

_LOADED_PLUGIN_NAMES: set[str] = set()


def _split_module_list(value: str | None) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_entry_points() -> None:
    entry_points = metadata.entry_points()
    if hasattr(entry_points, "select"):
        candidates = entry_points.select(group=ENTRY_POINT_GROUP)
    else:  # pragma: no cover - Python <3.10 compatibility
        candidates = entry_points.get(ENTRY_POINT_GROUP, [])

    for entry_point in candidates:
        plugin_name = f"{ENTRY_POINT_GROUP}:{entry_point.name}"
        if plugin_name in _LOADED_PLUGIN_NAMES:
            continue
        loaded = entry_point.load()
        if callable(loaded):
            loaded()
        _LOADED_PLUGIN_NAMES.add(plugin_name)


def _load_modules(module_names: Iterable[str]) -> None:
    for module_name in module_names:
        if module_name in _LOADED_PLUGIN_NAMES:
            continue
        importlib.import_module(module_name)
        _LOADED_PLUGIN_NAMES.add(module_name)


def load_plugins(module_names: Sequence[str] | None = None) -> None:
    """Load adapter plugins from entry points, environment, and CLI modules.

    External packages can expose entry points in the ``localize.format_adapters``
    group, or users can pass module names through ``--plugin`` /
    ``LOCALIZE_PLUGIN_MODULES``. A plugin module should register adapters during
    import with ``localize.formats.register_localization_adapter``.
    """
    _load_entry_points()
    _load_modules(_split_module_list(os.environ.get(ENVIRONMENT_MODULES)))
    _load_modules(module_names or ())
