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
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from capture.change_detector import decode_png_ex
from capture.recognition_regions import RecognitionRegion
from capture.roi import NormalizedROI, PixelROI, crop_rgba, resolve_roi
from ocr.stabilizer import OCRStabilizer, StableTextEvent


@dataclass(frozen=True)
class RegionStableTextEvent:
    """Internal region-tagged stable event (not a wire format yet)."""

    region_id: str
    event: StableTextEvent


@dataclass
class MultiRegionStats:
    frames_processed: int = 0
    regions_active: int = 0
    crops: int = 0
    ocr_calls: int = 0
    events_emitted: int = 0
    states_discarded: int = 0
    states_reset: int = 0


@dataclass
class RegionRecognitionState:
    region: RecognitionRegion
    stabilizer: OCRStabilizer
    geometry: tuple[float, float, float, float]


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runtime = runtime
        self._stabilizer_factory = stabilizer_factory or OCRStabilizer
        self._clock = clock
        self._states: dict[str, RegionRecognitionState] = {}
        self._stats = MultiRegionStats()

    # -- introspection -----------------------------------------------------

    @property
    def stats(self) -> MultiRegionStats:
        return self._stats

    def state_ids(self) -> tuple[str, ...]:
        return tuple(self._states.keys())

    # -- execution ---------------------------------------------------------

    def process_frame(self, frame: Any, regions: Iterable[RecognitionRegion]) -> list[RegionStableTextEvent]:
        """Decode once, then OCR each enabled region from the same decoded frame."""
        decoded = decode_png_ex(frame.encoded_bytes)
        return self.process_decoded(decoded.rgba, decoded.width, decoded.height, frame.sequence, regions)

    def process_decoded(
        self,
        rgba: bytes,
        frame_width: int,
        frame_height: int,
        sequence: Optional[int],
        regions: Iterable[RecognitionRegion],
    ) -> list[RegionStableTextEvent]:
        enabled = [region for region in regions if region.enabled]
        self._discard_missing(enabled)
        self._stats.frames_processed += 1
        self._stats.regions_active = len(enabled)

        events: list[RegionStableTextEvent] = []
        for region in enabled:
            state = self._ensure_state(region)
            pixel = region_pixel_rect(region, frame_width, frame_height)
            crop_width, crop_height, crop = crop_rgba(rgba, frame_width, frame_height, pixel)
            self._stats.crops += 1
            result = self._runtime.recognize_rgba(crop, crop_width, crop_height, sequence=sequence)
            self._stats.ocr_calls += 1
            for event in state.stabilizer.observe(result.lines, result.sequence, self._clock()):
                events.append(RegionStableTextEvent(region_id=region.region_id, event=event))
                self._stats.events_emitted += 1
        return events

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
                region=region, stabilizer=self._stabilizer_factory(), geometry=geometry
            )
            self._states[region.region_id] = state
            return state
        if state.geometry != geometry:
            state = RegionRecognitionState(
                region=region, stabilizer=self._stabilizer_factory(), geometry=geometry
            )
            self._states[region.region_id] = state
            self._stats.states_reset += 1
            return state
        # Same identity/geometry: preserve stabilizer state; refresh metadata only.
        state.region = region
        return state
