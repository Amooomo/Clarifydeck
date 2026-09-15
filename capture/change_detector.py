"""Frame freshness + change detection foundation (Phase 2D).

Consumes a CaptureFrame's in-memory PNG bytes and classifies it as changed /
unchanged against a single retained baseline.

Decoding has two backends:
- ``libpng`` (native, via the libpng simplified API ``png_image_*`` through
  ctypes) is used for eligible 8-bit RGB/RGBA non-interlaced PNGs;
- ``stdlib`` (a small pure-Python decoder) is the reference/fallback.

Bounded state: one baseline signature + scalars only. No frame history.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import time
import zlib
from dataclasses import dataclass, replace
from typing import Callable, Optional

from .errors import CaptureError
from .frame import CaptureFrame

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

DEFAULT_THRESHOLD = 0.005
DEFAULT_GRID = (32, 20)

PNG_IMAGE_VERSION = 1
PNG_FORMAT_FLAG_ALPHA = 0x01
PNG_FORMAT_FLAG_COLOR = 0x02
PNG_FORMAT_RGB = PNG_FORMAT_FLAG_COLOR
PNG_FORMAT_RGBA = PNG_FORMAT_FLAG_COLOR | PNG_FORMAT_FLAG_ALPHA

_MAX_DIMENSION = 16384
_MAX_PIXELS = 4096 * 4096


@dataclass
class PngStructure:
    width: int
    height: int
    bit_depth: int
    color_type: int
    interlace: int
    idat_bytes: int
    bpp: int
    row_bytes: int
    filter_histogram: dict


@dataclass(frozen=True)
class DecodeResult:
    width: int
    height: int
    rgba: bytes
    decoder_backend: str
    decoder_fallback_reason: Optional[str]
    structure: PngStructure
    parse_chunks_ms: float = 0.0
    zlib_ms: Optional[float] = None
    unfilter_ms: Optional[float] = None
    pixel_expand_ms: Optional[float] = None
    decode_total_ms: float = 0.0


def _parse_png(data: bytes) -> tuple[PngStructure, bytearray]:
    if len(data) < 33 or data[:8] != PNG_SIGNATURE:
        raise CaptureError("decode_failed", "not a png")
    pos = 8
    size = len(data)
    width = height = bit_depth = color_type = interlace = 0
    idat = bytearray()
    while pos + 8 <= size:
        length = int.from_bytes(data[pos : pos + 4], "big")
        chunk_type = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            if len(chunk) < 13:
                raise CaptureError("decode_failed", "truncated ihdr")
            width = int.from_bytes(chunk[0:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            bit_depth = chunk[8]
            color_type = chunk[9]
            interlace = chunk[12]
        elif chunk_type == b"IDAT":
            idat += chunk
        elif chunk_type == b"IEND":
            break
    if width <= 0 or height <= 0 or width > _MAX_DIMENSION or height > _MAX_DIMENSION:
        raise CaptureError("decode_failed", "invalid dimensions")
    channels = 4 if color_type == 6 else (3 if color_type == 2 else 1)
    structure = PngStructure(
        width=width,
        height=height,
        bit_depth=bit_depth,
        color_type=color_type,
        interlace=interlace,
        idat_bytes=len(idat),
        bpp=channels,
        row_bytes=width * channels,
        filter_histogram={},
    )
    return structure, idat


def _native_ineligible_reason(structure: PngStructure) -> Optional[str]:
    if structure.bit_depth != 8:
        return "unsupported_bit_depth"
    if structure.color_type not in (2, 6):
        return "unsupported_color_type"
    if structure.interlace != 0:
        return "interlaced_png"
    return None


def _filter_histogram(raw: bytes, structure: PngStructure) -> dict:
    if structure.bit_depth != 8 or structure.interlace != 0:
        return {}
    stride = structure.row_bytes + 1
    histogram: dict = {}
    for y in range(structure.height):
        index = y * stride
        if index >= len(raw):
            break
        filter_type = raw[index]
        histogram[filter_type] = histogram.get(filter_type, 0) + 1
    return histogram


def decode_png_stdlib(
    data: bytes,
    structure: Optional[PngStructure] = None,
    idat: Optional[bytearray] = None,
    collect_filters: bool = False,
    parse_chunks_ms: float = 0.0,
    fallback_reason: Optional[str] = None,
) -> DecodeResult:
    """Reference pure-Python PNG decoder (8-bit RGB/RGBA, non-interlaced)."""
    if structure is None or idat is None:
        structure, idat = _parse_png(data)
    if structure.bit_depth != 8 or structure.interlace != 0 or structure.color_type not in (2, 6):
        raise CaptureError(
            "decode_unsupported",
            f"bit_depth={structure.bit_depth} color={structure.color_type} interlace={structure.interlace}",
        )
    width, height = structure.width, structure.height
    channels = 4 if structure.color_type == 6 else 3
    stride = width * channels

    zlib_started = time.perf_counter()
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise CaptureError("decode_failed", f"zlib: {exc}") from exc
    zlib_ms = (time.perf_counter() - zlib_started) * 1000.0

    expected = height * (stride + 1)
    if len(raw) < expected:
        raise CaptureError("decode_failed", f"truncated image data {len(raw)}<{expected}")

    if collect_filters:
        structure.filter_histogram = _filter_histogram(raw, structure)

    out = bytearray(width * height * 4)
    prev = bytearray(stride)
    rpos = 0
    unfilter_started = time.perf_counter()
    for y in range(height):
        filter_type = raw[rpos]
        rpos += 1
        if filter_type > 4:
            raise CaptureError("decode_failed", f"bad filter {filter_type}")
        line = bytearray(raw[rpos : rpos + stride])
        rpos += stride
        if filter_type == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up = prev[i]
                up_left = prev[i - channels] if i >= channels else 0
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                if pa <= pb and pa <= pc:
                    pred = left
                elif pb <= pc:
                    pred = up
                else:
                    pred = up_left
                line[i] = (line[i] + pred) & 0xFF
        base = y * width * 4
        end = base + width * 4
        if channels == 4:
            out[base:end] = line
        else:
            out[base:end:4] = line[0::3]
            out[base + 1 : end : 4] = line[1::3]
            out[base + 2 : end : 4] = line[2::3]
            out[base + 3 : end : 4] = b"\xff" * width
        prev = line
    unfilter_ms = (time.perf_counter() - unfilter_started) * 1000.0
    return DecodeResult(
        width=width,
        height=height,
        rgba=bytes(out),
        decoder_backend="stdlib",
        decoder_fallback_reason=fallback_reason,
        structure=structure,
        parse_chunks_ms=round(parse_chunks_ms, 3),
        zlib_ms=round(zlib_ms, 3),
        unfilter_ms=round(unfilter_ms, 3),
        pixel_expand_ms=0.0,
        decode_total_ms=round(parse_chunks_ms + zlib_ms + unfilter_ms, 3),
    )


class _PngImage(ctypes.Structure):
    _fields_ = [
        ("opaque", ctypes.c_void_p),
        ("version", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("format", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("colormap_entries", ctypes.c_uint32),
        ("warning_or_error", ctypes.c_uint32),
        ("message", ctypes.c_char * 64),
    ]


class _LibpngDecoder:
    def __init__(self) -> None:
        self.lib = None
        self.reason: Optional[str] = None
        self.version: Optional[str] = None
        self._load()

    def _load(self) -> None:
        candidates = [
            ctypes.util.find_library("png16"),
            ctypes.util.find_library("png"),
            "libpng16.so.16",
            "libpng16.so",
            "libpng.so.3",
            "libpng16.dll",
            "libpng.dll",
        ]
        saw_library = False
        for name in candidates:
            if not name:
                continue
            try:
                lib = ctypes.CDLL(name)
            except OSError:
                continue
            saw_library = True
            if not hasattr(lib, "png_image_begin_read_from_memory"):
                self.reason = "libpng_symbol_missing"
                continue
            lib.png_image_begin_read_from_memory.argtypes = [
                ctypes.POINTER(_PngImage),
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            lib.png_image_begin_read_from_memory.restype = ctypes.c_int
            lib.png_image_finish_read.argtypes = [
                ctypes.POINTER(_PngImage),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int32,
                ctypes.c_void_p,
            ]
            lib.png_image_finish_read.restype = ctypes.c_int
            lib.png_image_free.argtypes = [ctypes.POINTER(_PngImage)]
            lib.png_image_free.restype = None
            self.lib = lib
            self.reason = None
            self.version = self._read_version(lib)
            return
        if not saw_library:
            self.reason = "libpng_unavailable"

    def _read_version(self, lib) -> Optional[str]:
        if not hasattr(lib, "png_get_libpng_ver"):
            return None
        lib.png_get_libpng_ver.argtypes = [ctypes.c_void_p]
        lib.png_get_libpng_ver.restype = ctypes.c_char_p
        try:
            raw = lib.png_get_libpng_ver(None)
        except Exception:
            return None
        if not raw:
            return None
        return raw.decode("ascii", errors="replace")

    @property
    def available(self) -> bool:
        return self.lib is not None


_LIBPNG = _LibpngDecoder()


def _png_message(image: _PngImage) -> str:
    try:
        raw = bytes(image.message).split(b"\0", 1)[0]
        return raw.decode("utf-8", errors="replace") or "libpng error"
    except Exception:
        return "libpng error"


def decode_png_fast(png_bytes: bytes, meta: Optional[PngStructure] = None) -> DecodeResult:
    """Native libpng simplified-API decode to RGBA (eligible formats only)."""
    lib = _LIBPNG.lib
    if lib is None:
        raise CaptureError("libpng_unavailable", _LIBPNG.reason or "libpng unavailable")

    started = time.perf_counter()
    image = _PngImage()
    ctypes.memset(ctypes.byref(image), 0, ctypes.sizeof(image))
    image.version = PNG_IMAGE_VERSION

    input_buf = ctypes.create_string_buffer(png_bytes, len(png_bytes))
    begun = False
    finished = False
    try:
        ok = lib.png_image_begin_read_from_memory(
            ctypes.byref(image), ctypes.cast(input_buf, ctypes.c_void_p), len(png_bytes)
        )
        if not ok:
            raise CaptureError("native_begin_failed", _png_message(image))
        begun = True

        width = int(image.width)
        height = int(image.height)
        if meta is not None and (width != meta.width or height != meta.height):
            raise CaptureError("native_dimensions_mismatch", f"{width}x{height} != {meta.width}x{meta.height}")
        if width <= 0 or height <= 0 or width > _MAX_DIMENSION or height > _MAX_DIMENSION:
            raise CaptureError("native_dimensions_invalid", f"{width}x{height}")
        pixel_count = width * height
        if pixel_count <= 0 or pixel_count > _MAX_PIXELS:
            raise CaptureError("native_dimensions_invalid", f"pixels={pixel_count}")

        image.format = PNG_FORMAT_RGBA
        out = (ctypes.c_ubyte * (pixel_count * 4))()
        ok = lib.png_image_finish_read(
            ctypes.byref(image), None, ctypes.cast(out, ctypes.c_void_p), 0, None
        )
        if not ok:
            raise CaptureError("native_finish_failed", _png_message(image))
        finished = True
        rgba = bytes(out)
    finally:
        if begun and not finished and image.opaque:
            lib.png_image_free(ctypes.byref(image))

    total_ms = (time.perf_counter() - started) * 1000.0
    return DecodeResult(
        width=width,
        height=height,
        rgba=rgba,
        decoder_backend="libpng",
        decoder_fallback_reason=None,
        structure=meta if meta is not None else _parse_png(png_bytes)[0],
        zlib_ms=None,
        unfilter_ms=None,
        pixel_expand_ms=None,
        decode_total_ms=round(total_ms, 3),
    )


def decode_png_ex(data: bytes, backend: str = "auto", collect_filters: bool = False) -> DecodeResult:
    parse_started = time.perf_counter()
    structure, idat = _parse_png(data)
    parse_ms = (time.perf_counter() - parse_started) * 1000.0

    reason: Optional[str] = None
    if backend == "auto":
        if _LIBPNG.available:
            backend = "libpng"
        else:
            backend = "stdlib"
            reason = _LIBPNG.reason or "libpng_unavailable"

    if backend == "libpng":
        reason = _native_ineligible_reason(structure)
        if reason is None and not _LIBPNG.available:
            reason = _LIBPNG.reason or "libpng_unavailable"
        if reason is None:
            try:
                result = decode_png_fast(data, structure)
                if collect_filters:
                    try:
                        structure.filter_histogram = _filter_histogram(zlib.decompress(bytes(idat)), structure)
                    except zlib.error:
                        structure.filter_histogram = {}
                return replace(result, parse_chunks_ms=round(parse_ms, 3), decode_total_ms=round(parse_ms + result.decode_total_ms, 3))
            except CaptureError as exc:
                reason = exc.code

    result = decode_png_stdlib(
        data,
        structure,
        idat,
        collect_filters=collect_filters,
        parse_chunks_ms=parse_ms,
        fallback_reason=reason,
    )
    return result


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    result = decode_png_ex(data)
    return result.width, result.height, result.rgba


def luminance_signature(rgba: bytes, width: int, height: int, grid: tuple[int, int] = DEFAULT_GRID) -> tuple[int, ...]:
    """Downsampled grayscale signature (2x2 samples per grid cell)."""
    gw, gh = grid
    values: list[int] = []
    for gy in range(gh):
        cy = min(height - 1, int((gy + 0.5) * height / gh))
        for gx in range(gw):
            cx = min(width - 1, int((gx + 0.5) * width / gw))
            total = 0
            count = 0
            for dy in (0, 1):
                y = min(height - 1, cy + dy)
                row = y * width
                for dx in (0, 1):
                    x = min(width - 1, cx + dx)
                    o = (row + x) * 4
                    total += (30 * rgba[o] + 59 * rgba[o + 1] + 11 * rgba[o + 2]) // 100
                    count += 1
            values.append(total // count)
    return tuple(values)


def signature_score(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (len(a) * 255.0)


@dataclass(frozen=True)
class ChangeDecision:
    sequence: int
    changed: bool
    score: float
    reason: str
    age_ms: float


@dataclass
class ChangeDetectorStats:
    frames_seen: int = 0
    changed: int = 0
    unchanged: int = 0
    stale_rejected: int = 0
    decode_errors: int = 0
    last_sequence: Optional[int] = None
    last_changed_sequence: Optional[int] = None
    last_score: Optional[float] = None
    threshold: float = DEFAULT_THRESHOLD
    decoder_backend: Optional[str] = None
    decoder_fallback_reason: Optional[str] = None
    libpng_version: Optional[str] = None
    native_decode_count: int = 0
    stdlib_decode_count: int = 0
    native_fallback_count: int = 0
    png_width: Optional[int] = None
    png_height: Optional[int] = None
    png_bit_depth: Optional[int] = None
    png_color_type: Optional[int] = None
    png_interlace: Optional[int] = None
    png_filters: Optional[dict] = None
    parse_chunks_ms: Optional[float] = None
    zlib_ms: Optional[float] = None
    unfilter_ms: Optional[float] = None
    pixel_expand_ms: Optional[float] = None
    decode_ms: Optional[float] = None
    avg_decode_ms: Optional[float] = None
    signature_ms: Optional[float] = None
    compare_ms: Optional[float] = None
    total_ms: Optional[float] = None


class FrameChangeDetector:
    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        grid: tuple[int, int] = DEFAULT_GRID,
        clock: Callable[[], float] = time.monotonic,
        max_frame_age_ms: Optional[float] = None,
        decoder_backend: str = "auto",
        collect_filters: bool = False,
    ) -> None:
        self._threshold = float(threshold)
        self._grid = grid
        self._clock = clock
        self._max_frame_age_ms = max_frame_age_ms
        self._decoder_backend = decoder_backend
        self._collect_filters = collect_filters
        self._baseline: Optional[tuple[int, ...]] = None
        self._last_seen_sequence: Optional[int] = None
        self._decode_total_accum = 0.0
        self._decode_count = 0
        self._stats = ChangeDetectorStats(threshold=self._threshold)

    def classify(self, frame: CaptureFrame) -> ChangeDecision:
        observed = self._clock()
        age_ms = max(0.0, (observed - frame.captured_monotonic) * 1000.0)
        self._stats.frames_seen += 1

        if self._last_seen_sequence is not None and frame.sequence <= self._last_seen_sequence:
            self._stats.stale_rejected += 1
            return ChangeDecision(frame.sequence, False, 0.0, "stale_sequence", age_ms)

        if self._max_frame_age_ms is not None and age_ms > self._max_frame_age_ms:
            self._stats.unchanged += 1
            return ChangeDecision(frame.sequence, False, 0.0, "too_old", age_ms)

        started = self._clock()
        try:
            decoded = decode_png_ex(
                frame.encoded_bytes,
                backend=self._decoder_backend,
                collect_filters=self._collect_filters,
            )
            signature_started = self._clock()
            signature = luminance_signature(decoded.rgba, decoded.width, decoded.height, self._grid)
            signature_done = self._clock()
        except CaptureError as exc:
            self._stats.decode_errors += 1
            self._last_seen_sequence = frame.sequence
            self._stats.last_sequence = frame.sequence
            self._stats.changed += 1
            self._stats.last_changed_sequence = frame.sequence
            return ChangeDecision(frame.sequence, True, 1.0, f"decode_error:{exc.code}", age_ms)

        self._stats.decoder_backend = decoded.decoder_backend
        self._stats.decoder_fallback_reason = decoded.decoder_fallback_reason
        self._stats.libpng_version = _LIBPNG.version
        if decoded.decoder_backend == "libpng":
            self._stats.native_decode_count += 1
        else:
            self._stats.stdlib_decode_count += 1
            if decoded.decoder_fallback_reason is not None:
                self._stats.native_fallback_count += 1
        self._stats.png_width = decoded.structure.width
        self._stats.png_height = decoded.structure.height
        self._stats.png_bit_depth = decoded.structure.bit_depth
        self._stats.png_color_type = decoded.structure.color_type
        self._stats.png_interlace = decoded.structure.interlace
        if decoded.structure.filter_histogram:
            self._stats.png_filters = decoded.structure.filter_histogram
        self._stats.parse_chunks_ms = decoded.parse_chunks_ms
        self._stats.zlib_ms = decoded.zlib_ms
        self._stats.unfilter_ms = decoded.unfilter_ms
        self._stats.pixel_expand_ms = decoded.pixel_expand_ms
        self._stats.decode_ms = decoded.decode_total_ms
        self._decode_total_accum += decoded.decode_total_ms
        self._decode_count += 1
        self._stats.avg_decode_ms = round(self._decode_total_accum / self._decode_count, 3)
        self._stats.signature_ms = round((signature_done - signature_started) * 1000.0, 3)
        self._last_seen_sequence = frame.sequence
        self._stats.last_sequence = frame.sequence

        if self._baseline is None:
            self._baseline = signature
            self._stats.changed += 1
            self._stats.last_changed_sequence = frame.sequence
            self._stats.last_score = 0.0
            self._stats.compare_ms = 0.0
            self._stats.total_ms = round((self._clock() - started) * 1000.0, 3)
            return ChangeDecision(frame.sequence, True, 0.0, "first_frame", age_ms)

        compare_started = self._clock()
        score = signature_score(signature, self._baseline)
        self._stats.compare_ms = round((self._clock() - compare_started) * 1000.0, 3)
        self._stats.last_score = score
        self._stats.total_ms = round((self._clock() - started) * 1000.0, 3)

        if score < self._threshold:
            self._stats.unchanged += 1
            return ChangeDecision(frame.sequence, False, score, "below_threshold", age_ms)

        self._baseline = signature
        self._stats.changed += 1
        self._stats.last_changed_sequence = frame.sequence
        return ChangeDecision(frame.sequence, True, score, "changed", age_ms)

    def reset_state(self) -> None:
        self._baseline = None
        self._last_seen_sequence = None
        self._decode_total_accum = 0.0
        self._decode_count = 0

    def reset_stats(self) -> None:
        self._stats = ChangeDetectorStats(threshold=self._threshold)

    def reset(self) -> None:
        self.reset_state()
        self.reset_stats()

    def stats(self) -> ChangeDetectorStats:
        return self._stats

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def baseline_size(self) -> int:
        return len(self._baseline) if self._baseline is not None else 0
