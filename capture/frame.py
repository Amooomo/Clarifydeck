"""In-memory capture frame model.

A CaptureFrame owns its bytes, so downstream consumers never depend on the
transient Gamescope screenshot path (Steam may consume/remove it shortly after
capture).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .errors import CaptureError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MIN_PNG_BYTES = 33  # 8 sig + 25 IHDR


def validate_image_bytes(data: bytes) -> dict:
    """Validate PNG bytes and derive authoritative dimensions."""
    if not data:
        raise CaptureError("invalid_frame", "empty frame")
    if len(data) < _MIN_PNG_BYTES or data[:8] != PNG_SIGNATURE:
        raise CaptureError("invalid_frame", "not a png")
    if data[12:16] != b"IHDR":
        raise CaptureError("invalid_frame", "missing IHDR")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        raise CaptureError("invalid_frame", "invalid png dimensions")
    return {"format": "png", "width": width, "height": height, "bytes": len(data)}


@dataclass(frozen=True)
class CaptureFrame:
    width: int
    height: int
    format: str
    encoded_bytes: bytes
    captured_monotonic: float
    captured_wall_time: Optional[float]
    source_backend: str
    source_mode: str
    sequence: int
    source_path: Optional[str] = field(default=None, metadata={"authoritative": False})

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise CaptureError("invalid_frame", "invalid dimensions")
        if not self.encoded_bytes:
            raise CaptureError("invalid_frame", "empty encoded_bytes")
        if self.encoded_bytes[:8] != PNG_SIGNATURE:
            raise CaptureError("invalid_frame", "encoded_bytes is not a png")
        if self.sequence <= 0:
            raise CaptureError("invalid_frame", "sequence must be positive")

    @classmethod
    def from_png(
        cls,
        data: bytes,
        *,
        sequence: int,
        source_backend: str,
        source_mode: str,
        source_path: Optional[str] = None,
        captured_wall_time: Optional[float] = None,
    ) -> "CaptureFrame":
        info = validate_image_bytes(data)
        return cls(
            width=info["width"],
            height=info["height"],
            format=info["format"],
            encoded_bytes=data,
            captured_monotonic=time.monotonic(),
            captured_wall_time=time.time() if captured_wall_time is None else captured_wall_time,
            source_backend=source_backend,
            source_mode=source_mode,
            sequence=sequence,
            source_path=source_path,
        )


@dataclass(frozen=True)
class DecodedFrame:
    """A frame already decoded to tightly packed RGBA in memory (Phase 2N.6).

    Non-PNG capture backends (Gamescope PipeWire/GStreamer) convert their source
    buffer to RGBA exactly once at the capture boundary and hand the existing
    Region/OCR pipeline an ordinary decoded representation. No PNG is encoded,
    written, or decoded on this path.
    """

    width: int
    height: int
    format: str
    rgba: bytes
    captured_monotonic: float
    captured_wall_time: Optional[float]
    source_backend: str
    source_mode: str
    sequence: int
    decode_ms: float = 0.0
    source_format: Optional[str] = None
    source_width: Optional[int] = None
    source_height: Optional[int] = None
    capture_pull_ms: Optional[float] = None
    conversion_ms: Optional[float] = None

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise CaptureError("invalid_frame", "invalid dimensions")
        if len(self.rgba) != self.width * self.height * 4:
            raise CaptureError(
                "invalid_frame",
                f"rgba size {len(self.rgba)} != {self.width * self.height * 4}",
            )
        if self.sequence <= 0:
            raise CaptureError("invalid_frame", "sequence must be positive")
