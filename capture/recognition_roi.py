"""Phase 2E.2: user-configurable recognition ROI foundation.

The authoritative working region is a full-frame ``NormalizedROI``. Resolution
priority:

    1. user per-game override
    2. game profile preset
    3. user global/default override
    4. built-in default ROI

The broad ROI / subtitle band from Phase 2E/2E.1 remain internal presets and
diagnostics; they are not the product configuration path.

Bounded state: one small config file, one resolved ROI. No frame history.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .errors import CaptureError
from .roi import (
    DEFAULT_PROFILE_STORE,
    DEFAULT_ROI,
    ROIProfileStore,
    NormalizedROI,
    PixelROI,
    crop_rgba,
    resolve_roi,
)

CONFIG_VERSION = 1
MAX_CONFIG_BYTES = 64 * 1024
MAX_APP_ID_LENGTH = 64
MIN_ROI_SIZE = 0.02

SOURCE_USER = "user"
SOURCE_GAME_PROFILE = "game_profile"
SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class RecognitionROI:
    roi: NormalizedROI
    source: str


@dataclass(frozen=True)
class ROIFrame:
    width: int
    height: int
    rgba: bytes
    roi: PixelROI
    source: str

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise CaptureError("invalid_roi", "invalid roi frame size")
        if len(self.rgba) != self.width * self.height * 4:
            raise CaptureError("invalid_roi", "roi frame buffer mismatch")


def parse_roi(value: Any) -> NormalizedROI:
    """Parse+validate a full-frame ROI mapping (rejects, never clamps)."""
    if isinstance(value, NormalizedROI):
        roi = value
    elif isinstance(value, Mapping):
        try:
            roi = NormalizedROI(
                x=float(value["x"]),
                y=float(value["y"]),
                width=float(value["width"]),
                height=float(value["height"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureError("invalid_roi", f"malformed roi: {exc}") from exc
    else:
        raise CaptureError("invalid_roi", "roi must be a mapping")
    return validate_user_roi(roi)


def validate_user_roi(roi: NormalizedROI) -> NormalizedROI:
    """Strict bounds for user configuration (0..1, in-frame, min size)."""
    if not isinstance(roi, NormalizedROI):
        raise CaptureError("invalid_roi", "roi must be a NormalizedROI")
    if not (0.0 <= roi.x < 1.0) or not (0.0 <= roi.y < 1.0):
        raise CaptureError("invalid_roi", "x/y must be in [0, 1)")
    if not (0.0 < roi.width <= 1.0) or not (0.0 < roi.height <= 1.0):
        raise CaptureError("invalid_roi", "width/height must be in (0, 1]")
    if roi.width < MIN_ROI_SIZE or roi.height < MIN_ROI_SIZE:
        raise CaptureError("invalid_roi", f"width/height must be >= {MIN_ROI_SIZE}")
    if roi.x + roi.width > 1.0 or roi.y + roi.height > 1.0:
        raise CaptureError("invalid_roi", "roi must stay inside the frame")
    return roi


def roi_to_dict(roi: NormalizedROI) -> dict[str, float]:
    return {"x": roi.x, "y": roi.y, "width": roi.width, "height": roi.height}


@dataclass
class ROIConfig:
    version: int = CONFIG_VERSION
    default_roi: Optional[NormalizedROI] = None
    games: dict[str, NormalizedROI] = field(default_factory=dict)


def default_config_path() -> Path:
    override = os.environ.get("CLARIFYDECK_ROI_CONFIG")
    if override:
        return Path(override)
    base = os.environ.get("CLARIFYDECK_CONFIG_DIR")
    if base:
        return Path(base) / "recognition_roi.json"
    return Path(os.path.expanduser("~")) / ".config" / "clarifydeck" / "recognition_roi.json"


class ROIConfigStore:
    """Small, strictly-validated, atomically-written JSON config."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._config = ROIConfig()
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
        self._config = ROIConfig()
        self._last_error = None
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            self._last_error = f"read_failed:{type(exc).__name__}"
            return
        if len(raw) > MAX_CONFIG_BYTES:
            self._last_error = "config_too_large"
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._last_error = "invalid_json"
            return
        if not isinstance(data, dict):
            self._last_error = "invalid_schema"
            return
        version = data.get("version")
        if version != CONFIG_VERSION:
            self._last_error = f"unsupported_version:{version}"
            return

        default_roi = self._safe_roi(data.get("default_roi"))
        games: dict[str, NormalizedROI] = {}
        raw_games = data.get("games")
        if isinstance(raw_games, dict):
            for app_id, entry in raw_games.items():
                if not isinstance(app_id, str) or not app_id or len(app_id) > MAX_APP_ID_LENGTH:
                    continue
                candidate = entry.get("roi") if isinstance(entry, dict) else entry
                roi = self._safe_roi(candidate)
                if roi is not None:
                    games[app_id] = roi
        self._config = ROIConfig(version=CONFIG_VERSION, default_roi=default_roi, games=games)

    @staticmethod
    def _safe_roi(value: Any) -> Optional[NormalizedROI]:
        if value is None:
            return None
        try:
            return parse_roi(value)
        except CaptureError:
            return None

    def _to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"version": CONFIG_VERSION}
        payload["default_roi"] = roi_to_dict(self._config.default_roi) if self._config.default_roi else None
        payload["games"] = {
            app_id: {"roi": roi_to_dict(roi)} for app_id, roi in sorted(self._config.games.items())
        }
        return payload

    def save(self) -> None:
        payload = json.dumps(self._to_dict(), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        if len(payload) > MAX_CONFIG_BYTES:
            raise CaptureError("config_too_large", f"{len(payload)} bytes")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self._path.name + ".", suffix=".tmp", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self._last_error = None

    # -- access ------------------------------------------------------------

    def get(self, app_id: Optional[str] = None) -> Optional[NormalizedROI]:
        if app_id is not None:
            return self._config.games.get(str(app_id))
        return self._config.default_roi

    def set(self, app_id: Optional[str], roi: NormalizedROI) -> NormalizedROI:
        validated = validate_user_roi(roi)
        if app_id is not None:
            key = str(app_id)
            if not key or len(key) > MAX_APP_ID_LENGTH:
                raise CaptureError("invalid_app_id", "app_id out of range")
            self._config.games[key] = validated
        else:
            self._config.default_roi = validated
        self.save()
        return validated

    def reset(self, app_id: Optional[str] = None) -> None:
        if app_id is not None:
            self._config.games.pop(str(app_id), None)
        else:
            self._config.default_roi = None
        self.save()

    def snapshot(self) -> dict[str, Any]:
        return self._to_dict()


class ActiveROIResolver:
    def __init__(
        self,
        store: ROIConfigStore,
        profile_store: Optional[ROIProfileStore] = None,
        builtin: NormalizedROI = DEFAULT_ROI,
    ) -> None:
        self._store = store
        self._profiles = profile_store or DEFAULT_PROFILE_STORE
        self._builtin = builtin

    def resolve(self, app_id: Optional[str] = None) -> RecognitionROI:
        if app_id is not None:
            override = self._store.get(app_id)
            if override is not None:
                return RecognitionROI(override, SOURCE_USER)

        profile = self._profiles.resolve(app_id)
        if app_id is not None and profile.app_id is not None:
            return RecognitionROI(profile.roi, SOURCE_GAME_PROFILE)

        global_override = self._store.get(None)
        if global_override is not None:
            return RecognitionROI(global_override, SOURCE_USER)

        return RecognitionROI(self._builtin, SOURCE_DEFAULT)


_STORE: Optional[ROIConfigStore] = None
_RESOLVER: Optional[ActiveROIResolver] = None


def configure(path: Path) -> ROIConfigStore:
    global _STORE, _RESOLVER
    _STORE = ROIConfigStore(path)
    _RESOLVER = ActiveROIResolver(_STORE)
    return _STORE


def get_store() -> ROIConfigStore:
    if _STORE is None:
        configure(default_config_path())
    return _STORE  # type: ignore[return-value]


def get_resolver() -> ActiveROIResolver:
    get_store()
    return _RESOLVER  # type: ignore[return-value]


def resolve_active_recognition_roi(
    app_id: Optional[str] = None,
    resolver: Optional[ActiveROIResolver] = None,
) -> NormalizedROI:
    """The helper future OCR must call (never duplicate profile resolution)."""
    return (resolver or get_resolver()).resolve(app_id).roi


def extract_recognition_roi(
    rgba: bytes,
    frame_width: int,
    frame_height: int,
    app_id: Optional[str] = None,
    resolver: Optional[ActiveROIResolver] = None,
) -> ROIFrame:
    """Resolve the active ROI, map to pixels, and crop once (no PNG re-decode)."""
    active = (resolver or get_resolver()).resolve(app_id)
    pixel = resolve_roi(active.roi, frame_width, frame_height)
    width, height, crop = crop_rgba(rgba, frame_width, frame_height, pixel)
    return ROIFrame(width=width, height=height, rgba=crop, roi=pixel, source=active.source)
