"""Phase 2L.2: multi-region OCR execution foundation.

Processes one already-decoded captured frame against multiple enabled
``RecognitionRegion`` crops, running OCR once per region through a single shared
``OCRRuntime`` and feeding each region's own ``OCRStabilizer``. Region identity is
the stable ``region_id``; per-region stabilizer state is fully isolated.

Pure/lightweight: no numpy/cv2/rapidocr import. The OCR runtime is injected, and
this module is not wired into the production OCR worker in Phase 2L.2. The
internal ``RegionStableTextEvent`` is NOT serialized to the v1 production JSONL
transport.

Change-gated multi-region scheduling is intentionally deferred.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from capture.change_detector import decode_png_ex
from capture.recognition_regions import RecognitionRegion
from capture.roi import NormalizedROI, PixelROI, crop_rgba, resolve_roi
from ocr.stabilizer import OCRStabilizer, StableTextEvent

# Phase 2N.3: bounded, runtime-only latency samples for changed Stable Text.
MAX_LATENCY_SAMPLES = 8


@dataclass(frozen=True)
class RegionStableTextEvent:
    """Internal region-tagged stable event (not a wire format yet)."""

    region_id: str
    event: StableTextEvent
    captured_monotonic: Optional[float] = None


@dataclass
class MultiRegionStats:
    frames_processed: int = 0
    regions_active: int = 0
    crops: int = 0
    ocr_calls: int = 0
    events_emitted: int = 0
    states_discarded: int = 0
    states_reset: int = 0
    change_checks: int = 0
    regions_skipped: int = 0
    regions_forced: int = 0
    detector_errors: int = 0


@dataclass
class RegionRecognitionState:
    region: RecognitionRegion
    stabilizer: OCRStabilizer
    geometry: tuple[float, float, float, float]
    gate: Any = None


def region_pixel_rect(region: RecognitionRegion, frame_width: int, frame_height: int) -> PixelROI:
    """Authoritative normalized -> pixel mapping (reuses the single-ROI rules)."""
    return resolve_roi(
        NormalizedROI(x=region.x, y=region.y, width=region.w, height=region.h),
        frame_width,
        frame_height,
    )


class MultiRegionOCRCoordinator:
    """Owns per-region transient recognition state for multi-region execution."""

    def __init__(
        self,
        runtime: Any,
        *,
        stabilizer_factory: Optional[Callable[[], OCRStabilizer]] = None,
        gate_factory: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runtime = runtime
        self._stabilizer_factory = stabilizer_factory or OCRStabilizer
        self._gate_factory = gate_factory
        self._clock = clock
        self._states: dict[str, RegionRecognitionState] = {}
        self._stats = MultiRegionStats()
        self._latency: deque = deque(maxlen=MAX_LATENCY_SAMPLES)

    # -- introspection -----------------------------------------------------

    @property
    def stats(self) -> MultiRegionStats:
        return self._stats

    def state_ids(self) -> tuple[str, ...]:
        return tuple(self._states.keys())

    def drain_latency(self) -> list[dict]:
        """Return and clear the bounded changed-text latency samples."""
        samples = list(self._latency)
        self._latency.clear()
        return samples

    # -- execution ---------------------------------------------------------

    def process_frame(self, frame: Any, regions: Iterable[RecognitionRegion]) -> list[RegionStableTextEvent]:
        """Decode once, then OCR each enabled region from the same decoded frame."""
        decode_started = self._clock()
        decoded = decode_png_ex(frame.encoded_bytes)
        decode_ms = round((self._clock() - decode_started) * 1000.0, 3)
        return self.process_decoded(
            decoded.rgba,
            decoded.width,
            decoded.height,
            frame.sequence,
            regions,
            captured_monotonic=getattr(frame, "captured_monotonic", None),
            decode_ms=decode_ms,
        )

    def process_decoded(
        self,
        rgba: bytes,
        frame_width: int,
        frame_height: int,
        sequence: Optional[int],
        regions: Iterable[RecognitionRegion],
        captured_monotonic: Optional[float] = None,
        decode_ms: Optional[float] = None,
    ) -> list[RegionStableTextEvent]:
        enabled = [region for region in regions if region.enabled]
        self._discard_missing(enabled)
        self._stats.frames_processed += 1
        self._stats.regions_active = len(enabled)

        events: list[RegionStableTextEvent] = []
        for region in enabled:
            state = self._ensure_state(region)
            pixel = region_pixel_rect(region, frame_width, frame_height)
            crop_started = self._clock()
            crop_width, crop_height, crop = crop_rgba(rgba, frame_width, frame_height, pixel)
            crop_ended = self._clock()
            self._stats.crops += 1

            if state.gate is not None:
                decision = state.gate.decide(
                    crop,
                    crop_width,
                    crop_height,
                    sequence,
                    self._clock() if captured_monotonic is None else captured_monotonic,
                    roi_key=pixel.as_tuple(),
                )
                self._stats.change_checks += 1
                if decision.action == "skip":
                    # Skip is NOT no-text evidence; advance the region's own clock
                    # and forward any tick-produced clear (never discard it).
                    self._stats.regions_skipped += 1
                    for event in state.stabilizer.tick(self._clock()):
                        events.append(
                            RegionStableTextEvent(
                                region_id=region.region_id,
                                event=event,
                                captured_monotonic=captured_monotonic,
                            )
                        )
                        self._stats.events_emitted += 1
                    continue
                if decision.reason == "forced_refresh":
                    self._stats.regions_forced += 1
                elif decision.reason == "detector_error":
                    self._stats.detector_errors += 1

            ocr_started = self._clock()
            result = self._runtime.recognize_rgba(crop, crop_width, crop_height, sequence=sequence)
            ocr_ended = self._clock()
            self._stats.ocr_calls += 1
            for event in state.stabilizer.observe(result.lines, result.sequence, ocr_ended):
                events.append(
                    RegionStableTextEvent(
                        region_id=region.region_id,
                        event=event,
                        captured_monotonic=captured_monotonic,
                    )
                )
                self._stats.events_emitted += 1
                if event.kind == "text":
                    self._record_latency(
                        region.region_id,
                        sequence,
                        captured_monotonic,
                        decode_ms,
                        (crop_ended - crop_started) * 1000.0,
                        result,
                        event,
                        state.stabilizer,
                        ocr_started,
                        ocr_ended,
                    )
        return events

    def _record_latency(
        self,
        region_id: str,
        sequence: Optional[int],
        captured_monotonic: Optional[float],
        decode_ms: Optional[float],
        roi_ms: float,
        result: Any,
        event: StableTextEvent,
        stabilizer: OCRStabilizer,
        ocr_started: float,
        ocr_ended: float,
    ) -> None:
        ocr_ms = getattr(result, "elapsed_ms", None)
        if ocr_ms is None:
            ocr_ms = (ocr_ended - ocr_started) * 1000.0
        first_candidate = stabilizer.first_candidate_timestamp(event.text)
        sample = {
            "region_id": region_id,
            "frame_seq": sequence,
            "captured_monotonic": captured_monotonic,
            "capture_age_at_ocr_start_ms": (
                round((ocr_started - captured_monotonic) * 1000.0, 3)
                if captured_monotonic is not None
                else None
            ),
            "decode_ms": decode_ms,
            "roi_ms": round(roi_ms, 3),
            "ocr_ms": round(float(ocr_ms), 3),
            "stabilizer_accept_ms": (
                round((event.timestamp_monotonic - first_candidate) * 1000.0, 3)
                if first_candidate is not None
                else None
            ),
            "worker_total_ms": (
                round((event.timestamp_monotonic - captured_monotonic) * 1000.0, 3)
                if captured_monotonic is not None
                else None
            ),
        }
        self._latency.append(sample)

    def reset(self) -> None:
        self._states.clear()

    # -- state lifecycle ---------------------------------------------------

    def _discard_missing(self, enabled: list[RecognitionRegion]) -> None:
        active = {region.region_id for region in enabled}
        for region_id in [region_id for region_id in self._states if region_id not in active]:
            del self._states[region_id]
            self._stats.states_discarded += 1

    def _ensure_state(self, region: RecognitionRegion) -> RegionRecognitionState:
        geometry = (region.x, region.y, region.w, region.h)
        state = self._states.get(region.region_id)
        if state is None:
            state = RegionRecognitionState(
                region=region,
                stabilizer=self._stabilizer_factory(),
                geometry=geometry,
                gate=self._gate_factory() if self._gate_factory is not None else None,
            )
            self._states[region.region_id] = state
            return state
        if state.geometry != geometry:
            # Geometry change: fresh stabilizer AND fresh change-detector state;
            # other regions are untouched.
            state = RegionRecognitionState(
                region=region,
                stabilizer=self._stabilizer_factory(),
                geometry=geometry,
                gate=self._gate_factory() if self._gate_factory is not None else None,
            )
            self._states[region.region_id] = state
            self._stats.states_reset += 1
            return state
        # Same identity/geometry: preserve stabilizer + gate state; refresh metadata.
        state.region = region
        return state
