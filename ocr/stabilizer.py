"""OCR output stabilization (Phase 2G).

Turns raw per-frame OCR lines into a clean, stable text stream:

    raw lines -> confidence filter -> normalization -> temporal consensus
              -> duplicate suppression -> stale timeout -> stable events

Deliberately dependency-free: this module imports no numpy / cv2 / onnxruntime /
rapidocr / capture code, so it is fully unit-testable with plain objects and runs
synchronously after each OCR result (no background worker).
"""

from __future__ import annotations

import hashlib
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

# Phase 2N.4: bounded, runtime-only first-candidate reliability audit.
MAX_AUDIT_RECORDS = 16
MAX_AUDIT_SAVINGS = 64
AUDIT_CONFIDENCE_BUCKETS = ("0.70-0.79", "0.80-0.89", "0.90-0.94", "0.95-1.00")

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


@dataclass
class StabilizerAuditStats:
    """Phase 2N.4 aggregate counters for first-candidate reliability (runtime-only)."""

    transitions_total: int = 0
    clear_transitions: int = 0
    first_matches_final: int = 0
    first_differs_from_final: int = 0
    # bucket label -> {"count", "matches", "saving_ms_sum"}
    confidence_buckets: dict = field(default_factory=dict)


def _audit_digest(text: str) -> str:
    """Short process-local digest (never persisted; not a content log)."""
    return hashlib.blake2b(text.encode("utf-8"), digest_size=4).hexdigest()


