"""Provider-agnostic translation foundation (Phase 2J.1).

Pure stdlib: defines the translation domain contract (input/result/event), a
minimal ``TranslationProvider`` protocol, and a deterministic session-scoped
``TranslationCoordinator``. No network, no provider SDK, no threading, no
subprocess, no QAM/renderer wiring.

This module must never import rapidocr / onnxruntime / numpy / cv2 / omegaconf /
antlr4 or any network/provider library. It also does not import ``ocr.*``: the
canonical ``StableTextEvent`` carries no worker-session/event identity (that is
transport-level), so a narrow dependency-safe ``TranslationInput`` is used and
kept free of OCR coupling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


class TranslationError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# -- domain types ------------------------------------------------------------


@dataclass(frozen=True)
class TranslationInput:
    """Source identity for one accepted stable OCR event.

    ``worker_session_id`` + ``source_event_seq`` are the authoritative event
    identity used to reject stale/duplicate/cross-session results. ``kind=text``
    requires non-empty text; ``kind=clear`` requires empty text.
    """

    worker_session_id: str
    source_event_seq: int
    kind: str  # "text" | "clear"
    text: str
    confidence: Optional[float]
    source_seq: Optional[int]
    timestamp_monotonic: Optional[float]
    source_language: Optional[str]
    target_language: str


@dataclass(frozen=True)
class TranslationResult:
    """Provider-level translation result."""

    translated_text: str
    provider_name: str
    detected_source_language: Optional[str] = None


@dataclass(frozen=True)
class TranslatedTextEvent:
    """Authoritative coordinator output for one accepted source event.

    ``translation_event_seq`` is coordinator-session-local and strictly
    increasing. For ``kind=clear`` the translated fields are empty/None.
    """

    translation_event_seq: int
    kind: str
    worker_session_id: str
    source_event_seq: int
    source_text: str
    translated_text: str
    source_language: Optional[str]
    target_language: str
    provider_name: Optional[str] = None
    detected_source_language: Optional[str] = None
    source_confidence: Optional[float] = None
    source_seq: Optional[int] = None
    source_timestamp_monotonic: Optional[float] = None


@dataclass(frozen=True)
class ConsumeOutcome:
    """Result of a ``consume`` call.

    ``reason`` is one of: ``ok``, ``invalid``, ``no_session``,
    ``session_mismatch``, ``stale_or_duplicate``, ``provider_error``.
    """

    accepted: bool
    reason: str
    event: Optional[TranslatedTextEvent] = None
    error: Optional[str] = None


@dataclass
class TranslationStats:
    inputs_received: int = 0
    inputs_rejected: int = 0
    text_events: int = 0
    clear_events: int = 0
    provider_calls: int = 0
    provider_errors: int = 0
    last_error: Optional[str] = None


# -- provider interface ------------------------------------------------------


class TranslationProvider(Protocol):
    @property
    def name(self) -> str:
        ...

    def translate(
        self,
        text: str,
        *,
        source_language: Optional[str],
        target_language: str,
    ) -> TranslationResult:
        ...


# -- validation --------------------------------------------------------------


def validate_translation_input(translation_input: TranslationInput) -> None:
    if not isinstance(translation_input, TranslationInput):
        raise TranslationError("invalid_input", type(translation_input).__name__)
    if not isinstance(translation_input.worker_session_id, str) or not translation_input.worker_session_id:
        raise TranslationError("missing_worker_session_id")
    if isinstance(translation_input.source_event_seq, bool) or not isinstance(
        translation_input.source_event_seq, int
    ):
        raise TranslationError("invalid_source_event_seq")
    if translation_input.source_event_seq <= 0:
        raise TranslationError("invalid_source_event_seq")
    if translation_input.kind not in ("text", "clear"):
        raise TranslationError("invalid_kind", str(translation_input.kind))
    if not isinstance(translation_input.target_language, str) or not translation_input.target_language:
        raise TranslationError("missing_target_language")
    if not isinstance(translation_input.text, str):
        raise TranslationError("invalid_text")
    if translation_input.kind == "text" and not translation_input.text:
        raise TranslationError("empty_text")
    if translation_input.kind == "clear" and translation_input.text != "":
        raise TranslationError("clear_text_not_empty")


def validate_translation_result(result: TranslationResult) -> None:
    if not isinstance(result, TranslationResult):
        raise TranslationError("invalid_result", type(result).__name__)
    if not isinstance(result.translated_text, str):
        raise TranslationError("invalid_result_text")
    if not isinstance(result.provider_name, str) or not result.provider_name:
        raise TranslationError("missing_provider_name")


# -- coordinator -------------------------------------------------------------


class TranslationCoordinator:
    """Deterministic, session-scoped translation coordinator.

    State is scoped to a single OCR worker session. It never starts itself and
    owns no OCR worker/renderer/QAM concerns. Ordering/idempotence is enforced
    against ``last_committed_source_event_seq`` (advanced only when an event is
    successfully emitted). A failed provider call advances
    ``last_seen_source_event_seq`` but NOT the committed value, so a failed
    source event is never recorded as successfully translated and may be
    retried explicitly in a future gate (no automatic retry here).
    """

    def __init__(self, provider: TranslationProvider) -> None:
        self._provider = provider
        self._session_id: Optional[str] = None
        self._translation_event_seq = 0
        self._last_committed_source_event_seq = 0
        self._last_seen_source_event_seq = 0
        self._latest: Optional[TranslatedTextEvent] = None
        self._stats = TranslationStats()

    # -- session -----------------------------------------------------------

    def begin_session(self, worker_session_id: str) -> None:
        if not isinstance(worker_session_id, str) or not worker_session_id:
            raise TranslationError("missing_worker_session_id")
        self._session_id = worker_session_id
        self._translation_event_seq = 0
        self._last_committed_source_event_seq = 0
        self._last_seen_source_event_seq = 0
        self._latest = None
        self._stats = TranslationStats()

    def reset(self) -> None:
        self._session_id = None
        self._translation_event_seq = 0
        self._last_committed_source_event_seq = 0
        self._last_seen_source_event_seq = 0
        self._latest = None
        self._stats = TranslationStats()

    # -- introspection -----------------------------------------------------

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def latest_event(self) -> Optional[TranslatedTextEvent]:
        return self._latest

    def status(self) -> dict:
        latest = self._latest
        return {
            "worker_session_id": self._session_id,
            "translation_event_seq": self._translation_event_seq,
            "last_committed_source_event_seq": self._last_committed_source_event_seq,
            "last_seen_source_event_seq": self._last_seen_source_event_seq,
            "latest_kind": latest.kind if latest is not None else None,
            "latest_translated_text": latest.translated_text if latest is not None else "",
            "latest_source_event_seq": latest.source_event_seq if latest is not None else None,
            "inputs_received": self._stats.inputs_received,
            "inputs_rejected": self._stats.inputs_rejected,
            "text_events": self._stats.text_events,
            "clear_events": self._stats.clear_events,
            "provider_calls": self._stats.provider_calls,
            "provider_errors": self._stats.provider_errors,
            "last_error": self._stats.last_error,
        }

    # -- consume -----------------------------------------------------------

    def consume(self, translation_input: TranslationInput) -> ConsumeOutcome:
        self._stats.inputs_received += 1
        try:
            validate_translation_input(translation_input)
        except TranslationError as exc:
            self._stats.inputs_rejected += 1
            self._stats.last_error = exc.code
            return ConsumeOutcome(False, "invalid", None, exc.code)

        if self._session_id is None:
            self._stats.inputs_rejected += 1
            self._stats.last_error = "no_session"
            return ConsumeOutcome(False, "no_session", None, "no_session")

        if translation_input.worker_session_id != self._session_id:
            self._stats.inputs_rejected += 1
            self._stats.last_error = "session_mismatch"
            return ConsumeOutcome(False, "session_mismatch", None, "session_mismatch")

        if translation_input.source_event_seq <= self._last_committed_source_event_seq:
            self._stats.inputs_rejected += 1
            self._stats.last_error = "stale_or_duplicate"
            return ConsumeOutcome(False, "stale_or_duplicate", None, "stale_or_duplicate")

        self._last_seen_source_event_seq = max(
            self._last_seen_source_event_seq, translation_input.source_event_seq
        )

        if translation_input.kind == "clear":
            event = self._emit_clear(translation_input)
            return ConsumeOutcome(True, "ok", event)

        return self._consume_text(translation_input)

    # -- internals ---------------------------------------------------------

    def _consume_text(self, translation_input: TranslationInput) -> ConsumeOutcome:
        self._stats.provider_calls += 1
        try:
            result = self._provider.translate(
                translation_input.text,
                source_language=translation_input.source_language,
                target_language=translation_input.target_language,
            )
            validate_translation_result(result)
        except Exception as exc:  # provider failure is explicit and non-destructive
            self._stats.provider_errors += 1
            self._stats.last_error = f"provider_error:{type(exc).__name__}"
            return ConsumeOutcome(False, "provider_error", None, self._stats.last_error)

        self._translation_event_seq += 1
        event = TranslatedTextEvent(
            translation_event_seq=self._translation_event_seq,
            kind="text",
            worker_session_id=translation_input.worker_session_id,
            source_event_seq=translation_input.source_event_seq,
            source_text=translation_input.text,
            translated_text=result.translated_text,
            source_language=translation_input.source_language,
            target_language=translation_input.target_language,
            provider_name=result.provider_name,
            detected_source_language=result.detected_source_language,
            source_confidence=translation_input.confidence,
            source_seq=translation_input.source_seq,
            source_timestamp_monotonic=translation_input.timestamp_monotonic,
        )
        self._latest = event
        self._last_committed_source_event_seq = translation_input.source_event_seq
        self._stats.text_events += 1
        self._stats.last_error = None
        return ConsumeOutcome(True, "ok", event)

    def _emit_clear(self, translation_input: TranslationInput) -> TranslatedTextEvent:
        self._translation_event_seq += 1
        event = TranslatedTextEvent(
            translation_event_seq=self._translation_event_seq,
            kind="clear",
            worker_session_id=translation_input.worker_session_id,
            source_event_seq=translation_input.source_event_seq,
            source_text="",
            translated_text="",
            source_language=translation_input.source_language,
            target_language=translation_input.target_language,
            provider_name=None,
            detected_source_language=None,
            source_confidence=None,
            source_seq=translation_input.source_seq,
            source_timestamp_monotonic=translation_input.timestamp_monotonic,
        )
        self._latest = event
        self._last_committed_source_event_seq = translation_input.source_event_seq
        self._stats.clear_events += 1
        self._stats.last_error = None
        return event
