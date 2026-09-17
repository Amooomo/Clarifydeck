"""Phase 2M.2D: per-region overlay presentation persistence (style + font size).

Pure stdlib. A single small JSON document keyed by the globally-unique
``region_id``. This file is intentionally separate from the Region Profile files
(which remain RecognitionRegion geometry/config only) and from runtime renderer
state.

Schema v1::

    {"version": 1, "regions": {"<region_id>": {"style": "white_on_black", "font_size": 20}}}

Behavior:
- missing file -> empty map (defaults), no file created on load;
- corrupt JSON / unsupported version -> empty map + ``last_error`` (never
  overwritten on load, evidence preserved);
- partially invalid entries -> valid entries load, invalid ones ignored;
- save is atomic (temp + fsync + ``os.replace``) and preserves every other
  entry, including entries for regions in inactive/deleted profiles (orphans).

Import safety: only ``overlay.protocol`` (pure stdlib); no renderer/OCR imports.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from . import protocol

PRESENTATION_VERSION = 1
MAX_PRESENTATION_BYTES = 256 * 1024


def sanitize_presentation_entry(region_id: Any, entry: Any) -> Optional[dict[str, Any]]:
    """Validate one region entry; return ``{"style", "font_size"}`` or None."""
    if not isinstance(region_id, str) or not region_id:
        return None
    if not isinstance(entry, Mapping):
        return None
    style = protocol.sanitize_region_style(entry.get("style"))
    font_size = protocol.sanitize_region_font_size(entry.get("font_size"))
    if style is None or font_size is None:
        return None
    return {"style": style, "font_size": font_size}


def parse_presentation(data: Any) -> tuple[dict[str, dict[str, Any]], Optional[str]]:
    """Parse a presentation document; return ``(entries, error)``.

    Unknown top-level/entry fields are ignored. A bad version or shape yields an
    empty map plus an explicit error code.
    """
    if not isinstance(data, dict):
        return {}, "invalid_schema"
    version = data.get("version")
    if version != PRESENTATION_VERSION:
        return {}, f"unsupported_version:{version}"
    regions = data.get("regions")
    if not isinstance(regions, dict):
        return {}, "invalid_schema"
    entries: dict[str, dict[str, Any]] = {}
    for region_id, entry in regions.items():
        normalized = sanitize_presentation_entry(region_id, entry)
        if normalized is not None:
            entries[str(region_id)] = normalized
    return entries, None


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class PresentationStore:
    """Small, strictly-validated, atomically-written per-region presentation file."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._entries: dict[str, dict[str, Any]] = {}
        self._last_error: Optional[str] = None
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    # -- load/save ---------------------------------------------------------

    def load(self) -> None:
        self._entries = {}
        self._last_error = None
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            self._last_error = f"read_failed:{type(exc).__name__}"
            return
        if len(raw) > MAX_PRESENTATION_BYTES:
            self._last_error = "config_too_large"
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._last_error = "invalid_json"
            return
        entries, error = parse_presentation(data)
        self._entries = entries
        self._last_error = error

    def entries(self) -> dict[str, dict[str, Any]]:
        return {region_id: dict(entry) for region_id, entry in self._entries.items()}

    def get(self, region_id: Any) -> Optional[dict[str, Any]]:
        key = str(region_id) if region_id is not None else ""
        entry = self._entries.get(key)
        return dict(entry) if entry is not None else None

    def update(self, region_id: Any, style: Any, font_size: Any) -> bool:
        normalized = sanitize_presentation_entry(
            region_id, {"style": style, "font_size": font_size}
        )
        if normalized is None:
            return False
        self._entries[str(region_id)] = normalized
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": PRESENTATION_VERSION,
            "regions": {region_id: dict(entry) for region_id, entry in self._entries.items()},
        }

    def save(self) -> None:
        payload = json.dumps(self.snapshot(), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        if len(payload) > MAX_PRESENTATION_BYTES:
            raise ValueError("presentation_too_large")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self._path.name + ".", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _chmod(Path(tmp_name), 0o600)
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        _chmod(self._path, 0o600)
        self._last_error = None
