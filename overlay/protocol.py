"""ClarifyDeck external overlay IPC protocol.

Newline-delimited JSON over an AF_UNIX SOCK_STREAM socket. Kept dependency
free and language neutral so a future native renderer can speak the same
protocol.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

MAX_MESSAGE_BYTES = 64 * 1024
MAX_TEXT_CHARS = 4096
MAX_PREVIEW_REGIONS = 8
MAX_REGION_LABEL_CHARS = 32

DEFAULT_FONT_SIZE = 24
DEFAULT_BACKGROUND_ALPHA = 0.55

# Phase 2M.2A per-region text panel styles. Runtime-only: never persisted.
STYLE_WHITE_ON_BLACK = "white_on_black"
STYLE_BLACK_ON_WHITE = "black_on_white"
STYLES = (STYLE_WHITE_ON_BLACK, STYLE_BLACK_ON_WHITE)
DEFAULT_STYLE = STYLE_WHITE_ON_BLACK
PANEL_ALPHA = 0.65

# Phase 2M.2B per-region font size. Runtime-only: never persisted.
DEFAULT_REGION_FONT_SIZE = 20
MIN_REGION_FONT_SIZE = 14
MAX_REGION_FONT_SIZE = 48
REGION_FONT_SIZE_STEP = 2


def runtime_dir() -> Path:
    override = os.environ.get("CLARIFYDECK_OVERLAY_RUNTIME_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not (base and Path(base).is_dir()):
        # Backend may run as root without XDG_RUNTIME_DIR. Prefer the deck
        # user's runtime dir so tools run as deck can reach the socket.
        deck_runtime = Path("/run/user/1000")
        if deck_runtime.is_dir():
            base = str(deck_runtime)
    if base and Path(base).is_dir():
        return Path(base) / "clarifydeck"
    return Path("/tmp") / "clarifydeck"


def socket_path() -> Path:
    override = os.environ.get("CLARIFYDECK_OVERLAY_SOCKET")
    if override:
        return Path(override)
    return runtime_dir() / "overlay.sock"


def renderer_log_path() -> Path:
    return runtime_dir() / "renderer.log"


def renderer_lock_path() -> Path:
    return runtime_dir() / "renderer.lock"


def renderer_pid_path() -> Path:
    return runtime_dir() / "renderer.pid"


def truncate_text(text: str) -> str:
    text = (text or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    return text


def encode_message(payload: dict[str, Any]) -> bytes:
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("overlay IPC message too large")
    return data


def decode_message(line: bytes) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def sanitize_preview_regions(value: Any) -> list[dict[str, Any]]:
    """Validate/normalize a region-preview payload. Never raises; drops bad entries."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in value[:MAX_PREVIEW_REGIONS]:
        if not isinstance(entry, dict):
            continue
        try:
            x = float(entry["x"])
            y = float(entry["y"])
            w = float(entry["w"])
            h = float(entry["h"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (x, y, w, h)):
            continue
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0):
            continue
        if x + w > 1.0001 or y + h > 1.0001:
            continue
        label = entry.get("label")
        if not isinstance(label, str):
            label = ""
        region_id = entry.get("region_id")
        out.append(
            {
                "region_id": str(region_id)[:64] if region_id is not None else "",
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "selected": bool(entry.get("selected", False)),
                "primary": bool(entry.get("primary", False)),
                "enabled": bool(entry.get("enabled", True)),
                "label": label[:MAX_REGION_LABEL_CHARS],
            }
        )
    return out


def preview_pixel_rect(region: dict[str, Any], width: int, height: int) -> tuple[float, float, float, float]:
    """Normalized region -> pixel rect on the renderer surface."""
    return (
        float(region["x"]) * width,
        float(region["y"]) * height,
        float(region["w"]) * width,
        float(region["h"]) * height,
    )


def sanitize_region_text(region_id: Any, rect: Any, text: Any) -> Optional[dict[str, Any]]:
    """Validate/normalize a per-region text-block payload; return block or None."""
    if not isinstance(region_id, str) or not region_id or not isinstance(text, str):
        return None
    if not isinstance(rect, dict):
        return None
    try:
        x = float(rect["x"])
        y = float(rect["y"])
        w = float(rect["w"])
        h = float(rect["h"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        return None
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0):
        return None
    return {"rect": {"x": x, "y": y, "w": w, "h": h}, "text": text}


def sanitize_region_style(value: Any) -> Optional[str]:
    """Validate a per-region panel style; return the canonical value or None."""
    return value if isinstance(value, str) and value in STYLES else None


def style_colors(style: Any) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float]]:
    """Return ``(text_rgba, panel_rgba)`` for a style.

    Unknown/missing styles fall back to the default WHITE_ON_BLACK. The panel is
    always drawn at the single fixed ``PANEL_ALPHA``.
    """
    if style == STYLE_BLACK_ON_WHITE:
        return (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0, PANEL_ALPHA)
    return (1.0, 1.0, 1.0, 1.0), (0.0, 0.0, 0.0, PANEL_ALPHA)


def sanitize_region_font_size(value: Any) -> Optional[int]:
    """Validate a per-region font size; return a canonical integer or None.

    Accepts only an in-range integer (or an integral finite float). Rejects
    bools, non-numeric types, non-integral floats, NaN/Infinity, and values
    outside ``MIN_REGION_FONT_SIZE .. MAX_REGION_FONT_SIZE``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        size = value
    elif isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None
        size = int(value)
    else:
        return None
    if size < MIN_REGION_FONT_SIZE or size > MAX_REGION_FONT_SIZE:
        return None
    return size


def region_line_height(font_size: float) -> float:
    """Existing region-text line-height rule (unchanged from the 20px baseline)."""
    return float(font_size) * 1.3


def wrap_text(text: str, max_width: float, measure: Any) -> list[str]:
    """Character-wrap each source line to ``max_width`` using ``measure(str)->float``.

    Newlines are preserved as line breaks; characters are never dropped.
    """
    lines: list[str] = []
    for raw in str(text).split("\n"):
        if not raw:
            lines.append("")
            continue
        current = ""
        for char in raw:
            candidate = current + char
            if current and measure(candidate) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current)
    return lines


def clip_lines(lines: list[str], line_height: float, max_height: float) -> list[str]:
    """Vertical clip: keep whole lines that fit inside ``max_height``.

    A positive drawable height always yields at least one line, even when it is
    shorter than ``line_height``: a short but drawable region must still render
    text. The glyphs stay clipped to the exact region rectangle by the caller, so
    nothing leaks outside it. No drawable area (``max_height <= 0``) or an
    invalid line height yields no lines.
    """
    if line_height <= 0 or max_height <= 0:
        return []
    capacity = int(max_height // line_height)
    if capacity < 1:
        capacity = 1
    return lines[:capacity]
