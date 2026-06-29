"""daemon/api/server.py — uvicorn mounting on the daemon loop (plan 07 §1-2).

UDS is the primary transport (07 §2): the data is the user's entire desktop
activity, so it must not be reachable over the network — a filesystem socket with
``chmod 600`` is owner-only.  Optional ``127.0.0.1:<port>`` is for curl/browser
debugging; uvicorn can't bind UDS *and* TCP from one ``Config``, so when both are
requested we run **two ``uvicorn.Server`` tasks sharing the same app** (07 §2).

``ApiServer.serve(stop)`` is one coroutine among the daemon's tasks; it brings up
the server(s), waits for ``stop``, then shuts them down cleanly.
"""
from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from .app import DaemonContext, create_app

log = logging.getLogger("dovw.api")


class ApiServer:
    def __init__(self, ctx: DaemonContext):
        self.ctx = ctx
        self.s = ctx.settings
        self.app = create_app(ctx)
        self._servers: list[uvicorn.Server] = []

    def _make(self, **bind) -> uvicorn.Server:
        config = uvicorn.Config(self.app, log_level=self.s.log_level, lifespan="on",
                                access_log=False, **bind)
        return uvicorn.Server(config)

    async def serve(self, stop: asyncio.Event) -> None:
        uds = str(self.s.uds_path)
        self._unlink_stale(uds)
        os.makedirs(os.path.dirname(uds) or ".", exist_ok=True)
        self._servers.append(self._make(uds=uds))
        if self.s.tcp_enabled:
            self._servers.append(self._make(host=self.s.tcp_host, port=self.s.tcp_port))

        tasks = [asyncio.create_task(srv.serve(), name=f"uvicorn-{i}")
                 for i, srv in enumerate(self._servers)]

        # uvicorn binds the UDS during startup; chmod it owner-only once it exists.
        await self._chmod_uds_when_ready(uds)
        log.info("API listening on uds=%s%s", uds,
                 f" tcp={self.s.tcp_host}:{self.s.tcp_port}" if self.s.tcp_enabled else "")

        await stop.wait()
        for srv in self._servers:
            srv.should_exit = True
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception) as exc:
                log.debug("uvicorn task ended: %s", exc)
        self._unlink_stale(uds)

    async def _chmod_uds_when_ready(self, uds: str) -> None:
        for _ in range(50):                       # up to ~5s for uvicorn to bind
            if os.path.exists(uds):
                try:
                    os.chmod(uds, self.s.uds_mode)   # 0o600 — owner-only (07 §2)
                except OSError as exc:
                    log.warning("chmod %s failed: %s", uds, exc)
                return
            await asyncio.sleep(0.1)
        log.warning("UDS %s did not appear; not chmod'd", uds)

    @staticmethod
    def _unlink_stale(uds: str) -> None:
        try:
            if os.path.exists(uds):
                os.unlink(uds)
        except OSError:
            pass
