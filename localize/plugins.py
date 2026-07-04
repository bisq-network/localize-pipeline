"""Plugin loading for external localization adapters."""

from __future__ import annotations

import importlib
import logging
import os
from importlib import metadata
from typing import Iterable, List, Sequence

ENTRY_POINT_GROUP = "localize.format_adapters"
ENVIRONMENT_MODULES = "LOCALIZE_PLUGIN_MODULES"
DISABLE_ENTRY_POINTS_ENV = "LOCALIZE_DISABLE_ENTRY_POINT_PLUGINS"

_LOADED_PLUGIN_NAMES: set[str] = set()
logger = logging.getLogger(__name__)


def _split_module_list(value: str | None) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_entry_points() -> None:
    if os.environ.get(DISABLE_ENTRY_POINTS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        logger.info("Entry-point localization plugins disabled by %s.", DISABLE_ENTRY_POINTS_ENV)
        return

    entry_points = metadata.entry_points()
    if hasattr(entry_points, "select"):
        candidates = entry_points.select(group=ENTRY_POINT_GROUP)
    else:  # pragma: no cover - Python <3.10 compatibility
        candidates = entry_points.get(ENTRY_POINT_GROUP, [])

    for entry_point in candidates:
        plugin_name = f"{ENTRY_POINT_GROUP}:{entry_point.name}"
        if plugin_name in _LOADED_PLUGIN_NAMES:
            continue
        try:
            loaded = entry_point.load()
            if callable(loaded):
                loaded()
        except Exception as exc:
            logger.warning("Could not load localization plugin entry point %s: %s", plugin_name, exc)
            continue
        _LOADED_PLUGIN_NAMES.add(plugin_name)


def _load_modules(module_names: Iterable[str], *, source: str) -> None:
    for module_name in module_names:
        if module_name in _LOADED_PLUGIN_NAMES:
            continue
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            logger.warning(
                "Could not load localization plugin module '%s' from %s: %s",
                module_name,
                source,
                exc,
            )
            continue
        _LOADED_PLUGIN_NAMES.add(module_name)


def load_plugins(module_names: Sequence[str] | None = None) -> None:
    """Load adapter plugins from entry points, environment, and CLI modules.

    External packages can expose entry points in the ``localize.format_adapters``
    group, or users can pass module names through ``--plugin`` /
    ``LOCALIZE_PLUGIN_MODULES``. A plugin module should register adapters during
    import with ``localize.formats.register_localization_adapter``.
    """
    _load_entry_points()
    _load_modules(_split_module_list(os.environ.get(ENVIRONMENT_MODULES)), source=ENVIRONMENT_MODULES)
    _load_modules(module_names or (), source="--plugin")
