"""Accepted StableTextEvent -> overlay action foundation (Phase 2K.1).

Pure stdlib domain semantics: turns already-accepted OCR transport events into
deterministic ``OverlayTextAction`` values. It does NOT call the renderer, touch
sockets, use asyncio, or know about OverlayManager/QAM/Decky.

Dependency direction: this module consumes the accepted-event/observer seam
defined by ``backend.ocr_transport``; ``backend.ocr_transport`` must never import
this module. This module must never import overlay_manager / overlay.renderer /
asyncio / socket / subprocess / decky.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.ocr_transport import AcceptedStableTextEvent


@dataclass(frozen=True)
class OverlayTextAction:
    """Deterministic overlay action derived from one accepted source event.

    ``action_seq`` is coordinator-session-local and strictly increasing. For
    ``kind=hide`` the text is empty. No styling/geometry fields exist yet.
    """

    action_seq: int
    kind: str  # "text" | "hide"
    worker_session_id: str
    source_event_seq: int
    text: str
    confidence: Optional[float] = None
    source_seq: Optional[int] = None
    timestamp_monotonic: Optional[float] = None


@dataclass
class OverlayTextStats:
    inputs_received: int = 0
    inputs_rejected: int = 0
    text_actions: int = 0
    hide_actions: int = 0
    last_error: Optional[str] = None


class OverlayTextCoordinator:
    """Synchronous, session-scoped, deterministic overlay-text coordinator.

    Source identity is ``(worker_session_id, event_seq)``. It never switches
    sessions implicitly and never emits an action for rejected input.
    """

    def __init__(self) -> None:
        self._session_id: Optional[str] = None
        self._action_seq = 0
        self._last_source_event_seq = 0
        self._latest: Optional[OverlayTextAction] = None
        self._stats = OverlayTextStats()

    # -- session -----------------------------------------------------------

    def begin_session(self, worker_session_id: str) -> None:
        """Reset all source-order/action authority to a new worker session.

        Intentionally emits NO hide action: whether a real renderer should hide
        immediately on OCR restart is a product/lifecycle decision deferred to
        the production delivery gate.
        """
        if not isinstance(worker_session_id, str) or not worker_session_id:
            raise ValueError("missing_worker_session_id")
        self._session_id = worker_session_id
        self._action_seq = 0
        self._last_source_event_seq = 0
        self._latest = None
        self._stats = OverlayTextStats()

    def reset(self) -> None:
        self._session_id = None
        self._action_seq = 0
        self._last_source_event_seq = 0
        self._latest = None
        self._stats = OverlayTextStats()

    # -- introspection -----------------------------------------------------

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def latest_action(self) -> Optional[OverlayTextAction]:
        return self._latest

    def status(self) -> dict:
        latest = self._latest
        return {
            "worker_session_id": self._session_id,
            "action_seq": self._action_seq,
            "last_source_event_seq": self._last_source_event_seq,
            "latest_kind": latest.kind if latest is not None else None,
            "latest_text": latest.text if latest is not None else "",
            "latest_source_event_seq": latest.source_event_seq if latest is not None else None,
            "inputs_received": self._stats.inputs_received,
            "inputs_rejected": self._stats.inputs_rejected,
            "text_actions": self._stats.text_actions,
            "hide_actions": self._stats.hide_actions,
            "last_error": self._stats.last_error,
        }

    # -- consume -----------------------------------------------------------

    def consume(self, event: AcceptedStableTextEvent) -> Optional[OverlayTextAction]:
        self._stats.inputs_received += 1

        if self._session_id is None:
            self._reject("no_session")
            return None
        if event.worker_session_id != self._session_id:
            self._reject("session_mismatch")
            return None
        if event.event_seq <= self._last_source_event_seq:
            self._reject("stale_or_duplicate")
            return None

        if event.kind == "text":
            if not event.text:
                self._reject("empty_text")
                return None
            action = self._emit(kind="text", text=event.text, event=event)
            self._stats.text_actions += 1
        elif event.kind == "clear":
            action = self._emit(kind="hide", text="", event=event)
            self._stats.hide_actions += 1
        else:
            self._reject("invalid_kind")
            return None

        self._last_source_event_seq = event.event_seq
        self._stats.last_error = None
        return action

    # -- internals ---------------------------------------------------------

    def _emit(self, *, kind: str, text: str, event: AcceptedStableTextEvent) -> OverlayTextAction:
        self._action_seq += 1
        action = OverlayTextAction(
            action_seq=self._action_seq,
            kind=kind,
            worker_session_id=event.worker_session_id,
            source_event_seq=event.event_seq,
            text=text,
            confidence=event.confidence,
            source_seq=event.source_seq,
            timestamp_monotonic=event.timestamp_monotonic,
        )
        self._latest = action
        return action

    def _reject(self, reason: str) -> None:
        self._stats.inputs_rejected += 1
        self._stats.last_error = reason


class OverlayTextTransportObserver:
    """Thin synchronous adapter: transport observer seam -> OverlayTextCoordinator.

    Injectable/test-only. No renderer access, no async, no loop handoff, and it
    is NOT installed by production engine construction in Phase 2K.1.
    """

    def __init__(self, coordinator: OverlayTextCoordinator) -> None:
        self._coordinator = coordinator

    def begin_session(self, worker_session_id: str) -> None:
        self._coordinator.begin_session(worker_session_id)

    def on_accepted_event(self, event: AcceptedStableTextEvent) -> None:
        self._coordinator.consume(event)
