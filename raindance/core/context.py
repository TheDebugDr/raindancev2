"""Shared services handed to every plugin's render() method.

A plugin never imports the app or other plugins directly — it only touches this
context. That's what keeps tools decoupled and drop-in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class AppContext:
    settings: Any                       # core.settings.Settings
    engine: Any                         # core.engine.CheckoutEngine (legacy single-run)
    hub: Any                            # core.notifications.NotificationHub
    bus: Any                            # core.events.EventBus
    store: Any = None                   # core.store.ProductStore
    search: Any = None                  # core.search.SearchService
    scanner: Any = None                 # core.scanner.Scanner
    browser_factory: Any = None         # evasion.browser_factory.BrowserFactory
    proxy_manager: Any = None           # evasion.proxy_manager.ProxyManager
    # The NAMED pools task runs actually pull from (the standalone proxy_manager
    # above is the Evasion page's editable pool and is a different object).
    # Optional: plugins fall back to ctx.orchestrator.proxy_groups when a host
    # builds an AppContext without it.
    proxy_groups: Any = None            # evasion.proxy_groups.ProxyGroupManager
    catalog: Any = None                 # core.catalog.CatalogStore
    queue: Any = None                   # core.execute_queue.ExecuteQueue
    links: Any = None                   # core.link_index.LinkIndex
    orchestrator: Any = None            # tasks.orchestrator.Orchestrator
    profile_store: Any = None           # profiles.store.ProfileStore
    task_store: Any = None              # tasks.store.TaskStore
    refresh: Optional[Callable[[], None]] = None   # re-render the active panel
