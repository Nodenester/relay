"""Plugin discovery and loading.

Scans `plugins/` for folders containing `plugin.json`. For enabled plugins,
imports the declared module and calls its `async setup(api)` function.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .event_bus import EventBus
from .plugin_api import PluginAPI, RegisteredTool

log = logging.getLogger("relay.plugin_loader")


@dataclass
class LoadedPlugin:
    name: str
    config: dict
    module: Any
    directory: Path


async def load_plugins(
    plugins_dir: Path,
    config: dict,
    event_bus: EventBus,
    tool_registry: list[RegisteredTool],
    task_group: list[asyncio.Task],
) -> list[LoadedPlugin]:
    loaded: list[LoadedPlugin] = []
    if not plugins_dir.exists():
        log.warning("No plugins directory at %s", plugins_dir)
        return loaded

    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_") or entry.name.startswith("."):
            continue

        plugin_json = entry / "plugin.json"
        if not plugin_json.exists():
            log.debug("Skipping %s — no plugin.json", entry.name)
            continue

        try:
            meta = json.loads(plugin_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.error("Invalid plugin.json in %s: %s", entry.name, e)
            continue

        name = meta.get("name", entry.name)
        plugin_cfg = config.get("plugins", {}).get(name, {})
        if not plugin_cfg.get("enabled", False):
            log.info("Plugin %s disabled - skipping", name)
            continue

        module_name = meta.get("module", "plugin")
        module_path = entry / f"{module_name}.py"
        if not module_path.exists():
            log.error("Plugin %s: module file %s.py missing", name, module_name)
            continue

        # Import the module under a unique name so multiple plugins don't collide.
        # `submodule_search_locations` makes the plugin folder a package so a
        # plugin can split itself across multiple files (e.g. plugin.py +
        # watcher.py) and use relative imports between them.
        full_module_name = f"relay_plugin_{name}"
        spec = importlib.util.spec_from_file_location(
            full_module_name,
            module_path,
            submodule_search_locations=[str(entry)],
        )
        if spec is None or spec.loader is None:
            log.error("Plugin %s: could not create import spec", name)
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            log.exception("Plugin %s: import failed", name)
            continue

        setup_fn = getattr(module, "setup", None)
        if setup_fn is None:
            log.error("Plugin %s: no setup() function in %s.py", name, module_name)
            continue

        api = PluginAPI(
            plugin_name=name,
            event_bus=event_bus,
            config=plugin_cfg,
            tool_registry=tool_registry,
            task_group=task_group,
        )
        try:
            if asyncio.iscoroutinefunction(setup_fn):
                await setup_fn(api)
            else:
                setup_fn(api)
        except Exception:
            log.exception("Plugin %s: setup() failed", name)
            continue

        loaded.append(LoadedPlugin(
            name=name,
            config=plugin_cfg,
            module=module,
            directory=entry,
        ))
        log.info("Plugin %s loaded", name)

    return loaded
