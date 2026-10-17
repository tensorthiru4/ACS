"""Live progress for slow console calls (chat turns, policy compiles), read by polling.

API Gateway buffers response bodies, so a streamed reply reaches the browser only once
it is complete. Instead, the console sends `X-Progress-Id: <random id>` with the call;
while the request does its work, progress events and a heartbeat are written to
`console_progress/{id}`, and the console polls `GET /v1/console/progress/{id}`:

  {"events": [{"seq": 1, "type": "status", "text": ..., "elapsed_s": 0.0}, ...],
   "elapsed_s": 12.5, "done": false}

The work stays inside the request on purpose: Cloud Run throttles the CPU of an
instance that is not serving a request (`cpu_idle`), so work after a 202 would stall.
The call itself still returns its ordinary JSON body (or error) when it finishes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request

from ..clients.store import Store

logger = logging.getLogger(__name__)

HEADER = "X-Progress-Id"
HEARTBEAT_S = 5.0
MIN_WRITE_INTERVAL_S = 1.0  # coalesce bursts of events into one Firestore write
MAX_EVENTS = 40
_ID = re.compile(r"^[A-Za-z0-9-]{16,64}$")

Emit = Callable[[dict[str, Any]], None]


def progress_id(request: Request) -> str | None:
    value = request.headers.get(HEADER)
    if value is None:
        return None
    if not _ID.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"invalid {HEADER} header")
    return value


async def run_tracked[T](
    store: Store, owner: str, pid: str | None, work: Callable[[Emit], Awaitable[T]]
) -> T:
    """Run `work(emit)`, publishing what it emits under `pid` (when the client sent one)."""
    if pid is None:
        return await work(lambda _: None)
    existing = await store.get_progress(pid)
    if existing is not None and existing.get("owner") != owner:
        raise HTTPException(status_code=409, detail=f"{HEADER} is already in use")

    started = time.monotonic()
    events: list[dict[str, Any]] = []
    changed = asyncio.Event()

    def elapsed() -> float:
        return round(time.monotonic() - started, 1)

    def emit(event: dict[str, Any]) -> None:
        seq = events[-1]["seq"] + 1 if events else 1
        events.append({**event, "seq": seq, "elapsed_s": elapsed()})
        del events[:-MAX_EVENTS]
        changed.set()

    async def publish(done: bool) -> None:
        try:
            await store.put_progress(
                pid, {"owner": owner, "events": list(events), "elapsed_s": elapsed(), "done": done}
            )
        except Exception:  # progress must never break the call it describes
            logger.warning("progress write failed", exc_info=True)

    async def heartbeat() -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), HEARTBEAT_S)
            changed.clear()
            await publish(done=False)
            await asyncio.sleep(MIN_WRITE_INTERVAL_S)

    writer = asyncio.create_task(heartbeat())
    try:
        return await work(emit)
    finally:
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
        await publish(done=True)


async def read_progress(store: Store, owner: str, pid: str) -> dict[str, Any]:
    if not _ID.fullmatch(pid):
        raise HTTPException(status_code=404, detail="not found")
    record = await store.get_progress(pid)
    # Another user's id is simply not found.
    if record is None or record.get("owner") != owner:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "events": record.get("events", []),
        "elapsed_s": record.get("elapsed_s", 0.0),
        "done": bool(record.get("done")),
    }
