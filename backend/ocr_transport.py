"""Decky backend OCR transport receiver (Phase 2I.1).

Pure stdlib: reads newline-delimited ``StableTextEnvelope`` messages, validates
them strictly, enforces per-session ``event_seq`` monotonicity, and keeps a small
in-memory latest-stable-state model.

This module must never import RapidOCR / numpy / cv2 / onnxruntime / omegaconf /
antlr4; the Decky backend process does not load the OCR runtime.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional, Protocol

from ocr.transport import TransportError, decode_envelope

STATE_IDLE = "idle"
STATE_TEXT = "text"
STATE_CLEAR = "clear"


@dataclass(frozen=True)
class StableOCRState:
    worker_session_id: Optional[str] = None
    last_event_seq: int = 0
    kind: Optional[str] = None
    text: str = ""
    confidence: Optional[float] = None
    source_seq: Optional[int] = None
    timestamp_monotonic: Optional[float] = None


@dataclass(frozen=True)
class AcceptedStableTextEvent:
    """Immutable view of an already-accepted transport event.

    Constructed only after decode/schema/order validation succeeds, so consumers
    never see malformed or rejected input. Exact text is preserved (no
    normalization). Pure backend transport view: no ocr.stabilizer or overlay
    dependency.
    """

    worker_session_id: str
    event_seq: int
    kind: str  # "text" | "clear"
    text: str
    confidence: Optional[float]
    source_seq: Optional[int]
    timestamp_monotonic: Optional[float]


class OCRTransportObserver(Protocol):
    """Optional synchronous post-accept observer.

    Called on the OCR stdout reader thread. Implementations must be synchronous
    and must not block or perform cross-thread async handoff.
    """

    def begin_session(self, worker_session_id: str) -> None:
        ...

    def on_accepted_event(self, event: AcceptedStableTextEvent) -> None:
        ...


@dataclass
class OCRTransportStats:
    transport_messages_received: int = 0
    transport_messages_rejected: int = 0
    transport_out_of_order: int = 0
    transport_text_events: int = 0
    transport_clear_events: int = 0
    last_transport_error: Optional[str] = None
    observer_errors: int = 0
    last_observer_error: Optional[str] = None


class OCRTransportReceiver:
    def __init__(
        self,
        session_id: Optional[str] = None,
        observer: Optional[OCRTransportObserver] = None,
    ) -> None:
        self._session_id = session_id or uuid.uuid4().hex
        self._state = StableOCRState(worker_session_id=self._session_id)
        self._stats = OCRTransportStats()
        self._observer = observer

    def begin_session(self, session_id: Optional[str] = None) -> StableOCRState:
        """Start a new worker session: reset the event_seq boundary and counters.

        The receiver object may be reused across real worker starts (Phase 2I.3.1),
        so a fresh logical session must explicitly reset both the stable state and
        the transport counters rather than relying on a newly allocated receiver.

        The optional observer is notified only AFTER the authoritative reset, and
        observer failures are isolated so they cannot affect OCR worker startup.
        """
        self._session_id = session_id or uuid.uuid4().hex
        self._state = StableOCRState(worker_session_id=self._session_id)
        self._stats = OCRTransportStats()
        self._notify_begin_session(self._session_id)
        return self._state

    def handle_line(self, line: str) -> bool:
        """Parse one line. Returns True if accepted (state updated)."""
        self._stats.transport_messages_received += 1
        try:
            envelope = decode_envelope(line)
        except TransportError as exc:
            self._stats.transport_messages_rejected += 1
            self._stats.last_transport_error = exc.code
            return False

        if envelope.event_seq <= self._state.last_event_seq:
            self._stats.transport_out_of_order += 1
            self._stats.last_transport_error = "out_of_order"
            return False

        if envelope.kind == "text":
            self._stats.transport_text_events += 1
        else:
            self._stats.transport_clear_events += 1
        self._state = StableOCRState(
            worker_session_id=self._session_id,
            last_event_seq=envelope.event_seq,
            kind=envelope.kind,
            text=envelope.text,
            confidence=envelope.confidence,
            source_seq=envelope.source_seq,
            timestamp_monotonic=envelope.timestamp_monotonic,
        )
        self._stats.last_transport_error = None
        # State is committed before notifying the observer so downstream overlay
        # problems can never invalidate OCR correctness.
        self._notify_accepted_event(
            AcceptedStableTextEvent(
                worker_session_id=self._session_id,
                event_seq=envelope.event_seq,
                kind=envelope.kind,
                text=envelope.text,
                confidence=envelope.confidence,
                source_seq=envelope.source_seq,
                timestamp_monotonic=envelope.timestamp_monotonic,
            )
        )
        return True

    # -- observer seam -----------------------------------------------------

    def set_observer(self, observer: Optional[OCRTransportObserver]) -> None:
        """Attach/replace the optional post-accept observer (narrow seam).

        Does not touch state, stats, or session; used only for production wiring.
        """
        self._observer = observer

    def _notify_begin_session(self, session_id: str) -> None:
        observer = self._observer
        if observer is None:
            return
        try:
            observer.begin_session(session_id)
        except Exception as exc:
            self._stats.observer_errors += 1
            self._stats.last_observer_error = f"begin_session:{type(exc).__name__}"

    def _notify_accepted_event(self, event: AcceptedStableTextEvent) -> None:
        observer = self._observer
        if observer is None:
            return
        try:
            observer.on_accepted_event(event)
        except Exception as exc:
            self._stats.observer_errors += 1
            self._stats.last_observer_error = f"on_accepted_event:{type(exc).__name__}"

    def state(self) -> StableOCRState:
        return self._state

    def state_dict(self) -> dict:
        state = self._state
        return {
            "worker_session_id": state.worker_session_id,
            "last_event_seq": state.last_event_seq,
            "kind": state.kind,
            "text": state.text,
            "confidence": state.confidence,
            "source_seq": state.source_seq,
            "timestamp_monotonic": state.timestamp_monotonic,
        }

    def status(self) -> dict:
        state = self._state
        if state.kind == "text" and state.text:
            transport_state = STATE_TEXT
        elif state.kind == "clear":
            transport_state = STATE_CLEAR
        else:
            transport_state = STATE_IDLE
        return {
            "state": transport_state,
            "worker_session_id": state.worker_session_id,
            "last_event_seq": state.last_event_seq,
            "last_kind": state.kind,
            "last_text": state.text,
            "last_confidence": state.confidence,
            "last_source_seq": state.source_seq,
            "last_timestamp_monotonic": state.timestamp_monotonic,
            "transport_messages_received": self._stats.transport_messages_received,
            "transport_messages_rejected": self._stats.transport_messages_rejected,
            "transport_out_of_order": self._stats.transport_out_of_order,
            "transport_text_events": self._stats.transport_text_events,
            "transport_clear_events": self._stats.transport_clear_events,
            "last_transport_error": self._stats.last_transport_error,
            "observer_errors": self._stats.observer_errors,
            "last_observer_error": self._stats.last_observer_error,
        }
