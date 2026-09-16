"""Phase 2L.1: multi-region recognition configuration foundation.

Pure stdlib. Defines the canonical multi-region domain model, a versioned
persistence store (evolving the existing ``recognition_roi.json`` file to v2),
and an effective-region resolver with legacy single-ROI compatibility.

This module does NOT execute multi-region OCR and does NOT change the existing
``roi_config_*`` RPC contract. The current OCR worker still crops exactly one
transitional "primary" region. Region-tagged transport, multiple stabilizers,
and multi-block overlay rendering are later gates.

Import safety: only ``capture.errors`` / ``capture.roi`` (pure stdlib) and
``capture.recognition_roi`` are used; no numpy/cv2/rapidocr/renderer imports.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

from .errors import CaptureError
from .recognition_roi import (
    CONFIG_VERSION as LEGACY_CONFIG_VERSION,
    MAX_APP_ID_LENGTH,
    MIN_ROI_SIZE,
    ROIConfigStore,
    ActiveROIResolver,
    parse_roi,
    roi_to_dict,
)
from .roi import DEFAULT_ROI, NormalizedROI

REGION_CONFIG_VERSION = 2
MAX_REGIONS = 8
MAX_CONFIG_BYTES = 64 * 1024
MAX_REGION_NAME_LENGTH = 64
LEGACY_REGION_ID = "legacy-primary"

# Built-in fallback single region (deterministic id, never written on read).
BUILTIN_REGION_ID = "builtin-default"


@dataclass(frozen=True)
class RecognitionRegion:
    region_id: str
    x: float
    y: float
    w: float
    h: float
    enabled: bool = True
    name: Optional[str] = None


@dataclass(frozen=True)
class RecognitionRegionSet:
    """Ordered, unique, bounded collection of regions (display order only)."""

    regions: tuple[RecognitionRegion, ...] = ()

    def __post_init__(self) -> None:
        if len(self.regions) > MAX_REGIONS:
            raise CaptureError("too_many_regions", f"{len(self.regions)} > {MAX_REGIONS}")
        seen: set[str] = set()
        for region in self.regions:
            validate_region(region)
            if region.region_id in seen:
                raise CaptureError("duplicate_region_id", region.region_id)
            seen.add(region.region_id)

    def enabled_regions(self) -> tuple[RecognitionRegion, ...]:
        return tuple(region for region in self.regions if region.enabled)

    def primary(self) -> Optional[RecognitionRegion]:
        """First enabled region (transitional single-ROI compatibility only)."""
        for region in self.regions:
            if region.enabled:
                return region
        return None


def validate_region(region: RecognitionRegion) -> RecognitionRegion:
    """Strict normalized geometry validation (rejects, never clamps)."""
    if not isinstance(region, RecognitionRegion):
        raise CaptureError("invalid_region", "region must be a RecognitionRegion")
    if not isinstance(region.region_id, str) or not region.region_id:
        raise CaptureError("invalid_region", "region_id must be a non-empty string")
    if region.name is not None:
        if not isinstance(region.name, str) or len(region.name) > MAX_REGION_NAME_LENGTH:
            raise CaptureError("invalid_region", "name out of range")
    for value in (region.x, region.y, region.w, region.h):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise CaptureError("invalid_region", "geometry must be finite numbers")
    if not (0.0 <= region.x < 1.0) or not (0.0 <= region.y < 1.0):
        raise CaptureError("invalid_region", "x/y must be in [0, 1)")
    if not (0.0 < region.w <= 1.0) or not (0.0 < region.h <= 1.0):
        raise CaptureError("invalid_region", "w/h must be in (0, 1]")
    if region.w < MIN_ROI_SIZE or region.h < MIN_ROI_SIZE:
        raise CaptureError("invalid_region", f"w/h must be >= {MIN_ROI_SIZE}")
    if region.x + region.w > 1.0 or region.y + region.h > 1.0:
        raise CaptureError("invalid_region", "region must stay inside the frame")
    return region


def region_from_roi(roi: NormalizedROI, region_id: str = LEGACY_REGION_ID, name: Optional[str] = None) -> RecognitionRegion:
    region = RecognitionRegion(region_id=region_id, x=roi.x, y=roi.y, w=roi.width, h=roi.height, enabled=True, name=name)
    return validate_region(region)


def region_to_dict(region: RecognitionRegion) -> dict[str, Any]:
    return {
        "region_id": region.region_id,
        "x": region.x,
        "y": region.y,
        "w": region.w,
        "h": region.h,
        "enabled": region.enabled,
        "name": region.name,
    }


def _parse_region(value: Any) -> Optional[RecognitionRegion]:
    if not isinstance(value, Mapping):
        return None
    try:
        region = RecognitionRegion(
            region_id=value["region_id"],
            x=float(value["x"]),
            y=float(value["y"]),
            w=float(value["w"]),
            h=float(value["h"]),
            enabled=bool(value.get("enabled", True)),
            name=value.get("name"),
        )
    except (KeyError, TypeError, ValueError):
        return None
    try:
        return validate_region(region)
    except CaptureError:
        return None


def _parse_region_list(value: Any) -> Optional[tuple[RecognitionRegion, ...]]:
    """Parse a v2 regions list; reject the whole list if anything is malformed.

    Never partially trust persisted geometry: an invalid entry makes the layer
    unconfigured (None) so resolution falls back to a trusted lower layer. An
    explicit empty list is valid and means "zero regions".
    """
    if not isinstance(value, list):
        return None
    regions = []
    for entry in value:
        region = _parse_region(entry)
        if region is None:
            return None
        regions.append(region)
    try:
        return RecognitionRegionSet(tuple(regions)).regions
    except CaptureError:
        return None


@dataclass
class RegionConfig:
    version: int = REGION_CONFIG_VERSION
    global_regions: Optional[tuple[RecognitionRegion, ...]] = None
    per_game: dict[str, tuple[RecognitionRegion, ...]] = field(default_factory=dict)
    legacy_global: Optional[NormalizedROI] = None
    legacy_games: dict[str, NormalizedROI] = field(default_factory=dict)
    legacy: bool = False


class RegionConfigStore:
    """Versioned multi-region config persisted to the shared ROI config file.

    Reads v2 region config directly. If the file is a legacy v1 single-ROI
    config, the legacy view is retained for fallback resolution (read-only).
    Writes are atomic (temp file + ``os.replace``), matching the existing ROI
    store contract.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._config = RegionConfig()
        self._last_error: Optional[str] = None
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def legacy(self) -> bool:
        return self._config.legacy

    # -- load/save ---------------------------------------------------------

    def load(self) -> None:
        self._config = RegionConfig()
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
        if version == LEGACY_CONFIG_VERSION:
            self._load_legacy_v1(data)
            return
        if version != REGION_CONFIG_VERSION:
            self._last_error = f"unsupported_version:{version}"
            return
        self._load_v2(data)

    def _load_v2(self, data: Mapping[str, Any]) -> None:
        global_regions: Optional[tuple[RecognitionRegion, ...]] = None
        global_block = data.get("global")
        if isinstance(global_block, Mapping) and "regions" in global_block:
            global_regions = _parse_region_list(global_block.get("regions"))
            if global_regions is None:
                self._last_error = "invalid_global_regions"
                global_regions = None

        per_game: dict[str, tuple[RecognitionRegion, ...]] = {}
        raw_per_game = data.get("per_game")
        if isinstance(raw_per_game, Mapping):
            for app_id, entry in raw_per_game.items():
                if not isinstance(app_id, str) or not app_id or len(app_id) > MAX_APP_ID_LENGTH:
                    continue
                if not isinstance(entry, Mapping) or "regions" not in entry:
                    continue
                parsed = _parse_region_list(entry.get("regions"))
                if parsed is None:
                    continue
                per_game[app_id] = parsed
        self._config = RegionConfig(
            version=REGION_CONFIG_VERSION,
            global_regions=global_regions,
            per_game=per_game,
        )

    def _load_legacy_v1(self, data: Mapping[str, Any]) -> None:
        legacy_global = self._safe_legacy_roi(data.get("default_roi"))
        legacy_games: dict[str, NormalizedROI] = {}
        raw_games = data.get("games")
        if isinstance(raw_games, Mapping):
            for app_id, entry in raw_games.items():
                if not isinstance(app_id, str) or not app_id or len(app_id) > MAX_APP_ID_LENGTH:
                    continue
                candidate = entry.get("roi") if isinstance(entry, Mapping) else entry
                roi = self._safe_legacy_roi(candidate)
                if roi is not None:
                    legacy_games[app_id] = roi
        self._config = RegionConfig(
            version=LEGACY_CONFIG_VERSION,
            legacy_global=legacy_global,
            legacy_games=legacy_games,
            legacy=True,
        )

    @staticmethod
    def _safe_legacy_roi(value: Any) -> Optional[NormalizedROI]:
        if value is None:
            return None
        try:
            return parse_roi(value)
        except CaptureError:
            return None

    def _to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"version": REGION_CONFIG_VERSION}
        payload["global"] = (
            {"regions": [region_to_dict(region) for region in self._config.global_regions]}
            if self._config.global_regions is not None
            else None
        )
        payload["per_game"] = {
            app_id: {"regions": [region_to_dict(region) for region in regions]}
            for app_id, regions in sorted(self._config.per_game.items())
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

    def get_regions(self, app_id: Optional[str] = None) -> Optional[tuple[RecognitionRegion, ...]]:
        """Configured v2 regions for the app_id (None if not configured)."""
        if app_id is None:
            return self._config.global_regions
        return self._config.per_game.get(str(app_id))

    def get_global_regions(self) -> Optional[tuple[RecognitionRegion, ...]]:
        return self._config.global_regions

    def get_legacy_roi(self, app_id: Optional[str] = None) -> Optional[NormalizedROI]:
        if app_id is not None:
            override = self._config.legacy_games.get(str(app_id))
            if override is not None:
                return override
        return self._config.legacy_global

    def set_regions(self, app_id: Optional[str], regions: RecognitionRegionSet) -> tuple[RecognitionRegion, ...]:
        validated = RecognitionRegionSet(tuple(regions.regions)).regions
        if app_id is not None:
            key = str(app_id)
            if not key or len(key) > MAX_APP_ID_LENGTH:
                raise CaptureError("invalid_app_id", "app_id out of range")
            self._config.per_game[key] = validated
        else:
            self._config.global_regions = validated
        self.save()
        return validated

    def reset_regions(self, app_id: Optional[str] = None) -> None:
        if app_id is not None:
            self._config.per_game.pop(str(app_id), None)
        else:
            self._config.global_regions = None
        self.save()

    def snapshot(self) -> dict[str, Any]:
        return self._to_dict()


class RegionResolver:
    """Effective-region resolution with legacy single-ROI compatibility.

    Precedence: v2 per-game regions > v2 global regions > legacy single ROI
    (delegated to the existing authoritative ``ActiveROIResolver`` when
    supplied) > built-in default. The legacy layer therefore preserves the
    existing per-game > preset > global > built-in precedence.
    """

    def __init__(self, store: RegionConfigStore, legacy_resolver: Optional[ActiveROIResolver] = None) -> None:
        self._store = store
        self._legacy = legacy_resolver

    def resolve_effective_regions(self, app_id: Optional[str] = None) -> RecognitionRegionSet:
        if app_id is not None:
            per_game = self._store.get_regions(app_id)
            if per_game is not None:
                return RecognitionRegionSet(per_game)
        global_regions = self._store.get_global_regions()
        if global_regions is not None:
            return RecognitionRegionSet(global_regions)
        return RecognitionRegionSet((self._legacy_region(app_id),))

    def _legacy_region(self, app_id: Optional[str]) -> RecognitionRegion:
        if self._legacy is not None:
            return region_from_roi(self._legacy.resolve(app_id).roi, region_id=LEGACY_REGION_ID)
        roi = self._store.get_legacy_roi(app_id)
        if roi is None:
            roi = DEFAULT_ROI
            region_id = BUILTIN_REGION_ID
        else:
            region_id = LEGACY_REGION_ID
        return region_from_roi(roi, region_id=region_id)

    def primary_region(self, app_id: Optional[str] = None) -> Optional[RecognitionRegion]:
        """Transitional single-ROI compatibility: first enabled effective region."""
        return self.resolve_effective_regions(app_id).primary()

    def adopt_effective_regions(self, app_id: Optional[str] = None) -> RecognitionRegionSet:
        """Migrate the current effective (possibly legacy) set to a v2 config.

        Geometry is preserved exactly; fresh stable region IDs are assigned and
        persisted so subsequent reloads keep the same identity.
        """
        current = self.resolve_effective_regions(app_id)
        adopted = tuple(replace(region, region_id=uuid.uuid4().hex) for region in current.regions)
        self._store.set_regions(app_id, RecognitionRegionSet(adopted))
        return RecognitionRegionSet(adopted)


def default_regions_path() -> Path:
    """Same path the existing ROI config uses (single source of truth)."""
    from .recognition_roi import default_config_path

    return default_config_path()
