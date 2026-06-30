"""daemon/db/store.py — async SQLite access for the daemon.

Owns the single **writer** connection (WAL; one writer per SQLite, 01 §3) and a
read-only connection for queries.  Two write paths:

* ``enqueue(sql, params)`` + ``writer_loop()`` — batched, for high-rate event
  inserts (keyboard, window_captures…).  Drains up to ``WRITE_QUEUE_MAX_N`` ops or
  waits ``WRITE_QUEUE_MAX_WAIT_S`` then commits once.
* ``execute(sql, params)`` — immediate, awaited, returns ``lastrowid``; used for
  identity/registry rows where we need the id right back.

Reads go through ``fetchall``/``fetchone`` on a ``query_only`` connection so a
slow query can never stall the writer.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import namedtuple
from pathlib import Path

import aiosqlite

log = logging.getLogger("dovw.store")

WriteOp = namedtuple("WriteOp", "sql params")

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "1"


class Store:
    def __init__(self, settings):
        self.s = settings
        self._writer: aiosqlite.Connection | None = None
        self._reader: aiosqlite.Connection | None = None
        self._wq: asyncio.Queue[WriteOp] = asyncio.Queue(maxsize=settings.write_queue_maxsize)
        self._writer_lock = asyncio.Lock()   # serialize execute() vs batch commit
        self._dropped = 0                     # back-pressure counter

    # ───────────────────────── lifecycle ─────────────────────────
    async def open(self) -> None:
        self.s.data_dir.mkdir(parents=True, exist_ok=True)
        log.info("opening db at %s", self.s.db_path)
        self._writer = await aiosqlite.connect(self.s.db_path)
        await self._apply_pragmas(self._writer, writer=True)
        await self._init_schema(self._writer)

        self._reader = await aiosqlite.connect(self.s.db_path)
        self._reader.row_factory = aiosqlite.Row
        await self._apply_pragmas(self._reader, writer=False)
        await self._reader.execute("PRAGMA query_only=ON;")
        log.info("db open with writer=%s reader=%s",
                 self._writer is not None, self._reader is not None)

    async def _apply_pragmas(self, db: aiosqlite.Connection, *, writer: bool) -> None:
        await db.execute(f"PRAGMA journal_mode={self.s.sqlite_journal_mode};")
        await db.execute(f"PRAGMA synchronous={self.s.sqlite_synchronous};")
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute(f"PRAGMA busy_timeout={self.s.sqlite_busy_timeout_ms};")

    async def _init_schema(self, db: aiosqlite.Connection) -> None:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8").replace("{tokenizer}", self.s.fts_tokenizer)
        await db.executescript(sql)
        await db.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
        await db.commit()

    async def close(self) -> None:
        log.info("closing db")
        try:
            await self.flush()
        finally:
            if self._writer:
                await self._writer.close()
            if self._reader:
                await self._reader.close()
        log.info("db closed")

    # ───────────────────────── writes ─────────────────────────
    async def execute(self, sql: str, params=()) -> int:
        """Immediate write; returns lastrowid. For id-returning inserts/updates."""
        async with self._writer_lock:
            cur = await self._writer.execute(sql, params)
            await self._writer.commit()
            return cur.lastrowid

    def enqueue(self, sql: str, params=()) -> None:
        """Queue a write for the batched writer_loop (call on the loop thread)."""
        try:
            self._wq.put_nowait(WriteOp(sql, params))
        except asyncio.QueueFull:
            self._dropped += 1   # 01 §2: never block the producer
            log.warning("write queue full; dropped=%d", self._dropped)

    async def _drain(self, max_n: int, max_wait: float) -> list[WriteOp]:
        batch = [await self._wq.get()]            # block for the first
        deadline = time.monotonic() + max_wait
        while len(batch) < max_n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self._wq.get(), remaining))
            except asyncio.TimeoutError:
                break
        return batch

    async def writer_loop(self, stop: asyncio.Event) -> None:
        """Drain → executemany grouped by identical SQL → commit. Runs until stop."""
        log.debug("writer_loop started")
        while not stop.is_set():
            try:
                batch = await asyncio.wait_for(
                    self._drain(self.s.write_queue_max_n, self.s.write_queue_max_wait_s),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue
            await self._commit_batch(batch)
        log.debug("writer_loop stopped")

    async def _commit_batch(self, batch: list[WriteOp]) -> None:
        if not batch:
            return
        log.debug("committing batch of %d writes", len(batch))
        async with self._writer_lock:
            # group consecutive ops with identical SQL → one executemany
            i = 0
            while i < len(batch):
                sql = batch[i].sql
                j = i
                params = []
                while j < len(batch) and batch[j].sql == sql:
                    params.append(batch[j].params)
                    j += 1
                try:
                    if len(params) == 1:
                        await self._writer.execute(sql, params[0])
                    else:
                        await self._writer.executemany(sql, params)
                except Exception as exc:   # 01 §7: quarantine, don't crash the writer
                    log.warning("write failed (sql=%s): %s", sql[:80], exc)
                i = j
            await self._writer.commit()
        log.debug("batch committed")

    async def flush(self) -> None:
        """Drain everything still queued and commit (shutdown / tests)."""
        pending: list[WriteOp] = []
        while not self._wq.empty():
            pending.append(self._wq.get_nowait())
        await self._commit_batch(pending)

    @property
    def dropped(self) -> int:
        return self._dropped

    # ───────────────────────── reads ─────────────────────────
    async def fetchall(self, sql: str, params=()) -> list[aiosqlite.Row]:
        async with self._reader.execute(sql, params) as cur:
            return await cur.fetchall()

    async def fetchone(self, sql: str, params=()):
        async with self._reader.execute(sql, params) as cur:
            return await cur.fetchone()
