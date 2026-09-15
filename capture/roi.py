"""Phase 2E: bounded subtitle ROI foundation (no OCR).

Sits *after* native RGBA decode:

    decoded RGBA -> normalized ROI -> pixel rect -> RGBA crop -> ROI signature

Bounded state: the current decoded frame, the current ROI buffer, and a single
retained ROI baseline signature. No frame/ROI history, no adaptive threshold.

The full-frame detector defaults (``DEFAULT_THRESHOLD`` / ``DEFAULT_GRID``) and
the libpng decode backend are intentionally untouched.
"""

from __future__ import annotations

import math
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Callable, Optional

from .change_detector import decode_png_ex, luminance_signature, signature_score
from .errors import CaptureError
from .frame import CaptureFrame

# ROI detector configuration (separate from the frozen full-frame defaults).
ROI_CHANGE_THRESHOLD = 0.006
ROI_GRID = (48, 12)

# Conservative default subtitle band (generous on purpose; not tuned per game).
DEFAULT_ROI_SCALE = 2
ALLOWED_SCALES = (1, 2, 3)
DEFERRED_SCALES = (2, 3)


@dataclass(frozen=True)
class NormalizedROI:
    """Fractional ROI with top-left origin; values are clamped at resolution."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CaptureError("invalid_roi", f"{name} must be a number")
            if math.isnan(value) or math.isinf(value):
                raise CaptureError("invalid_roi", f"{name} must be finite")
        if self.width <= 0 or self.height <= 0:
            raise CaptureError("invalid_roi", "width/height must be > 0")

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.width, self.height)


@dataclass(frozen=True)
class SubtitleBandROI(NormalizedROI):
    """Normalized ROI interpreted *relative to the broad ROI*, not the frame.

    Same validation/rounding rules as ``NormalizedROI`` (origin = top-left of the
    broad ROI). The broad ROI stays the game-specific safety envelope; the band
    is the change-detection / future-OCR working region.
    """


@dataclass(frozen=True)
class PixelROI:
    """Integer pixel rectangle, guaranteed fully inside the frame."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            raise CaptureError("invalid_roi", "pixel roi out of range")

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)


DEFAULT_ROI = NormalizedROI(x=0.08, y=0.62, width=0.84, height=0.32)

# Conservative subtitle band, relative to the broad ROI (tunable, not final).
DEFAULT_SUBTITLE_BAND = SubtitleBandROI(x=0.05, y=0.35, width=0.90, height=0.45)


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def _resolve_rect(roi, frame_width: int, frame_height: int) -> PixelROI:
    """Shared deterministic resolution for broad ROI and subtitle band."""
    try:
        fw = int(frame_width)
        fh = int(frame_height)
    except (TypeError, ValueError) as exc:
        raise CaptureError("invalid_roi", "invalid frame size") from exc
    if fw <= 0 or fh <= 0:
        raise CaptureError("invalid_roi", "invalid frame size")

    raw_left = _round_half_up(roi.x * fw)
    raw_top = _round_half_up(roi.y * fh)
    raw_width = max(1, _round_half_up(roi.width * fw))
    raw_height = max(1, _round_half_up(roi.height * fh))

    if raw_left >= fw or raw_top >= fh or raw_left + raw_width <= 0 or raw_top + raw_height <= 0:
        raise CaptureError("invalid_roi", "outside frame")

    left = max(0, min(raw_left, fw - 1))
    top = max(0, min(raw_top, fh - 1))
    width = max(1, min(raw_width, fw - left))
    height = max(1, min(raw_height, fh - top))
    return PixelROI(x=left, y=top, width=width, height=height)


def resolve_roi(roi: NormalizedROI, frame_width: int, frame_height: int) -> PixelROI:
    """Resolve a normalized ROI to a clamped pixel rectangle.

    Rounding policy (deterministic, documented): the left/top edges and the
    width/height are rounded half-up (``floor(v + 0.5)``) independently, then the
    rectangle is clamped to the frame. This keeps the default ROI stable across
    frame sizes (1280x800 -> 102,496,1075,256). A ROI that does not intersect the
    frame at all is rejected; a partially out-of-frame ROI is clamped.
    """
    return _resolve_rect(roi, frame_width, frame_height)


def resolve_subtitle_band(band: SubtitleBandROI, broad_width: int, broad_height: int) -> PixelROI:
    """Resolve a subtitle band relative to the broad ROI (same rounding policy)."""
    return _resolve_rect(band, broad_width, broad_height)


