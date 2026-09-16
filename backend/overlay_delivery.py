"""Thread-safe main-loop delivery of overlay actions (Phase 2K.2).

Bridges the synchronous OCR transport observer (running on the ``ocr-worker-stdout``
thread) to the existing async ``OverlayManager`` on the plugin/main asyncio loop.

Safety properties:
- the worker thread never awaits the renderer; submission only schedules a
  callback onto the already-running main loop via ``loop.call_soon_threadsafe``;
- bounded newest-wins pending state (capacity 1), so the reader thread can never
  be back-pressured by renderer IPC;
- serial delivery: at most one ``OverlayManager.update/hide`` runs at a time;
- the renderer must already be explicitly enabled; this layer never enables or
  starts it and never creates an ``OverlayManager``;
- delivery failures are isolated and cannot affect OCR acceptance.

Pure stdlib: imports no OCR runtime/native modules and does not import the
renderer implementation (only a narrow duck-typed manager accessor is used).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from backend.overlay_text import OverlayTextAction, OverlayTextCoordinator


def default_is_running(manager: Any) -> bool:
    """Best-effort predicate: an existing manager is usable only when RUNNING."""
    try:
        status = manager.status()
    except Exception:
        return False
    return bool(status.get("enabled"))


@dataclass
class OverlayDeliveryStats:
    actions_submitted: int = 0
    actions_delivered: int = 0
    actions_dropped_disabled: int = 0
    actions_dropped_session: int = 0
    actions_dropped_closed: int = 0
    actions_coalesced: int = 0
    delivery_errors: int = 0
    last_delivery_error: Optional[str] = None


class MainLoopOverlayDelivery:
    """Non-blocking, bounded, serial bridge from observer thread to main loop."""

    def __init__(
        self,
        loop: Any,
        get_overlay_manager: Callable[[], Any],
        *,
        is_running: Optional[Callable[[Any], bool]] = None,
    ) -> None:
        self._loop = loop
        self._get_overlay_manager = get_overlay_manager
        self._is_running = is_running or default_is_running
        self._lock = threading.Lock()
        self._pending: Optional[OverlayTextAction] = None
        self._drain_scheduled = False
        self._closed = False
        self._session_id: Optional[str] = None
        self._stats = OverlayDeliveryStats()

    # -- thread-safe submission -------------------------------------------

    def submit(self, action: OverlayTextAction) -> None:
        """Store newest action and schedule exactly one main-loop drain.

        Never blocks on the renderer and never raises into the caller.
        """
        with self._lock:
            if self._closed:
                self._stats.actions_dropped_closed += 1
                return
            self._stats.actions_submitted += 1
            if self._pending is not None:
                self._stats.actions_coalesced += 1
            self._pending = action
            if self._drain_scheduled:
                return
            self._drain_scheduled = True
        try:
            self._loop.call_soon_threadsafe(self._on_loop_schedule)
        except Exception as exc:  # loop closed / not running
            with self._lock:
                self._drain_scheduled = False
                if self._pending is not None:
                    self._pending = None
                    self._stats.actions_dropped_closed += 1
            self._stats.delivery_errors += 1
            self._stats.last_delivery_error = f"schedule_failed:{type(exc).__name__}"

    def set_session(self, session_id: str) -> None:
        """Update the authoritative session; drop any not-yet-delivered old action."""
        with self._lock:
            if self._session_id == session_id:
                return
            self._session_id = session_id
            if self._pending is not None and self._pending.worker_session_id != session_id:
                self._pending = None
                self._stats.actions_dropped_session += 1

    def close(self) -> None:
        """Idempotent non-blocking close: drop pending, reject new submissions."""
        with self._lock:
            self._closed = True
            self._pending = None
            self._drain_scheduled = False

    def status(self) -> dict:
        with self._lock:
            return {
                "closed": self._closed,
                "session_id": self._session_id,
                "pending": self._pending is not None,
                "drain_scheduled": self._drain_scheduled,
                "actions_submitted": self._stats.actions_submitted,
                "actions_delivered": self._stats.actions_delivered,
                "actions_dropped_disabled": self._stats.actions_dropped_disabled,
                "actions_dropped_session": self._stats.actions_dropped_session,
                "actions_dropped_closed": self._stats.actions_dropped_closed,
                "actions_coalesced": self._stats.actions_coalesced,
                "delivery_errors": self._stats.delivery_errors,
                "last_delivery_error": self._stats.last_delivery_error,
            }

    # -- main-loop internals ----------------------------------------------

    def _on_loop_schedule(self) -> None:
        with self._lock:
            if self._closed:
                self._drain_scheduled = False
                self._pending = None
                return
        try:
            self._loop.create_task(self._drain())
        except Exception as exc:
            with self._lock:
                self._drain_scheduled = False
                self._pending = None
            self._stats.delivery_errors += 1
            self._stats.last_delivery_error = f"create_task_failed:{type(exc).__name__}"

    async def _drain(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    self._pending = None
                    self._drain_scheduled = False
                    return
                action = self._pending
                self._pending = None
                if action is None:
                    self._drain_scheduled = False
                    return
            try:
                await self._deliver(action)
            except Exception as exc:
                self._stats.delivery_errors += 1
                self._stats.last_delivery_error = f"{type(exc).__name__}"

    async def _deliver(self, action: OverlayTextAction) -> None:
        with self._lock:
            closed = self._closed
            session = self._session_id
        if closed:
            self._stats.actions_dropped_closed += 1
            return
        if session is not None and action.worker_session_id != session:
            self._stats.actions_dropped_session += 1
            return
        manager = self._get_overlay_manager()
        if manager is None or not self._is_running(manager):
            self._stats.actions_dropped_disabled += 1
            return
        if action.kind == "hide":
            await manager.hide()
        else:
            await manager.update(action.text)
        self._stats.actions_delivered += 1


class OverlayDeliveryObserver:
    """Synchronous transport observer: coordinator -> delivery.

    Installed on the authoritative shared ``OCRTransportReceiver`` by production
    wiring. Fast and non-blocking; never touches ``OverlayManager`` directly.
    """

    def __init__(self, coordinator: OverlayTextCoordinator, delivery: MainLoopOverlayDelivery) -> None:
        self._coordinator = coordinator
        self._delivery = delivery

    def begin_session(self, worker_session_id: str) -> None:
        self._coordinator.begin_session(worker_session_id)
        self._delivery.set_session(worker_session_id)

    def on_accepted_event(self, event: Any) -> None:
        # Defensive Phase 2L.3 guard: region-tagged (v2) events are not routed to
        # the legacy single-block overlay until multi-block rendering exists.
        if getattr(event, "region_id", None) is not None:
            return
        action = self._coordinator.consume(event)
        if action is not None:
            self._delivery.submit(action)
