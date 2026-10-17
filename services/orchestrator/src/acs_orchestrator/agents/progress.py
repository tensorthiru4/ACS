"""Live progress notes for the request being served (see api/live_progress.py).

Code deep inside an agent turn, such as the model fallback, calls `report()`; the
endpoint that bound a sink publishes each note for the console to poll. With no sink
bound (the plain JSON endpoints, A2A), notes are dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

Sink = Callable[[dict[str, Any]], None]
_sink: ContextVar[Sink | None] = ContextVar("acs_progress_sink", default=None)


def report(event: dict[str, Any]) -> None:
    sink = _sink.get()
    if sink is None:
        return
    try:
        sink(event)
    except Exception:  # progress must never break the turn it describes
        logger.exception("progress sink failed")


@contextmanager
def reporting_to(sink: Sink) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)
