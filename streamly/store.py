from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class NotAuthenticated(RuntimeError):
    pass


@dataclass
class Entry(Generic[T]):
    value: T
    expires_at: float


class TTLStore(Generic[T]):
    """Small process-local TTL store.

    Use only for local/single-worker deployments. Production should implement the same
    get/put/delete contract over Redis plus envelope encryption for tokens.
    """

    def __init__(self, ttl_seconds: int, max_entries: int, clock: Callable[[], float] = time.time):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.clock = clock
        self._lock = threading.RLock()
        self._items: dict[str, Entry[T]] = {}

    def put(self, key: str, value: T) -> None:
        now = self.clock()
        with self._lock:
            if len(self._items) >= self.max_entries:
                self._prune_locked(now)
            if len(self._items) >= self.max_entries:
                # Evict the item closest to expiry to cap memory deterministically.
                oldest = min(self._items, key=lambda k: self._items[k].expires_at)
                del self._items[oldest]
            self._items[key] = Entry(value=value, expires_at=now + self.ttl_seconds)

    def get(self, key: str) -> T:
        now = self.clock()
        with self._lock:
            entry = self._items.get(key)
            if entry is None or entry.expires_at <= now:
                self._items.pop(key, None)
                raise NotAuthenticated("Not authenticated")
            entry.expires_at = now + self.ttl_seconds
            return entry.value

    def _prune_locked(self, now: float) -> None:
        for key, entry in list(self._items.items()):
            if entry.expires_at <= now:
                del self._items[key]
