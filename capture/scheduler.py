"""Change-gated OCR scheduling (Phase 2H).

A thin scheduling adapter around the existing ROI change detector. It decides,
per already-decoded/cropped ROI frame, whether OCR should run:

    first frame        -> OCR (baseline init)
    detector changed   -> OCR
    force interval     -> OCR (recovery from detector false negatives)
    otherwise          -> skip OCR

Detector failures fail open (OCR runs). The gate is opt-in and only an
optimization: OCR/stabilizer correctness must remain testable with it disabled.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import CaptureError
from .roi import ROIChangeDetector

DEFAULT_FORCE_OCR_INTERVAL_SEC = 3.0
DEFAULT_MAX_CONFIRMATION_ATTEMPTS = 2

ACTIONS = ("ocr", "skip")
REASONS = ("first_frame", "detector_error", "change", "confirmation", "forced_refresh", "unchanged")


def validate_force_interval(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CaptureError("invalid_force_interval", f"{value!r}") from exc
    if parsed <= 0.0:
        raise CaptureError("invalid_force_interval", f"{value!r}")
    return parsed


@dataclass
class SchedulerStats:
    frames_seen: int = 0
    change_checks: int = 0
    changed_frames: int = 0
    unchanged_frames: int = 0
    ocr_calls: int = 0
    ocr_skipped: int = 0
    ocr_trigger_first: int = 0
    ocr_trigger_change: int = 0
    ocr_trigger_confirmation: int = 0
    ocr_trigger_forced: int = 0
    ocr_trigger_detector_error: int = 0
    change_detector_errors: int = 0
    change_check_total_ms: float = 0.0
    confirmation_armed: int = 0
    confirmation_superseded: int = 0
    confirmation_retried: int = 0
    confirmation_exhausted: int = 0

    @property
    def ocr_skip_ratio(self) -> float:
        return round(self.ocr_skipped / self.frames_seen, 3) if self.frames_seen else 0.0

    @property
    def avg_change_check_ms(self) -> Optional[float]:
        if not self.change_checks:
            return None
        return round(self.change_check_total_ms / self.change_checks, 3)


@dataclass(frozen=True)
class SchedulerDecision:
    action: str
    reason: str
    changed: Optional[bool]
    change_check_ms: float
    confirmation_remaining: int = 0
    roi_geometry_changed: bool = False
    roi_geometry: Optional[tuple] = None


class OCRChangeGate:
    def __init__(
        self,
        detector: Optional[ROIChangeDetector] = None,
        *,
        force_interval_sec: float = DEFAULT_FORCE_OCR_INTERVAL_SEC,
        max_confirmation_attempts: int = DEFAULT_MAX_CONFIRMATION_ATTEMPTS,
        force_unchanged: bool = False,
        force_detector_error: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._detector = detector if detector is not None else ROIChangeDetector()
        self._force_interval = validate_force_interval(force_interval_sec)
        self._max_confirmation_attempts = max(1, int(max_confirmation_attempts))
        # Diagnostic-only: force the scheduler-facing changed result to False.
        self._force_unchanged = bool(force_unchanged)
        # Diagnostic-only: inject an exception at the detector boundary.
        self._force_detector_error = bool(force_detector_error)
        self._clock = clock
        self._first_done = False
        self._last_ocr_time: Optional[float] = None
        self._geometry: Optional[tuple] = None
        self._previous_geometry: Optional[tuple] = None
        self._confirmation_pending = False
        self._confirmation_remaining = 0
        self._confirmation_attempts_done = 0
        self._confirmation_exhausted_counted = False
        self._stats = SchedulerStats()

    def note_ocr_result(self, needs_confirmation: bool) -> None:
        """Bounded downstream feedback: arm/retry confirmation OCRs.

        The budget is `max_confirmation_attempts` confirmation OCRs per detected
        change (default 2 => change + 2 confirmations max). Fails open (arms
        confirmation) if the feedback value is unexpected.
        """
        try:
            needs = bool(needs_confirmation)
        except Exception:
            needs = True
        if needs and self._confirmation_remaining > 0:
            self._confirmation_pending = True
            if self._confirmation_attempts_done == 0:
                self._stats.confirmation_armed += 1
            else:
                self._stats.confirmation_retried += 1
        else:
            self._confirmation_pending = False
            if needs and not self._confirmation_exhausted_counted and self._confirmation_attempts_done > 0:
                self._confirmation_exhausted_counted = True
                self._stats.confirmation_exhausted += 1

    def _arm_budget(self) -> None:
        self._confirmation_remaining = self._max_confirmation_attempts
        self._confirmation_attempts_done = 0
        self._confirmation_exhausted_counted = False

    def _clear_confirmation(self) -> None:
        self._confirmation_pending = False
        self._confirmation_remaining = 0
        self._confirmation_attempts_done = 0
        self._confirmation_exhausted_counted = False

    @property
    def confirmation_pending(self) -> bool:
        return self._confirmation_pending

    @property
    def confirmation_remaining(self) -> int:
        return self._confirmation_remaining

    def decide(
        self,
        rgba,
        width: int,
        height: int,
        sequence,
        captured_monotonic,
        roi_key: Optional[tuple] = None,
    ) -> SchedulerDecision:
        from .roi import PixelROI  # local import keeps the module import surface small

        self._stats.frames_seen += 1
        now = self._clock()

        # Track the full authoritative ROI rect (x, y, w, h) when provided; fall
        # back to the pixel size. A ROI change must reset detector + confirmation.
        geometry = tuple(roi_key) if roi_key is not None else (int(width), int(height))
        roi_geometry_changed = False
        if self._geometry != geometry:
            self._detector.reset_state()
            self._first_done = False
            self._geometry = geometry
            self._clear_confirmation()
            roi_geometry_changed = self._previous_geometry is not None
        self._previous_geometry = geometry

        changed: Optional[bool] = None
        detector_error = False
        started = time.perf_counter()
        try:
            if self._force_detector_error:
                # Diagnostic-only: inject at the exact boundary where a real
                # detector exception would be raised.
                raise RuntimeError("injected_detector_error")
            decision = self._detector.classify_rgba(
                sequence=sequence,
                captured_monotonic=captured_monotonic,
                width=int(width),
                height=int(height),
                rgba=rgba,
                pixel_roi=PixelROI(0, 0, int(width), int(height)),
            )
            changed = bool(decision.changed)
        except Exception:
            # Optimization must fail open to OCR.
            detector_error = True
            changed = True
            self._stats.change_detector_errors += 1
        change_ms = round((time.perf_counter() - started) * 1000.0, 3)
        self._stats.change_checks += 1
        self._stats.change_check_total_ms += change_ms

        # Diagnostic override: force the scheduler-facing result to unchanged
        # (detector failure still fails open to OCR).
        effective_changed = changed
        if self._force_unchanged and not detector_error:
            effective_changed = False
        if effective_changed:
            self._stats.changed_frames += 1
        else:
            self._stats.unchanged_frames += 1

        if not self._first_done:
            reason = "first_frame"
        elif detector_error:
            if self._confirmation_pending:
                self._confirmation_pending = False
            reason = "detector_error"
        elif effective_changed:
            if self._confirmation_pending:
                self._stats.confirmation_superseded += 1
                self._confirmation_pending = False
            reason = "change"
        elif self._confirmation_pending:
            reason = "confirmation"
        elif self._last_ocr_time is None or (now - self._last_ocr_time) >= self._force_interval:
            reason = "forced_refresh"
        else:
            reason = "unchanged"

        if reason == "unchanged":
            self._stats.ocr_skipped += 1
            return SchedulerDecision(
                "skip", reason, effective_changed, change_ms, self._confirmation_remaining,
                roi_geometry_changed, geometry,
            )

        self._first_done = True
        self._last_ocr_time = now
        self._stats.ocr_calls += 1
        if reason == "first_frame":
            self._arm_budget()
            self._stats.ocr_trigger_first += 1
        elif reason == "detector_error":
            self._arm_budget()
            self._stats.ocr_trigger_detector_error += 1
        elif reason == "confirmation":
            self._confirmation_pending = False
            self._confirmation_remaining = max(0, self._confirmation_remaining - 1)
            self._confirmation_attempts_done += 1
            self._stats.ocr_trigger_confirmation += 1
        elif reason == "forced_refresh":
            self._stats.ocr_trigger_forced += 1
        else:  # change
            self._arm_budget()
            self._stats.ocr_trigger_change += 1
        return SchedulerDecision(
            "ocr", reason, effective_changed, change_ms, self._confirmation_remaining,
            roi_geometry_changed, geometry,
        )

    def reset(self) -> None:
        """Explicit session boundary: clear transient scheduling state.

        Detector baseline, confirmation state, ROI geometry, per-session
        first-frame state and forced-refresh timing are cleared so the next valid
        frame OCRs as a fresh `first_frame`. Cumulative counters persist so a
        reset is observable.
        """
        self._detector.reset_state()
        self._first_done = False
        self._last_ocr_time = None
        self._geometry = None
        self._previous_geometry = None
        self._clear_confirmation()

    def stats(self) -> SchedulerStats:
        return self._stats

    @property
    def force_interval_sec(self) -> float:
        return self._force_interval