def crop_rgba(
    rgba: bytes,
    frame_width: int,
    frame_height: int,
    roi: PixelROI,
) -> tuple[int, int, bytes]:
    """Copy a pixel ROI out of tightly packed RGBA (row-slice copies only)."""
    try:
        fw = int(frame_width)
        fh = int(frame_height)
    except (TypeError, ValueError) as exc:
        raise CaptureError("crop_failed", "invalid frame size") from exc
    if fw <= 0 or fh <= 0:
        raise CaptureError("crop_failed", "invalid frame size")
    expected = fw * fh * 4
    if len(rgba) != expected:
        raise CaptureError("crop_failed", f"buffer {len(rgba)} != {expected}")
    if roi.x < 0 or roi.y < 0 or roi.width <= 0 or roi.height <= 0:
        raise CaptureError("crop_failed", "invalid roi")
    if roi.x + roi.width > fw or roi.y + roi.height > fh:
        raise CaptureError("crop_failed", "roi outside frame")

    if roi.x == 0 and roi.y == 0 and roi.width == fw and roi.height == fh:
        return fw, fh, rgba

    row_bytes = roi.width * 4
    src_stride = fw * 4
    out = bytearray(row_bytes * roi.height)
    for y in range(roi.height):
        src = (roi.y + y) * src_stride + roi.x * 4
        dst = y * row_bytes
        out[dst : dst + row_bytes] = rgba[src : src + row_bytes]
    return roi.width, roi.height, bytes(out)


def preprocess_roi(rgba: bytes, width: int, height: int, scale: int) -> tuple[int, int, bytes]:
    """ROI preprocessing API for future OCR input preparation.

    ``scale=1`` is the identity (no-op). ``scale`` 2x/3x (Lanczos) is deferred:
    Phase 2E must not add a heavy dependency (OpenCV/Pillow) solely for scaling,
    so those scales raise ``scale_unavailable`` rather than silently returning a
    different size.
    """
    try:
        value = int(scale)
    except (TypeError, ValueError) as exc:
        raise CaptureError("invalid_scale", f"scale={scale!r}") from exc
    if value not in ALLOWED_SCALES:
        raise CaptureError("invalid_scale", f"scale={value} not in {ALLOWED_SCALES}")
    try:
        w = int(width)
        h = int(height)
    except (TypeError, ValueError) as exc:
        raise CaptureError("preprocess_failed", "invalid size") from exc
    if w <= 0 or h <= 0:
        raise CaptureError("preprocess_failed", "invalid size")
    if value != 1:
        raise CaptureError("scale_unavailable", f"{value}x native scaler deferred")
    if len(rgba) != w * h * 4:
        raise CaptureError("preprocess_failed", f"buffer {len(rgba)} != {w * h * 4}")
    return w, h, rgba


def scale_supported(scale: int) -> bool:
    return int(scale) == 1


# -- per-game profile foundation ----------------------------------------------


@dataclass(frozen=True)
class GameROIProfile:
    app_id: Optional[str]
    roi: NormalizedROI
    subtitle_band: SubtitleBandROI = DEFAULT_SUBTITLE_BAND
    scale: int = DEFAULT_ROI_SCALE
    label: str = "default"

    def __post_init__(self) -> None:
        if not isinstance(self.roi, NormalizedROI):
            raise CaptureError("invalid_profile", "roi must be a NormalizedROI")
        if not isinstance(self.subtitle_band, NormalizedROI):
            raise CaptureError("invalid_profile", "subtitle_band must be a SubtitleBandROI")
        if int(self.scale) not in ALLOWED_SCALES:
            raise CaptureError("invalid_profile", f"scale={self.scale}")


DEFAULT_PROFILE = GameROIProfile(
    app_id=None,
    roi=DEFAULT_ROI,
    subtitle_band=DEFAULT_SUBTITLE_BAND,
    scale=DEFAULT_ROI_SCALE,
    label="default",
)


class ROIProfileStore:
    """Static in-code profile table: one default + optional exact app_id overrides."""

    def __init__(self, default: GameROIProfile = DEFAULT_PROFILE, overrides=None) -> None:
        self._default = default
        self._overrides: dict[str, GameROIProfile] = {}
        for profile in overrides or ():
            self.register(profile)

    def register(self, profile: GameROIProfile) -> None:
        if profile.app_id is None:
            self._default = profile
            return
        self._overrides[str(profile.app_id)] = profile

    def resolve(self, app_id: Optional[str] = None) -> GameROIProfile:
        if app_id is not None:
            profile = self._overrides.get(str(app_id))
            if profile is not None:
                return profile
        return self._default

    @property
    def default(self) -> GameROIProfile:
        return self._default

    @property
    def overrides(self) -> dict:
        return dict(self._overrides)


