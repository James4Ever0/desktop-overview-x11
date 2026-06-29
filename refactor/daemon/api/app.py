"""daemon/api/app.py — FastAPI app factory + shared daemon context (plan 07 §1, §6).

The API runs **in-process** on the daemon's asyncio loop, so handlers read the
same ``Store`` (read-only connection), the live ``WindowRegistry``, and other
in-memory daemon state directly — no IPC (07 §1).  We hang a single
:class:`DaemonContext` off ``app.state`` and the routes pull it via a dependency.

``create_app(ctx)`` is called once by ``server.py`` after the daemon core is up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request


@dataclass
class DaemonContext:
    """Everything the API handlers need from the running daemon (07 §1)."""
    store: Any                      # db.store.Store (read helpers)
    registry: Any                   # windows.WindowRegistry (focus/liveness)
    settings: Any                   # config.Settings
    runtime: Any = None             # runtime.Runtime (executor for activate, queue depth)
    identity: Any = None            # identity.Identity (session_key, daemon_boot_id)
    window_captures: Any = None          # window_captures.WindowCaptureScheduler (refresh)
    handlers: Any = None            # handlers.EventHandlers (current vdesktop, kbd toggle target)
    stats: dict = field(default_factory=dict)   # last_full_sweep, etc.

    @property
    def session_key(self) -> str | None:
        return self.identity.session_key if self.identity else self.registry.session_key


def create_app(ctx: DaemonContext) -> FastAPI:
    app = FastAPI(title="desktop-overview daemon", version="1", docs_url="/docs")
    app.state.ctx = ctx
    from .routes import router
    app.include_router(router)
    return app


def get_ctx(request: Request) -> DaemonContext:
    return request.app.state.ctx
