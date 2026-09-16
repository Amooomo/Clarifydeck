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
    actions_dropped_unknown_region: int = 0
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
        # Per-region newest-wins pending (never cross-region overwrite).
        self._pending_by_region: dict[str, OverlayTextAction] = {}
        # Start-time region layout snapshot: region_id -> (x, y, w, h).
        self._region_layout: dict[str, tuple] = {}
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

    def submit_region(self, action: OverlayTextAction) -> None:
        """Store the newest action for one region and schedule a drain.

        Per-region coalescing: a newer action for the same region replaces the
        pending one, but different regions never overwrite each other.
        """
        region_id = action.region_id
        if not region_id:
            return
        with self._lock:
            if self._closed:
                self._stats.actions_dropped_closed += 1
                return
            self._stats.actions_submitted += 1
            if self._pending_by_region.get(region_id) is not None:
                self._stats.actions_coalesced += 1
            self._pending_by_region[region_id] = action
            if self._drain_scheduled:
                return
            self._drain_scheduled = True
        self._schedule_drain()

    def set_region_layout(self, layout: Optional[dict]) -> None:
        """Set the immutable start-time region geometry snapshot."""
        with self._lock:
            self._region_layout = {
                str(key): tuple(value) for key, value in (layout or {}).items()
            }

    def clear_region_pending(self) -> None:
        with self._lock:
            self._pending_by_region.clear()

    def schedule_clear_region_text(self) -> None:
        """Ask the main loop to clear all region text blocks (new session)."""
        try:
            self._loop.call_soon_threadsafe(self._on_clear_region_text)
        except Exception:
            pass

    def _on_clear_region_text(self) -> None:
        try:
            self._loop.create_task(self._clear_region_text())
        except Exception:
            pass

    async def _clear_region_text(self) -> None:
        manager = self._get_overlay_manager()
        if manager is None:
            return
        try:
            await manager.clear_all_region_text()
        except Exception as exc:
            self._stats.delivery_errors += 1
            self._stats.last_delivery_error = f"clear_region_text:{type(exc).__name__}"

    def _schedule_drain(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._on_loop_schedule)
        except Exception as exc:  # loop closed / not running
            with self._lock:
                self._drain_scheduled = False
                self._pending = None
                self._pending_by_region.clear()
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
            self._pending_by_region.clear()

    def close(self) -> None:
        """Idempotent non-blocking close: drop pending, reject new submissions."""
        with self._lock:
            self._closed = True
            self._pending = None
            self._pending_by_region.clear()
            self._drain_scheduled = False

    def status(self) -> dict:
        with self._lock:
            return {
                "closed": self._closed,
                "session_id": self._session_id,
                "pending": self._pending is not None,
                "pending_regions": len(self._pending_by_region),
                "region_layout_count": len(self._region_layout),
                "drain_scheduled": self._drain_scheduled,
                "actions_submitted": self._stats.actions_submitted,
                "actions_delivered": self._stats.actions_delivered,
                "actions_dropped_disabled": self._stats.actions_dropped_disabled,
                "actions_dropped_session": self._stats.actions_dropped_session,
                "actions_dropped_closed": self._stats.actions_dropped_closed,
                "actions_dropped_unknown_region": self._stats.actions_dropped_unknown_region,
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
                self._pending_by_region.clear()
                return
        try:
            self._loop.create_task(self._drain())
        except Exception as exc:
            with self._lock:
                self._drain_scheduled = False
                self._pending = None
                self._pending_by_region.clear()
            self._stats.delivery_errors += 1
            self._stats.last_delivery_error = f"create_task_failed:{type(exc).__name__}"

    async def _drain(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    self._pending = None
                    self._pending_by_region.clear()
                    self._drain_scheduled = False
                    return
                action = self._pending
                self._pending = None
                region_action = None
                if self._pending_by_region:
                    region_id = next(iter(self._pending_by_region))
                    region_action = self._pending_by_region.pop(region_id)
                if action is None and region_action is None:
                    self._drain_scheduled = False
                    return
            try:
                if action is not None:
                    await self._deliver(action)
                if region_action is not None:
                    await self._deliver_region(region_action)
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

    async def _deliver_region(self, action: OverlayTextAction) -> None:
        with self._lock:
            closed = self._closed
            session = self._session_id
            rect = self._region_layout.get(action.region_id) if action.region_id else None
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
        if rect is None:
            # Unknown/disabled region: geometry is unavailable, so do not render.
            self._stats.actions_dropped_unknown_region += 1
            return
        if action.kind == "hide":
            await manager.hide_region_text(action.region_id)
        else:
            await manager.set_region_text(action.region_id, rect, action.text)
        self._stats.actions_delivered += 1


MODE_UNKNOWN = "UNKNOWN"
MODE_LEGACY_V1 = "LEGACY_V1"
MODE_REGION_V2 = "REGION_V2"


class OverlayDeliveryObserver:
    """Synchronous transport observer: coordinator -> delivery.

    Installed on the authoritative shared ``OCRTransportReceiver`` by production
    wiring. Fast and non-blocking; never touches ``OverlayManager`` directly.

    Session mode: the first accepted v2 event switches the overlay to
    ``REGION_V2``; thereafter the paired primary v1 compatibility projection is
    ignored by the overlay (it still updates backend legacy/QAM state). A
    true v1-only session stays ``LEGACY_V1`` and keeps the legacy single block.
    """

    def __init__(self, coordinator: OverlayTextCoordinator, delivery: MainLoopOverlayDelivery) -> None:
        self._coordinator = coordinator
        self._delivery = delivery
        self._mode = MODE_UNKNOWN

    @property
    def mode(self) -> str:
        return self._mode

    def begin_session(self, worker_session_id: str) -> None:
        self._mode = MODE_UNKNOWN
        self._coordinator.begin_session(worker_session_id)
        self._delivery.set_session(worker_session_id)
        self._delivery.clear_region_pending()
        self._delivery.schedule_clear_region_text()

    def on_accepted_event(self, event: Any) -> None:
        region_id = getattr(event, "region_id", None)
        if region_id is not None:
            self._mode = MODE_REGION_V2
        elif self._mode == MODE_REGION_V2:
            # Primary v1 compatibility projection: backend legacy/QAM only.
            return
        elif self._mode == MODE_UNKNOWN:
            self._mode = MODE_LEGACY_V1

        action = self._coordinator.consume(event)
        if action is None:
            return
        if action.region_id is not None:
            self._delivery.submit_region(action)
        else:
            self._delivery.submit(action)