DEFAULT_PROFILE_STORE = ROIProfileStore()


def resolve_profile(app_id: Optional[str] = None, store: Optional[ROIProfileStore] = None) -> GameROIProfile:
    return (store or DEFAULT_PROFILE_STORE).resolve(app_id)


# -- debug export -------------------------------------------------------------


def encode_rgba_png(rgba: bytes, width: int, height: int) -> bytes:
    """Minimal stdlib RGBA PNG encoder for debug export (no Pillow/OpenCV)."""
    try:
        w = int(width)
        h = int(height)
    except (TypeError, ValueError) as exc:
        raise CaptureError("encode_failed", "invalid size") from exc
    if w <= 0 or h <= 0 or len(rgba) != w * h * 4:
        raise CaptureError("encode_failed", "invalid rgba buffer")

    stride = w * 4
    raw = bytearray((stride + 1) * h)
    for y in range(h):
        dst = y * (stride + 1)
        src = y * stride
        raw[dst + 1 : dst + 1 + stride] = rgba[src : src + stride]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


# -- subtitle band extraction -------------------------------------------------


@dataclass(frozen=True)
class SubtitleBandFrame:
    """Immutable, already-cropped subtitle band.

    Retains only the band pixels plus geometry; the full-frame decoded RGBA and
    any broad-ROI history are intentionally not referenced.
    """

    sequence: int
    width: int
    height: int
    rgba: bytes
    broad_roi: PixelROI
    band_roi: PixelROI
    captured_monotonic: float
    decoder_backend: Optional[str] = None
    decoder_fallback_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise CaptureError("invalid_band", "invalid band size")
        if len(self.rgba) != self.width * self.height * 4:
            raise CaptureError("invalid_band", "band buffer size mismatch")


def make_subtitle_band_frame(
    *,
    sequence: int,
    captured_monotonic: float,
    broad_rgba: bytes,
    broad_width: int,
    broad_height: int,
    broad_roi: PixelROI,
    band_roi: PixelROI,
    decoder_backend: Optional[str] = None,
    decoder_fallback_reason: Optional[str] = None,
) -> SubtitleBandFrame:
    """Crop the subtitle band from an already-cropped broad ROI (row slices only)."""
    width, height, band_rgba = crop_rgba(broad_rgba, broad_width, broad_height, band_roi)
    return SubtitleBandFrame(
        sequence=sequence,
        width=width,
        height=height,
        rgba=band_rgba,
        broad_roi=broad_roi,
        band_roi=band_roi,
        captured_monotonic=captured_monotonic,
        decoder_backend=decoder_backend,
        decoder_fallback_reason=decoder_fallback_reason,
    )


def extract_subtitle_band(
    frame_rgba: bytes,
    frame_width: int,
    frame_height: int,
    broad: NormalizedROI,
    band: SubtitleBandROI,
    *,
    sequence: int,
    captured_monotonic: float,
    decoder_backend: Optional[str] = None,
    decoder_fallback_reason: Optional[str] = None,
) -> tuple[SubtitleBandFrame, bytes]:
    """Full RGBA -> broad crop -> band crop (one broad crop, one band crop).

    Returns ``(band_frame, broad_rgba)``. ``broad_rgba`` is transient pipeline
    data (for the broad detector/debug copy); it is not retained by the band
    frame.
    """
    broad_roi = resolve_roi(broad, frame_width, frame_height)
    broad_width, broad_height, broad_rgba = crop_rgba(frame_rgba, frame_width, frame_height, broad_roi)
    band_roi = resolve_subtitle_band(band, broad_width, broad_height)
    band_frame = make_subtitle_band_frame(
        sequence=sequence,
        captured_monotonic=captured_monotonic,
        broad_rgba=broad_rgba,
        broad_width=broad_width,
        broad_height=broad_height,
        broad_roi=broad_roi,
        band_roi=band_roi,
        decoder_backend=decoder_backend,
        decoder_fallback_reason=decoder_fallback_reason,
    )
    return band_frame, broad_rgba


# -- ROI change detector ------------------------------------------------------


@dataclass(frozen=True)
class ROIDecision:
    sequence: int
    changed: bool
    score: float
    reason: str
    age_ms: float


