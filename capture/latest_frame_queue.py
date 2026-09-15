"""Bounded latest-frame mailbox (capacity exactly 1).

Semantics:
- the newest frame always wins; a stale pending frame is dropped, never queued;
- the producer never blocks on a full queue;
- a frame already taken by a consumer belongs to the consumer and is never
  mutated or cancelled by the producer;
- the queue never holds more than one pending frame, so memory stays bounded.

Concurrency model: asyncio, single event loop, one producer. ``clear`` is
synchronous and must not be called concurrently with an in-flight ``get`` from
another thread; within a single loop it is safe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from .frame import CaptureFrame


@dataclass
class LatestFrameStats:
    produced: int = 0
    replaced: int = 0
    consumed: int = 0
    pending: int = 0
    cleared: int = 0
    capture_errors: int = 0
    max_pending: int = 0


class LatestFrameQueue:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self._pending: Optional[CaptureFrame] = None
        self._produced = 0
        self._replaced = 0
        self._consumed = 0
        self._cleared = 0
        self._capture_errors = 0
        self._max_pending = 0

    async def put_latest(self, frame: CaptureFrame) -> None:
        async with self._lock:
            self._produced += 1
            if self._pending is not None:
                self._replaced += 1
            self._pending = frame
            if self._max_pending < 1:
                self._max_pending = 1
            self._event.set()

    async def get(self) -> CaptureFrame:
        while True:
            async with self._lock:
                if self._pending is not None:
                    frame = self._pending
                    self._pending = None
                    self._consumed += 1
                    self._event.clear()
                    return frame
                self._event.clear()
            await self._event.wait()

    def clear(self) -> None:
        if self._pending is not None:
            self._pending = None
            self._cleared += 1
        self._event.clear()

    def note_capture_error(self) -> None:
        self._capture_errors += 1

    @property
    def pending(self) -> int:
        return 1 if self._pending is not None else 0

    def stats(self) -> LatestFrameStats:
        return LatestFrameStats(
            produced=self._produced,
            replaced=self._replaced,
            consumed=self._consumed,
            pending=self.pending,
            cleared=self._cleared,
            capture_errors=self._capture_errors,
            max_pending=self._max_pending,
        )
