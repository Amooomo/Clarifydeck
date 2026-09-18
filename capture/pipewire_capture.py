"""Gamescope PipeWire capture adapter (Phase 2N.6 prototype).

Streams the Gamescope PipeWire node through GStreamer and exposes a
pull-latest, in-memory NV12 -> RGBA frame source. This module is import-safe
without GStreamer/PyGObject on a development host: ``gi`` is imported lazily
inside :func:`load_gst`, never at module import time.

Design constraints (see ARCHITECTURE_NOTES Phase 2N.6):

- the source may deliver ~90 FPS; ClarifyDeck maps/converts only one sample per
  existing capture tick (``appsink max-buffers=1 drop=true sync=false``);
- no Python callback copies every source frame;
- no PNG encode/decode and no raw-frame files;
- NV12 is converted to RGBA exactly once, honouring stride/offset metadata.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import CaptureError

PIPEWIRE_TARGET_DEFAULT = "gamescope"
PIPEWIRE_SINK_NAME = "clarifydeck-sink"
DEFAULT_KEEPALIVE_MS = 33

# Bounded startup readiness (real device observed transient target-not-found).
DEFAULT_RETRY_WINDOW_SEC = 5.0
DEFAULT_RETRY_INTERVAL_SEC = 0.5
DEFAULT_FIRST_SAMPLE_TIMEOUT_SEC = 2.0


@dataclass(frozen=True)
class PipeWireFrame:
    """One mapped/converted PipeWire sample (tightly packed RGBA)."""

    width: int
    height: int
    rgba: bytes
    format: str
    source_width: int
    source_height: int
    captured_monotonic: float
    pts_ns: Optional[int] = None
    map_ms: float = 0.0
    conversion_ms: float = 0.0


def load_gst():
    """Lazily import PyGObject/GStreamer; never executed at module import.

    ``GstApp`` MUST be required/imported before ``Gst.parse_launch`` creates an
    appsink: without the GstApp namespace the PyGObject override is not
    installed and the appsink lacks ``try_pull_sample``.
    """
    try:
        import gi  # noqa: F401  (intentional lazy import)

        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst, GstVideo, GstApp  # noqa: WPS433

        if not Gst.is_initialized():
            Gst.init(None)
        return Gst, GstVideo, GstApp
    except Exception as exc:  # missing gi / typelib / Gst
        raise CaptureError("pipewire_unavailable", f"{type(exc).__name__}: {exc}") from exc


def _clamp(value: int) -> int:
    return 0 if value < 0 else (255 if value > 255 else value)


def _nv12_to_rgba_cv2(data: bytes, width: int, height: int, y_stride: int, uv_stride: int, uv_offset: int):
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return None
    if y_stride != width or uv_stride != width or uv_offset != y_stride * height:
        return None
    expected = width * height * 3 // 2
    if len(data) < expected:
        return None
    arr = np.frombuffer(data, dtype=np.uint8, count=expected).reshape(height * 3 // 2, width)
    rgba = cv2.cvtColor(arr, cv2.COLOR_YUV2RGBA_NV12)
    return rgba.tobytes()


def _nv12_to_rgba_numpy(data: bytes, width: int, height: int, y_stride: int, uv_stride: int, uv_offset: int):
    try:
        import numpy as np  # type: ignore
    except Exception:
        return None
    half = height // 2
    y = (
        np.frombuffer(data, dtype=np.uint8, count=y_stride * height)
        .reshape(height, y_stride)[:, :width]
        .astype(np.int32)
    )
    uv = (
        np.frombuffer(data, dtype=np.uint8, count=uv_stride * half, offset=uv_offset)
        .reshape(half, uv_stride)[:, :width]
    )
    u = uv[:, 0::2].astype(np.int32)
    v = uv[:, 1::2].astype(np.int32)
    # Upsample chroma to luma resolution (nearest neighbour, 4:2:0).
    u = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1)
    v = np.repeat(np.repeat(v, 2, axis=0), 2, axis=1)
    c = y - 16
    d = u - 128
    e = v - 128
    r = np.clip((298 * c + 409 * e + 128) >> 8, 0, 255)
    g = np.clip((298 * c - 100 * d - 208 * e + 128) >> 8, 0, 255)
    b = np.clip((298 * c + 516 * d + 128) >> 8, 0, 255)
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[:, :, 0] = r
    rgba[:, :, 1] = g
    rgba[:, :, 2] = b
    rgba[:, :, 3] = 255
    return rgba.tobytes()


def _nv12_to_rgba_python(data: bytes, width: int, height: int, y_stride: int, uv_stride: int, uv_offset: int) -> bytes:
    out = bytearray(width * height * 4)
    half = height // 2
    for j in range(half):
        uv_row = uv_offset + j * uv_stride
        row0 = (2 * j) * width * 4
        row1 = (2 * j + 1) * width * 4
        y_row0 = (2 * j) * y_stride
        y_row1 = (2 * j + 1) * y_stride
        for i in range(width):
            c0 = data[y_row0 + i] - 16
            c1 = data[y_row1 + i] - 16
            uv_index = uv_row + (i // 2) * 2
            d = data[uv_index] - 128
            e = data[uv_index + 1] - 128
            r0 = _clamp((298 * c0 + 409 * e + 128) >> 8)
            g0 = _clamp((298 * c0 - 100 * d - 208 * e + 128) >> 8)
            b0 = _clamp((298 * c0 + 516 * d + 128) >> 8)
            r1 = _clamp((298 * c1 + 409 * e + 128) >> 8)
            g1 = _clamp((298 * c1 - 100 * d - 208 * e + 128) >> 8)
            b1 = _clamp((298 * c1 + 516 * d + 128) >> 8)
            o0 = row0 + i * 4
            o1 = row1 + i * 4
            out[o0] = r0
            out[o0 + 1] = g0
            out[o0 + 2] = b0
            out[o0 + 3] = 255
            out[o1] = r1
            out[o1 + 1] = g1
            out[o1 + 2] = b1
            out[o1 + 3] = 255
    return bytes(out)


def nv12_to_rgba(
    data,
    width: int,
    height: int,
    *,
    y_stride: Optional[int] = None,
    uv_stride: Optional[int] = None,
    uv_offset: Optional[int] = None,
) -> bytes:
    """Convert one NV12 buffer to tightly packed RGBA.

    Reuses OpenCV (then numpy) when available in the OCR runtime and falls back
    to a dependency-free converter. Stride/offset metadata is honoured; a
    non-default stride never silently corrupts the image.
    """
    if width <= 0 or height <= 0:
        raise CaptureError("invalid_frame", f"dimensions {width}x{height}")
    if height % 2 != 0:
        raise CaptureError("invalid_frame", "nv12 height must be even")
    raw = data if isinstance(data, (bytes, bytearray, memoryview)) else bytes(data)
    y_stride = int(y_stride) if y_stride else width
    uv_stride = int(uv_stride) if uv_stride else width
    if y_stride < width or uv_stride < width:
        raise CaptureError("invalid_frame", f"stride {y_stride}/{uv_stride} < width {width}")
    uv_offset = int(uv_offset) if uv_offset is not None else y_stride * height
    needed = max(y_stride * height, uv_offset + uv_stride * (height // 2))
    if len(raw) < needed:
        raise CaptureError("invalid_frame", f"nv12 buffer {len(raw)} < {needed}")

    for converter in (_nv12_to_rgba_cv2, _nv12_to_rgba_numpy):
        result = converter(raw, width, height, y_stride, uv_stride, uv_offset)
        if result is not None:
            return result
    return _nv12_to_rgba_python(raw, width, height, y_stride, uv_stride, uv_offset)


class GstPipeWireAdapter:
    """Thin, bounded GStreamer wrapper. One instance owns one pipeline."""

    def __init__(
        self,
        *,
        target: str = PIPEWIRE_TARGET_DEFAULT,
        logger: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        keepalive_ms: int = DEFAULT_KEEPALIVE_MS,
        first_sample_timeout: float = DEFAULT_FIRST_SAMPLE_TIMEOUT_SEC,
    ) -> None:
        self._target = target
        self._log = logger or (lambda message: None)
        self._clock = clock
        self._keepalive_ms = int(keepalive_ms)
        self._first_sample_timeout = float(first_sample_timeout)
        self._Gst = None
        self._GstVideo = None
        self._GstApp = None
        self._pipeline = None
        self._appsink = None
        self._bus = None
        self._state = "STOPPED"
        self._source_width: Optional[int] = None
        self._source_height: Optional[int] = None
        self._source_format: Optional[str] = None
        self._startup_ms: Optional[float] = None
        self._frames_pulled = 0
        self._last_map_ms: Optional[float] = None
        self._last_conversion_ms: Optional[float] = None

    # -- description -------------------------------------------------------

    def pipeline_description(self) -> str:
        return (
            f"pipewiresrc target-object={self._target} keepalive-time={self._keepalive_ms} ! "
            "video/x-raw,format=NV12 ! "
            "queue max-size-buffers=1 leaky=downstream ! "
            f"appsink name={PIPEWIRE_SINK_NAME} max-buffers=1 drop=true sync=false emit-signals=false"
        )

    # -- lifecycle ---------------------------------------------------------

    def start_attempt(self) -> dict:
        """Build one pipeline and wait for the first sample (single attempt)."""
        self._teardown()
        # GstApp is imported before parse_launch so the appsink override (and its
        # try_pull_sample method) is installed on the created element.
        Gst, GstVideo, GstApp = load_gst()
        self._Gst = Gst
        self._GstVideo = GstVideo
        self._GstApp = GstApp
        started = self._clock()
        try:
            pipeline = Gst.parse_launch(self.pipeline_description())
        except Exception as exc:
            raise CaptureError("pipewire_pipeline_error", f"{type(exc).__name__}: {exc}") from exc
        appsink = pipeline.get_by_name(PIPEWIRE_SINK_NAME)
        if appsink is None:
            self._set_null(pipeline)
            raise CaptureError("pipewire_pipeline_error", "appsink missing")
        if not hasattr(appsink, "try_pull_sample"):
            # Confirmed device failure mode: GstApp namespace not loaded, so the
            # PyGObject appsink override (and its pull method) is absent.
            self._set_null(pipeline)
            raise CaptureError(
                "pipewire_appsink_unavailable",
                f"appsink type={type(appsink).__name__} has no try_pull_sample (GstApp override missing)",
            )
        self._pipeline = pipeline
        self._appsink = appsink
        self._bus = pipeline.get_bus()
        self._state = "STARTING"
        try:
            pipeline.set_state(Gst.State.PLAYING)
            sample = appsink.try_pull_sample(int(max(0.1, self._first_sample_timeout) * 1e9))
        except Exception as exc:
            self._teardown()
            raise CaptureError("pipewire_start_failed", f"{type(exc).__name__}: {exc}") from exc
        if sample is None:
            error = self.poll_error()
            self._teardown()
            raise CaptureError("pipewire_start_failed", error or "no sample before timeout")
        caps = sample.get_caps()
        if caps is not None:
            structure = caps.get_structure(0)
            self._source_width = structure.get_value("width")
            self._source_height = structure.get_value("height")
            self._source_format = structure.get_value("format")
        self._startup_ms = round((self._clock() - started) * 1000.0, 3)
        self._state = "PLAYING"
        self._log(
            f"[capture-pipewire] pipeline PLAYING target={self._target} "
            f"caps={self._source_width}x{self._source_height} format={self._source_format} "
            f"startup_ms={self._startup_ms}"
        )
        return self.status()

    def stop(self) -> None:
        self._teardown()

    def _set_null(self, pipeline) -> None:
        if pipeline is not None and self._Gst is not None:
            try:
                pipeline.set_state(self._Gst.State.NULL)
            except Exception:
                pass

    def _teardown(self) -> None:
        pipeline = self._pipeline
        self._pipeline = None
        self._appsink = None
        self._bus = None
        self._set_null(pipeline)
        self._state = "STOPPED"

    # -- health / frames ---------------------------------------------------

    def poll_error(self) -> Optional[str]:
        bus = self._bus
        Gst = self._Gst
        if bus is None or Gst is None:
            return None
        try:
            message = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        except Exception:
            return None
        if message is None:
            return None
        if message.type == Gst.MessageType.ERROR:
            try:
                error, _debug = message.parse_error()
                text = error.message if error is not None else "error"
            except Exception:
                text = "error"
            return f"bus_error:{text}"
        return "eos"

    def try_pull_sample(self, timeout: float) -> Optional[PipeWireFrame]:
        appsink = self._appsink
        if appsink is None:
            return None
        timeout_ns = int(max(0.0, float(timeout)) * 1e9)
        try:
            sample = appsink.try_pull_sample(timeout_ns)
        except Exception as exc:
            raise CaptureError("pipewire_pull_failed", f"{type(exc).__name__}: {exc}") from exc
        if sample is None:
            return None
        frame = self._sample_to_frame(sample)
        self._frames_pulled += 1
        return frame

    def _sample_to_frame(self, sample) -> PipeWireFrame:
        Gst = self._Gst
        buffer = sample.get_buffer()
        caps = sample.get_caps()
        if caps is None:
            raise CaptureError("pipewire_missing_caps", "sample has no caps")
        structure = caps.get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        fmt = str(structure.get_value("format"))

        map_started = self._clock()
        mapinfo = buffer.map(Gst.MapFlags.READ)
        try:
            data = bytes(mapinfo.data)
        finally:
            try:
                buffer.unmap(mapinfo)
            except Exception:
                pass
        map_ms = round((self._clock() - map_started) * 1000.0, 3)

        y_stride, uv_stride, uv_offset = width, width, None
        try:
            meta = buffer.get_video_meta()
        except Exception:
            meta = None
        if meta is not None:
            try:
                strides = list(meta.stride)
                offsets = list(meta.offset)
                if strides:
                    y_stride = int(strides[0]) or width
                if len(strides) > 1:
                    uv_stride = int(strides[1]) or width
                if len(offsets) > 1:
                    uv_offset = int(offsets[1])
            except Exception:
                y_stride, uv_stride, uv_offset = width, width, None

        conversion_started = self._clock()
        if fmt.upper() != "NV12":
            raise CaptureError("pipewire_unsupported_format", fmt)
        rgba = nv12_to_rgba(
            data, width, height, y_stride=y_stride, uv_stride=uv_stride, uv_offset=uv_offset
        )
        conversion_ms = round((self._clock() - conversion_started) * 1000.0, 3)

        pts = None
        try:
            raw_pts = buffer.pts
            if raw_pts is not None and raw_pts != Gst.CLOCK_TIME_NONE:
                pts = int(raw_pts)
        except Exception:
            pts = None

        self._source_width = width
        self._source_height = height
        self._source_format = fmt
        self._last_map_ms = map_ms
        self._last_conversion_ms = conversion_ms
        return PipeWireFrame(
            width=width,
            height=height,
            rgba=rgba,
            format=fmt,
            source_width=width,
            source_height=height,
            captured_monotonic=self._clock(),
            pts_ns=pts,
            map_ms=map_ms,
            conversion_ms=conversion_ms,
        )

    def status(self) -> dict:
        return {
            "backend": "pipewire",
            "state": self._state,
            "target": self._target,
            "source_width": self._source_width,
            "source_height": self._source_height,
            "source_format": self._source_format,
            "startup_ms": self._startup_ms,
            "frames_pulled": self._frames_pulled,
            "last_map_ms": self._last_map_ms,
            "last_conversion_ms": self._last_conversion_ms,
        }


def create_gst_adapter(
    *,
    target: str = PIPEWIRE_TARGET_DEFAULT,
    logger: Optional[Callable[[str], None]] = None,
    clock: Callable[[], float] = time.monotonic,
    keepalive_ms: int = DEFAULT_KEEPALIVE_MS,
    first_sample_timeout: float = DEFAULT_FIRST_SAMPLE_TIMEOUT_SEC,
) -> GstPipeWireAdapter:
    """Construct the adapter without importing GStreamer (lazy until start)."""
    return GstPipeWireAdapter(
        target=target,
        logger=logger,
        clock=clock,
        keepalive_ms=keepalive_ms,
        first_sample_timeout=first_sample_timeout,
    )
