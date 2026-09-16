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
