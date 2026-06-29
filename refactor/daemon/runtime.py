"""daemon/runtime.py — the asyncio core that ties collectors to the DB (plan 01).

Hard requirement: the loop thread never blocks.  Blocking X11 sources live in
their own threads and hand events back through :meth:`Runtime.emit`, which uses
``loop.call_soon_threadsafe`` (the only safe way to feed an ``asyncio.Queue``
from another thread, 01 §2).

Flow:  collector thread → emit() → bounded queue → dispatch_loop() → handler →
store.enqueue() → store.writer_loop() batches & commits (01 §3).

Reads bypass all of this via ``store.fetchall`` on a read-only connection, so a
slow API query can never stall the writer.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

# Events safe to drop first under back-pressure (01 §2): raw high-rate keystrokes.
_LOW_VALUE_KINDS = frozenset({"key"})


class Runtime:
    def __init__(self, store, settings):
        self.store = store
        self.s = settings
        self.loop: asyncio.AbstractEventLoop | None = None
        self._q: asyncio.Queue[dict] = asyncio.Queue(maxsize=settings.event_queue_maxsize)
        self._handlers: dict[str, callable] = {}
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._writer_stop = asyncio.Event()
        self.executor = ThreadPoolExecutor(max_workers=settings.executor_max_workers,
                                           thread_name_prefix="dovw-exec")
        self.dropped = 0

    # ───────────────────────── registration ─────────────────────────
    def register(self, kind: str, handler) -> None:
        """Bind an event ``kind`` to an async handler ``handler(ev) -> None``."""
        self._handlers[kind] = handler

    # ───────────────────────── thread → loop shim (01 §2) ─────────────────────────
    def emit(self, event: dict) -> None:
        """Called from collector threads. Schedules the enqueue on the loop thread."""
        loop = self.loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._enqueue, event)

    def _enqueue(self, event: dict) -> None:
        """Runs on the loop thread. Never blocks the producer (01 §2)."""
        try:
            self._q.put_nowait(event)
        except asyncio.QueueFull:
            # Drop lowest-value first: discard the incoming low-value event, or
            # evict one queued low-value event to make room for a richer one.
            self.dropped += 1
            if event.get("kind") not in _LOW_VALUE_KINDS and self._evict_low_value():
                try:
                    self._q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    def _evict_low_value(self) -> bool:
        """Drop one queued low-value event if present (best-effort)."""
        dq = self._q._queue  # deque backing asyncio.Queue
        for i, ev in enumerate(dq):
            if ev.get("kind") in _LOW_VALUE_KINDS:
                del dq[i]
                return True
        return False

    # ───────────────────────── dispatch (01 §2) ─────────────────────────
    async def dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ev = await asyncio.wait_for(self._q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            handler = self._handlers.get(ev.get("kind"))
            if handler is None:
                continue
            try:
                await handler(ev)          # validate, enrich, enqueue DB write
            except Exception as exc:        # 01 §7: isolate; one bad event ≠ crash
                print(f"[dispatch] handler for {ev.get('kind')!r} failed: {exc}")

    # ───────────────────────── lifecycle ─────────────────────────
    async def start_core(self) -> None:
        """Start the writer + dispatch tasks (01 §5 step 3)."""
        self.loop = asyncio.get_running_loop()
        self._tasks.append(asyncio.create_task(
            self.store.writer_loop(self._writer_stop), name="db_writer"))
        self._tasks.append(asyncio.create_task(self.dispatch_loop(), name="dispatch"))

    def add_task(self, coro, name: str | None = None) -> asyncio.Task:
        """Register an extra long-lived task (collectors/aggregator/scheduler)."""
        t = asyncio.create_task(coro, name=name)
        self._tasks.append(t)
        return t

    async def run_in_executor(self, fn, *args):
        return await self.loop.run_in_executor(self.executor, fn, *args)

    async def stop(self) -> None:
        """Graceful shutdown (01 §5): stop dispatch, drain writer, close store."""
        self._stop.set()
        self._writer_stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self.store.flush()
        self.executor.shutdown(wait=False, cancel_futures=True)