@dataclass
class ROIDetectorStats:
    frames_seen: int = 0
    changed: int = 0
    unchanged: int = 0
    stale_rejected: int = 0
    decode_errors: int = 0
    crop_errors: int = 0
    last_sequence: Optional[int] = None
    last_changed_sequence: Optional[int] = None
    last_score: Optional[float] = None
    threshold: float = ROI_CHANGE_THRESHOLD
    grid: tuple = ROI_GRID
    scale: int = 1
    decoder_backend: Optional[str] = None
    decoder_fallback_reason: Optional[str] = None
    roi_x: Optional[int] = None
    roi_y: Optional[int] = None
    roi_width: Optional[int] = None
    roi_height: Optional[int] = None
    roi_bytes: Optional[int] = None
    crop_ms: Optional[float] = None
    preprocess_ms: Optional[float] = None
    roi_signature_ms: Optional[float] = None
    roi_compare_ms: Optional[float] = None
    total_ms: Optional[float] = None


class ROIChangeDetector:
    """Change detection restricted to a subtitle ROI.

    Same conceptual method as the full-frame detector (grayscale -> compact grid
    -> normalized mean absolute difference) but with its own grid and threshold,
    so small subtitle-like changes become visible without raising full-frame
    noise sensitivity.
    """

    def __init__(
        self,
        roi: NormalizedROI = DEFAULT_ROI,
        threshold: float = ROI_CHANGE_THRESHOLD,
        grid: tuple = ROI_GRID,
        scale: int = 1,
        clock: Callable[[], float] = time.monotonic,
        max_frame_age_ms: Optional[float] = None,
        decoder_backend: str = "auto",
    ) -> None:
        self._roi = roi
        self._threshold = float(threshold)
        self._grid = grid
        self._scale = int(scale)
        self._clock = clock
        self._max_frame_age_ms = max_frame_age_ms
        self._decoder_backend = decoder_backend
        self._baseline: Optional[tuple[int, ...]] = None
        self._last_seen_sequence: Optional[int] = None
        self._last_roi: Optional[tuple[int, int, bytes]] = None
        self._stats = ROIDetectorStats(threshold=self._threshold, grid=grid, scale=self._scale)

    def classify(self, frame: CaptureFrame) -> ROIDecision:
        age_ms, early = self._begin(frame.sequence, frame.captured_monotonic)
        if early is not None:
            return early
        started = self._clock()
        try:
            decoded = decode_png_ex(frame.encoded_bytes, backend=self._decoder_backend)
        except CaptureError as exc:
            return self._decode_error(frame.sequence, age_ms, exc.code)
        return self._classify_decoded(
            sequence=frame.sequence,
            age_ms=age_ms,
            started=started,
            width=decoded.width,
            height=decoded.height,
            rgba=decoded.rgba,
            decoder_backend=decoded.decoder_backend,
            decoder_fallback_reason=decoded.decoder_fallback_reason,
        )

    def classify_rgba(
        self,
        *,
        sequence: int,
        captured_monotonic: float,
        width: int,
        height: int,
        rgba: bytes,
        pixel_roi: Optional[PixelROI] = None,
        report_roi: Optional[PixelROI] = None,
        decoder_backend: Optional[str] = None,
        decoder_fallback_reason: Optional[str] = None,
    ) -> ROIDecision:
        """Classify already-decoded RGBA without re-decoding the PNG.

        ``pixel_roi`` selects the region to crop (defaults to this detector's
        normalized ROI); ``report_roi`` overrides the geometry recorded in stats.
        """
        age_ms, early = self._begin(sequence, captured_monotonic)
        if early is not None:
            return early
        started = self._clock()
        return self._classify_decoded(
            sequence=sequence,
            age_ms=age_ms,
            started=started,
            width=width,
            height=height,
            rgba=rgba,
            pixel_roi=pixel_roi,
            report_roi=report_roi,
            decoder_backend=decoder_backend,
            decoder_fallback_reason=decoder_fallback_reason,
        )

    def classify_subtitle_band(self, band_frame: SubtitleBandFrame) -> ROIDecision:
        """Classify a pre-cropped ``SubtitleBandFrame`` (no re-crop, no re-decode)."""
        return self.classify_rgba(
            sequence=band_frame.sequence,
            captured_monotonic=band_frame.captured_monotonic,
            width=band_frame.width,
            height=band_frame.height,
            rgba=band_frame.rgba,
            pixel_roi=PixelROI(0, 0, band_frame.width, band_frame.height),
            report_roi=band_frame.band_roi,
            decoder_backend=band_frame.decoder_backend,
            decoder_fallback_reason=band_frame.decoder_fallback_reason,
        )

    def _begin(self, sequence: int, captured_monotonic: float):
        observed = self._clock()
        age_ms = max(0.0, (observed - captured_monotonic) * 1000.0)
        self._stats.frames_seen += 1
        if self._last_seen_sequence is not None and sequence <= self._last_seen_sequence:
            self._stats.stale_rejected += 1
            return age_ms, ROIDecision(sequence, False, 0.0, "stale_sequence", age_ms)
        if self._max_frame_age_ms is not None and age_ms > self._max_frame_age_ms:
            self._stats.unchanged += 1
            return age_ms, ROIDecision(sequence, False, 0.0, "too_old", age_ms)
        return age_ms, None

    def _decode_error(self, sequence: int, age_ms: float, code: str) -> ROIDecision:
        self._last_seen_sequence = sequence
        self._stats.last_sequence = sequence
        self._stats.decode_errors += 1
        self._stats.changed += 1
        self._stats.last_changed_sequence = sequence
        return ROIDecision(sequence, True, 1.0, f"decode_error:{code}", age_ms)

    def _classify_decoded(
        self,
        *,
        sequence: int,
        age_ms: float,
        started: float,
        width: int,
        height: int,
        rgba: bytes,
        pixel_roi: Optional[PixelROI] = None,
        report_roi: Optional[PixelROI] = None,
        decoder_backend: Optional[str] = None,
        decoder_fallback_reason: Optional[str] = None,
    ) -> ROIDecision:
        try:
            roi = pixel_roi if pixel_roi is not None else resolve_roi(self._roi, width, height)
            crop_started = self._clock()
            roi_w, roi_h, roi_rgba = crop_rgba(rgba, width, height, roi)
            crop_done = self._clock()
            prep_started = self._clock()
            out_w, out_h, out_rgba = preprocess_roi(roi_rgba, roi_w, roi_h, self._scale)
            prep_done = self._clock()
            signature_started = self._clock()
            signature = luminance_signature(out_rgba, out_w, out_h, self._grid)
            signature_done = self._clock()
        except CaptureError as exc:
            self._last_seen_sequence = sequence
            self._stats.last_sequence = sequence
            if exc.code in ("decode_failed", "decode_unsupported", "libpng_unavailable", "native_begin_failed", "native_finish_failed"):
                return self._decode_error(sequence, age_ms, exc.code)
            self._stats.crop_errors += 1
            return ROIDecision(sequence, False, 0.0, f"roi_error:{exc.code}", age_ms)

        reported = report_roi if report_roi is not None else roi
        self._stats.decoder_backend = decoder_backend
        self._stats.decoder_fallback_reason = decoder_fallback_reason
        self._last_roi = (roi_w, roi_h, roi_rgba)
        self._stats.roi_x = reported.x
        self._stats.roi_y = reported.y
        self._stats.roi_width = reported.width
        self._stats.roi_height = reported.height
        self._stats.roi_bytes = len(roi_rgba)
        self._stats.crop_ms = round((crop_done - crop_started) * 1000.0, 3)
        self._stats.preprocess_ms = round((prep_done - prep_started) * 1000.0, 3)
        self._stats.roi_signature_ms = round((signature_done - signature_started) * 1000.0, 3)
        self._last_seen_sequence = sequence
        self._stats.last_sequence = sequence

        if self._baseline is None:
            self._baseline = signature
            self._stats.changed += 1
            self._stats.last_changed_sequence = sequence
            self._stats.last_score = 0.0
            self._stats.roi_compare_ms = 0.0
            self._stats.total_ms = round((self._clock() - started) * 1000.0, 3)
            return ROIDecision(sequence, True, 0.0, "first_frame", age_ms)

        compare_started = self._clock()
        score = signature_score(signature, self._baseline)
        self._stats.roi_compare_ms = round((self._clock() - compare_started) * 1000.0, 3)
        self._stats.last_score = score
        self._stats.total_ms = round((self._clock() - started) * 1000.0, 3)

        if score < self._threshold:
            self._stats.unchanged += 1
            return ROIDecision(sequence, False, score, "below_threshold", age_ms)

        self._baseline = signature
        self._stats.changed += 1
        self._stats.last_changed_sequence = sequence
        return ROIDecision(sequence, True, score, "changed", age_ms)

    def reset_state(self) -> None:
        self._baseline = None
        self._last_seen_sequence = None
        self._last_roi = None

    def reset_stats(self) -> None:
        self._stats = ROIDetectorStats(threshold=self._threshold, grid=self._grid, scale=self._scale)

    def reset(self) -> None:
        self.reset_state()
        self.reset_stats()

    def stats(self) -> ROIDetectorStats:
        return self._stats

    def last_roi(self) -> Optional[tuple[int, int, bytes]]:
        """Most recent ROI buffer as ``(width, height, rgba)`` (single buffer)."""
        return self._last_roi

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def roi(self) -> NormalizedROI:
        return self._roi

    @property
    def baseline_size(self) -> int:
        return len(self._baseline) if self._baseline is not None else 0