def audit_confidence_bucket(confidence: float) -> str:
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return AUDIT_CONFIDENCE_BUCKETS[0]
    if value < 0.80:
        return "0.70-0.79"
    if value < 0.90:
        return "0.80-0.89"
    if value < 0.95:
        return "0.90-0.94"
    return "0.95-1.00"


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
        self._last_observed_candidate: Optional[OCRCandidate] = None
        self._last_consensus = 0
        self._last_observation_had_text = False
        self._stats = StabilizerStats()
        # Phase 2N.4 first-candidate reliability audit (observational only).
        self.audit_region_id: Optional[str] = None
        self._audit_trial: Optional[dict] = None
        self._audit_pending: Optional[dict] = None
        self._audit_recent: deque = deque(maxlen=MAX_AUDIT_RECORDS)
        self._audit_savings: deque = deque(maxlen=MAX_AUDIT_SAVINGS)
        self._audit_stats = StabilizerAuditStats()

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
        self._last_observed_candidate = candidate
        if candidate is not None:
            return self._accept_candidate(candidate, sequence, now)
        self._last_observation_had_text = False
        return self._maybe_clear(now)

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
        return self._maybe_clear(now)

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
        self._audit_observe(candidate)

        if self._last_consensus < self._consensus_required:
            return []
        if candidate.text == self._last_emitted_text:
            self._stats.duplicate_suppressed += 1
            return []
        self._audit_finalize(candidate, now)
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

    def _maybe_clear(self, now) -> list:
        if self._last_emitted_text is None or self._clear_emitted or self._last_activity is None:
            return []
        if now - self._last_activity < self._stale_timeout_sec:
            return []
        self._clear_emitted = True
        self._last_emitted_text = None
        self._stats.clear_emits += 1
        self._audit_note_clear()
        return [
            StableTextEvent(
                kind="clear",
                text="",
                confidence=None,
                source_seq=None,
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
        self._last_observed_candidate = None
        self._last_consensus = 0
        self._last_observation_had_text = False
        self._stats = StabilizerStats()
        self._audit_trial = None
        self._audit_pending = None
        self._audit_recent.clear()
        self._audit_savings.clear()
        self._audit_stats = StabilizerAuditStats()

    def stats(self) -> StabilizerStats:
        return self._stats

    @property
    def last_candidate(self) -> Optional[OCRCandidate]:
        return self._last_candidate

    @property
    def last_observed_candidate(self) -> Optional[OCRCandidate]:
        """Candidate built for the most recent ``observe`` (None on real no-text).

        Unlike ``last_candidate`` this is reset to None when an observation yields
        no usable candidate; used only for opt-in diagnostics.
        """
        return self._last_observed_candidate

    @property
    def min_line_confidence(self) -> float:
        return self._min_line_confidence

    @property
    def last_consensus(self) -> int:
        return self._last_consensus

    def first_candidate_timestamp(self, text: str) -> Optional[float]:
        """Earliest history timestamp for ``text`` (diagnostics only, read-only).

        Used to measure how long a new exact string took to reach consensus
        acceptance; never affects stabilization behavior.
        """
        for candidate in self._history:
            if candidate.text == text:
                return candidate.timestamp_monotonic
        return None

    # -- Phase 2N.4 first-candidate reliability audit (observational) ------

    def take_audit_record(self) -> Optional[dict]:
        record = self._audit_pending
        self._audit_pending = None
        return record

    def audit_recent(self) -> list:
        return list(self._audit_recent)

    def audit_savings(self) -> list:
        return list(self._audit_savings)

    def audit_summary(self) -> dict:
        stats = self._audit_stats
        total = stats.transitions_total
        savings = sorted(self._audit_savings)
        median = savings[len(savings) // 2] if savings else None
        return {
            "region_id": self.audit_region_id,
            "transitions_total": total,
            "clear_transitions": stats.clear_transitions,
            "first_matches_final": stats.first_matches_final,
            "first_differs_from_final": stats.first_differs_from_final,
            "match_rate": round(stats.first_matches_final / total, 4) if total else None,
            "median_first_to_accept_ms": median,
            "confidence_buckets": {key: dict(value) for key, value in stats.confidence_buckets.items()},
        }

    def _audit_observe(self, candidate: OCRCandidate) -> None:
        current = self._last_emitted_text
        if current is not None and candidate.text == current:
            # Repeated already-stable text: never starts a transition trial.
            self._audit_trial = None
            return
        if self._audit_trial is None:
            self._audit_trial = {
                "text": candidate.text,
                "digest": _audit_digest(candidate.text),
                "length": len(candidate.text),
                "confidence": candidate.confidence,
                "timestamp": candidate.timestamp_monotonic,
                "source_seq": candidate.source_seq,
                "count": 1,
                "distinct": {candidate.text},
            }
        else:
            self._audit_trial["count"] += 1
            self._audit_trial["distinct"].add(candidate.text)

    def _audit_finalize(self, candidate: OCRCandidate, now: float) -> None:
        trial = self._audit_trial
        if trial is None:
            trial = {
                "text": candidate.text,
                "digest": _audit_digest(candidate.text),
                "length": len(candidate.text),
                "confidence": candidate.confidence,
                "timestamp": candidate.timestamp_monotonic,
                "source_seq": candidate.source_seq,
                "count": 1,
                "distinct": {candidate.text},
            }
        matches = trial["text"] == candidate.text
        first_to_accept_ms = round((now - trial["timestamp"]) * 1000.0, 3)
        saving_ms = first_to_accept_ms if matches else 0.0
        record = {
            "region_id": self.audit_region_id,
            "frame_seq": candidate.source_seq,
            "first_candidate_seq": trial["source_seq"],
            "candidate_count_until_accept": trial["count"],
            "first_candidate_confidence": trial["confidence"],
            "accepted_candidate_confidence": candidate.confidence,
            "first_matches_final": matches,
            "first_candidate_length": trial["length"],
            "final_length": len(candidate.text),
            "first_candidate_to_accept_ms": first_to_accept_ms,
            "theoretical_fast_accept_saving_ms": saving_ms,
            "first_candidate_was_empty": False,
            "intermediate_distinct_candidate_count": len(trial["distinct"]),
            "first_candidate_digest": trial["digest"],
            "final_digest": _audit_digest(candidate.text),
        }
        self._audit_pending = record
        self._audit_recent.append(record)
        self._audit_savings.append(first_to_accept_ms)
        stats = self._audit_stats
        stats.transitions_total += 1
        if matches:
            stats.first_matches_final += 1
        else:
            stats.first_differs_from_final += 1
        bucket = audit_confidence_bucket(trial["confidence"])
        entry = stats.confidence_buckets.setdefault(
            bucket, {"count": 0, "matches": 0, "saving_ms_sum": 0.0}
        )
        entry["count"] += 1
        if matches:
            entry["matches"] += 1
        entry["saving_ms_sum"] = round(entry["saving_ms_sum"] + saving_ms, 3)
        self._audit_trial = None

    def _audit_note_clear(self) -> None:
        self._audit_stats.clear_transitions += 1
        self._audit_trial = None

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
