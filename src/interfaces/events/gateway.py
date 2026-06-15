"""
interfaces/events/gateway.py — in-process event gateway.

Fires registered callbacks on signal events — zero network overhead.
Used by PipelineManager to receive events from each in-process engine.
"""
from __future__ import annotations

import logging
from typing import Callable

from domain.entities.enums import SignalEvent

logger = logging.getLogger(__name__)

EventHandler = Callable[[SignalEvent, dict], None]


class EventGateway:
    """Synchronous callback dispatcher — no network I/O."""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def broadcast(self, event: SignalEvent, payload: dict) -> None:
        for handler in self._handlers:
            try:
                handler(event, payload)
            except Exception as exc:
                logger.warning("EventGateway handler error: %s", exc)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass
