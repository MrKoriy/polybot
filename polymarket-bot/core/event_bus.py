import asyncio
from collections import defaultdict
from typing import Any, Callable, Coroutine

import structlog

log = structlog.get_logger()

EventHandler = Callable[..., Coroutine[Any, Any, None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event: str, handler: EventHandler) -> None:
        self._handlers[event].append(handler)

    def unsubscribe(self, event: str, handler: EventHandler) -> None:
        self._handlers[event] = [h for h in self._handlers[event] if h is not handler]

    async def publish(self, event: str, **kwargs: Any) -> None:
        handlers = self._handlers.get(event, [])
        if not handlers:
            return

        tasks = [self._safe_call(handler, event, **kwargs) for handler in handlers]
        await asyncio.gather(*tasks)

    async def _safe_call(self, handler: EventHandler, event: str, **kwargs: Any) -> None:
        try:
            await handler(**kwargs)
        except Exception:
            log.exception("event_handler_error", event=event, handler=handler.__name__)
