"""OCR output stabilization (Phase 2G).

Turns raw per-frame OCR lines into a clean, stable text stream:

    raw lines -> confidence filter -> normalization -> temporal consensus
              -> duplicate suppression -> stale timeout -> stable events

Deliberately dependency-free: this module imports no numpy / cv2 / onnxruntime /
rapidocr / capture code, so it is fully unit-testable with plain objects and runs
synchronously after each OCR result (no background worker).
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

DEFAULT_MIN_LINE_CONFIDENCE = 0.70
DEFAULT_CONSENSUS_REQUIRED = 2
DEFAULT_HISTORY_SIZE = 3
DEFAULT_STALE_TIMEOUT_SEC = 2.0
DEFAULT_LINE_SEPARATOR = "\n"

_INNER_SPACE_RE = re.compile(r"[ \t]+")


class StabilizerConfigError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class OCRCandidate:
    text: str
    confidence: float
    source_seq: Optional[int]
    timestamp_monotonic: float


@dataclass(frozen=True)
class StableTextEvent:
    kind: str  # "text" | "clear"
    text: str
    confidence: Optional[float]
    source_seq: Optional[int]
    timestamp_monotonic: float


@dataclass
class StabilizerStats:
    raw_frames: int = 0
    eligible_candidates: int = 0
    low_conf_lines_dropped: int = 0
    stable_text_emits: int = 0
    duplicate_suppressed: int = 0
    clear_emits: int = 0


def normalize_line(text: Optional[str]) -> str:
    """Conservative per-line normalization (no translation/punctuation edits)."""
    if text is None:
        return ""
    value = unicodedata.normalize("NFC", str(text))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _INNER_SPACE_RE.sub(" ", value)
    return value.strip()


def build_candidate_text(lines: Iterable, separator: str = DEFAULT_LINE_SEPARATOR) -> str:
    """Join normalized non-empty lines, preserving order."""
    parts = []
    for line in lines:
        normalized = normalize_line(getattr(line, "text", ""))
        if normalized:
            parts.append(normalized)
    return separator.join(parts)


class OCRStabilizer:
    def __init__(
        self,
        min_line_confidence: float = DEFAULT_MIN_LINE_CONFIDENCE,
        consensus_required: int = DEFAULT_CONSENSUS_REQUIRED,
        history_size: int = DEFAULT_HISTORY_SIZE,
        stale_timeout_sec: float = DEFAULT_STALE_TIMEOUT_SEC,
        line_separator: str = DEFAULT_LINE_SEPARATOR,
        exclude: Optional[Callable[[str], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_line_confidence = self._validate_confidence(min_line_confidence)
        self._consensus_required = self._validate_consensus(consensus_required)
        self._history_size = self._validate_history(history_size, self._consensus_required)
        self._stale_timeout_sec = self._validate_timeout(stale_timeout_sec)
        self._line_separator = line_separator
        self._exclude = exclude or (lambda _text: False)
        self._clock = clock
        self._history: deque = deque(maxlen=self._history_size)
        self._last_emitted_text: Optional[str] = None
        self._clear_emitted = False
        self._last_activity: Optional[float] = None
        self._last_candidate: Optional[OCRCandidate] = None
        self._last_consensus = 0
        self._last_observation_had_text = False
        self._stats = StabilizerStats()

    # -- validation --------------------------------------------------------

    @staticmethod
    def _validate_confidence(value: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise StabilizerConfigError("invalid_min_line_confidence", str(value)) from exc
        if not 0.0 <= parsed <= 1.0:
            raise StabilizerConfigError("invalid_min_line_confidence", str(value))
        return parsed

    @staticmethod
    def _validate_consensus(value: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise StabilizerConfigError("invalid_consensus_required", str(value)) from exc
        if parsed < 1:
            raise StabilizerConfigError("invalid_consensus_required", str(value))
        return parsed

    @staticmethod
    def _validate_history(value: int, consensus_required: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise StabilizerConfigError("invalid_history_size", str(value)) from exc
        if parsed < consensus_required:
            raise StabilizerConfigError("invalid_history_size", f"{value} < {consensus_required}")
        return parsed

    @staticmethod
    def _validate_timeout(value: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise StabilizerConfigError("invalid_stale_timeout", str(value)) from exc
        if parsed <= 0.0:
            raise StabilizerConfigError("invalid_stale_timeout", str(value))
        return parsed

    # -- observation -------------------------------------------------------

    def observe(self, lines: Iterable, sequence: Optional[int] = None, timestamp_monotonic: Optional[float] = None) -> list:
        """Feed one OCR frame; return zero or more stable events."""
        now = self._clock() if timestamp_monotonic is None else timestamp_monotonic
        self._stats.raw_frames += 1

        candidate = self._build_candidate(lines, sequence, now)
        if candidate is not None:
            return self._accept_candidate(candidate, sequence, now)
        self._last_observation_had_text = False
        return self._maybe_clear(sequence, now)

    def tick(self, timestamp_monotonic: Optional[float] = None) -> list:
        """Advance time for a skipped-OCR frame (unchanged ROI).

        Must NOT add candidate history, count as empty OCR, reset consensus, or
        increment raw_frames. If the last real observation contained text, the
        stable text is preserved (keep-alive); otherwise the stale timeout may
        complete and emit one clear.
        """
        now = self._clock() if timestamp_monotonic is None else timestamp_monotonic
        if self._last_observation_had_text:
            if self._last_emitted_text is not None:
                self._last_activity = now
            return []
        return self._maybe_clear(None, now)

    def _build_candidate(self, lines, sequence, now) -> Optional[OCRCandidate]:
        kept = []
        for line in lines or ():
            text = getattr(line, "text", "")
            confidence = getattr(line, "confidence", None)
            if confidence is None:
                # conservative: missing confidence is rejected, never treated as 1.0
                self._stats.low_conf_lines_dropped += 1
                continue
            try:
                value = float(confidence)
            except (TypeError, ValueError):
                self._stats.low_conf_lines_dropped += 1
                continue
            if value < self._min_line_confidence:
                self._stats.low_conf_lines_dropped += 1
                continue
            normalized = normalize_line(text)
            if not normalized:
                continue
            kept.append((normalized, value))
        if not kept:
            return None
        text = self._line_separator.join(item[0] for item in kept)
        if self._exclude(text):
            return None
        confidence = min(item[1] for item in kept)
        return OCRCandidate(text=text, confidence=confidence, source_seq=sequence, timestamp_monotonic=now)

    def _accept_candidate(self, candidate: OCRCandidate, sequence, now) -> list:
        self._stats.eligible_candidates += 1
        self._last_activity = now
        self._last_candidate = candidate
        self._last_observation_had_text = True
        self._clear_emitted = False
        self._history.append(candidate)
        self._last_consensus = sum(1 for item in self._history if item.text == candidate.text)

        if self._last_consensus < self._consensus_required:
            return []
        if candidate.text == self._last_emitted_text:
            self._stats.duplicate_suppressed += 1
            return []
        self._last_emitted_text = candidate.text
        self._stats.stable_text_emits += 1
        return [
            StableTextEvent(
                kind="text",
                text=candidate.text,
                confidence=candidate.confidence,
                source_seq=candidate.source_seq,
                timestamp_monotonic=now,
            )
        ]

    def _maybe_clear(self, sequence, now) -> list:
        if self._last_emitted_text is None or self._clear_emitted or self._last_activity is None:
            return []
        if now - self._last_activity < self._stale_timeout_sec:
            return []
        self._clear_emitted = True
        self._last_emitted_text = None
        self._stats.clear_emits += 1
        return [
            StableTextEvent(
                kind="clear",
                text="",
                confidence=None,
                source_seq=sequence,
                timestamp_monotonic=now,
            )
        ]

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        self._history.clear()
        self._last_emitted_text = None
        self._clear_emitted = False
        self._last_activity = None
        self._last_candidate = None
        self._last_consensus = 0
        self._last_observation_had_text = False
        self._stats = StabilizerStats()

    def stats(self) -> StabilizerStats:
        return self._stats

    @property
    def last_candidate(self) -> Optional[OCRCandidate]:
        return self._last_candidate

    @property
    def last_consensus(self) -> int:
        return self._last_consensus

    @property
    def consensus_required(self) -> int:
        return self._consensus_required

    @property
    def history_size(self) -> int:
        return self._history_size

    @property
    def last_emitted_text(self) -> Optional[str]:
        return self._last_emitted_text

    @property
    def needs_confirmation(self) -> bool:
        """True when the last real observation has an unstable, not-yet-emitted candidate."""
        if not self._last_observation_had_text or self._last_candidate is None:
            return False
        if self._last_consensus >= self._consensus_required:
            return False
        return self._last_candidate.text != self._last_emitted_text

    @property
    def history_length(self) -> int:
        return len(self._history)
