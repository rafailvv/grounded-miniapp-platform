from __future__ import annotations

import asyncio
from dataclasses import dataclass
import threading
from typing import Any

from app.models.protocol import RpcNotificationEnvelopeV2


@dataclass(frozen=True)
class RpcSubscription:
    queue: asyncio.Queue[dict[str, Any]]
    loop: asyncio.AbstractEventLoop


class RpcEventHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: set[RpcSubscription] = set()

    def subscribe(self) -> RpcSubscription:
        subscription = RpcSubscription(queue=asyncio.Queue(), loop=asyncio.get_running_loop())
        with self._lock:
            self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: RpcSubscription) -> None:
        with self._lock:
            self._subscribers.discard(subscription)

    def publish(self, method: str, params: dict[str, Any]) -> None:
        message = RpcNotificationEnvelopeV2(method=method, params=params).model_dump(mode="json", by_alias=True, exclude_none=True)
        with self._lock:
            subscribers = list(self._subscribers)
        for subscription in subscribers:
            subscription.loop.call_soon_threadsafe(subscription.queue.put_nowait, message)
