"""Immutable OCR result models (Phase 2F).

Raw recognized UTF-8 text is preserved as-is: no translation, no aggressive
punctuation normalization, no temporal consensus, no confidence-based dropping by
default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

Box = tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class OCRLine:
    text: str
    confidence: Optional[float]
    box: Optional[Box]

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "box": [list(point) for point in self.box] if self.box else None,
        }


@dataclass(frozen=True)
class OCRFrameResult:
    sequence: Optional[int]
    lines: tuple[OCRLine, ...]
    elapsed_ms: float
    backend: str
    roi_width: int
    roi_height: int
    det_ms: Optional[float] = None
    rec_ms: Optional[float] = None

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "backend": self.backend,
            "elapsed_ms": self.elapsed_ms,
            "det_ms": self.det_ms,
            "rec_ms": self.rec_ms,
            "roi_width": self.roi_width,
            "roi_height": self.roi_height,
            "lines": [line.to_dict() for line in self.lines],
        }
