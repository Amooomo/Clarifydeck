"""Phase 2M.2C: independently persisted Region Profile ("Region Set") store.

Pure stdlib. Manages a directory holding one JSON file per Region Set plus an
``index.json`` that tracks stable ``profile_id`` identities, display labels, and
the active profile. Each profile file reuses the proven v2 RecognitionRegion
schema through :class:`RegionConfigStore` (no schema duplication, no schema
change).

Bootstrap/migration: when the index does not exist, a first profile is created.
If a valid legacy ``recognition_roi.json`` exists its regions (and their stable
``region_id`` values) are preserved into that first profile; otherwise the
built-in fallback region is used. After bootstrap the profile store is
authoritative and the legacy file is never written again (no dual-write).

Import safety: only ``capture.errors`` / ``capture.recognition_regions`` /
``capture.recognition_roi`` / ``capture.roi`` (pure stdlib); no numpy/cv2/
rapidocr/renderer imports.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .errors import CaptureError
from .recognition_regions import (
    RegionConfigStore,
    RegionResolver,
    RecognitionRegion,
    RecognitionRegionSet,
    region_from_roi,
)
from .roi import DEFAULT_ROI

PROFILE_INDEX_VERSION = 1
MAX_REGION_PROFILES = 16
INDEX_FILENAME = "index.json"
PROFILE_FILE_PREFIX = "profile_"
PROFILE_FILE_SUFFIX = ".json"
DEFAULT_LABEL_PREFIX = "Region Set"
MAX_PROFILE_ID_LENGTH = 64
MAX_PROFILE_LABEL_LENGTH = 64
MAX_PROFILE_FILENAME_LENGTH = 128

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LABEL_RE = re.compile(r"^Region Set (\d+)$")


def next_available_region_set_number(existing_numbers) -> int:
    """Smallest unused positive display number for a Region Set label."""
    used = {int(number) for number in existing_numbers}
    number = 1
    while number in used:
        number += 1
    return number


def _safe_filename(name: Any) -> Optional[str]:
    """A safe local basename for a profile file (no traversal, no separators)."""
    if not isinstance(name, str) or not name or len(name) > MAX_PROFILE_FILENAME_LENGTH:
        return None
    if name in (".", "..") or name.startswith("."):
        return None
    if "/" in name or "\\" in name:
        return None
    if Path(name).name != name:
        return None
    if not name.endswith(PROFILE_FILE_SUFFIX):
        return None
    return name


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class RegionProfileStore:
    """Directory-backed store of independent Region Sets (Region Profiles)."""

    def __init__(self, directory: Path, legacy_path: Optional[Path] = None) -> None:
        self._dir = Path(directory)
        self._legacy_path = Path(legacy_path) if legacy_path is not None else None
        self._index: dict[str, Any] = {}
        self._last_error: Optional[str] = None
        self._bootstrap()

    # -- properties --------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def index_path(self) -> Path:
        return self._dir / INDEX_FILENAME

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def active_profile_id(self) -> str:
        return self._index["active_profile_id"]

    def active_profile_path(self) -> Path:
        return self._dir / self._active_profile()["file"]

    # -- bootstrap ---------------------------------------------------------

    def _bootstrap(self) -> None:
        index = self._read_index()
        if index is None:
            self._create_initial_index()
            index = self._read_index()
        if index is None:  # pragma: no cover - write should have succeeded
            raise CaptureError("region_profiles_unavailable", "could not bootstrap profile index")
        self._index = index
        self._ensure_active_valid()

    def _read_index(self) -> Optional[dict[str, Any]]:
        path = self.index_path
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._last_error = f"index_read_failed:{type(exc).__name__}"
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._quarantine_corrupt_index("invalid_json")
            return None
        validated = self._validate_index(data)
        if validated is None:
            self._quarantine_corrupt_index("invalid_schema")
            return None
        if (
            validated["active_profile_id"] != data.get("active_profile_id")
            or validated["next_label_number"] != data.get("next_label_number")
        ):
            self._write_index(validated)  # repair active/label counter in place
        return validated

    def _validate_index(self, data: Any) -> Optional[dict[str, Any]]:
        if not isinstance(data, dict):
            return None
        if data.get("version") != PROFILE_INDEX_VERSION:
            return None
        raw_profiles = data.get("profiles")
        if not isinstance(raw_profiles, list) or not raw_profiles:
            return None
        if len(raw_profiles) > MAX_REGION_PROFILES:
            return None
        profiles: list[dict[str, str]] = []
        ids: set[str] = set()
        files: set[str] = set()
        for entry in raw_profiles:
            if not isinstance(entry, dict):
                return None
            profile_id = entry.get("profile_id")
            label = entry.get("label")
            filename = _safe_filename(entry.get("file"))
            if not isinstance(profile_id, str) or not _ID_RE.match(profile_id):
                return None
            if len(profile_id) > MAX_PROFILE_ID_LENGTH:
                return None
            if not isinstance(label, str) or not label or len(label) > MAX_PROFILE_LABEL_LENGTH:
                return None
            if filename is None:
                return None
            if profile_id in ids or filename in files:
                return None
            ids.add(profile_id)
            files.add(filename)
            profiles.append({"profile_id": profile_id, "label": label, "file": filename})

        active = data.get("active_profile_id")
        if not isinstance(active, str) or active not in ids:
            active = profiles[0]["profile_id"]
        next_label = data.get("next_label_number")
        if not isinstance(next_label, int) or isinstance(next_label, bool) or next_label < 1:
            next_label = len(profiles) + 1
        return {
            "version": PROFILE_INDEX_VERSION,
            "active_profile_id": active,
            "next_label_number": next_label,
            "profiles": profiles,
        }

    def _create_initial_index(self) -> None:
        regions = self._initial_regions()
        profile_id = uuid.uuid4().hex
        filename = f"{PROFILE_FILE_PREFIX}{profile_id}{PROFILE_FILE_SUFFIX}"
        self._write_profile_regions(filename, regions)
        self._write_index(
            {
                "version": PROFILE_INDEX_VERSION,
                "active_profile_id": profile_id,
                "next_label_number": 2,
                "profiles": [
                    {
                        "profile_id": profile_id,
                        "label": f"{DEFAULT_LABEL_PREFIX} 1",
                        "file": filename,
                    }
                ],
            }
        )

    def _initial_regions(self) -> tuple[RecognitionRegion, ...]:
        """Legacy regions if a valid legacy file exists, else built-in fallback."""
        if self._legacy_path is not None and self._legacy_path.is_file():
            store = RegionConfigStore(self._legacy_path)
            if store.last_error is None:
                configured = store.get_global_regions()
                if configured is not None:
                    return tuple(configured)
                resolved = RegionResolver(store).resolve_effective_regions(None).regions
                if resolved:
                    return tuple(resolved)
        return (region_from_roi(DEFAULT_ROI, region_id=uuid.uuid4().hex),)

    def _ensure_active_valid(self) -> None:
        valid_ids = [profile["profile_id"] for profile in self._valid_profiles()]
        if not valid_ids:
            self._append_default_profile()
            return
        if self._index["active_profile_id"] not in valid_ids:
            for profile in self._index["profiles"]:
                if profile["profile_id"] in valid_ids:
                    self._index["active_profile_id"] = profile["profile_id"]
                    break
            self._write_index(self._index)

    def _append_default_profile(self) -> None:
        regions = (region_from_roi(DEFAULT_ROI, region_id=uuid.uuid4().hex),)
        profile_id = uuid.uuid4().hex
        filename = f"{PROFILE_FILE_PREFIX}{profile_id}{PROFILE_FILE_SUFFIX}"
        self._write_profile_regions(filename, regions)
        label = self._next_label()
        self._index["profiles"].append(
            {"profile_id": profile_id, "label": label, "file": filename}
        )
        self._index["next_label_number"] = self._next_label_number()
        self._index["active_profile_id"] = profile_id
        self._write_index(self._index)

    # -- display-number allocation (smallest unused positive integer) -------

    def _display_numbers(self) -> set[int]:
        numbers: set[int] = set()
        for profile in self._index["profiles"]:
            match = _LABEL_RE.match(str(profile.get("label", "")))
            if match:
                numbers.add(int(match.group(1)))
        return numbers

    def _next_label_number(self) -> int:
        return next_available_region_set_number(self._display_numbers())

    def _next_label(self) -> str:
        return f"{DEFAULT_LABEL_PREFIX} {self._next_label_number()}"

    def _quarantine_corrupt_index(self, reason: str) -> None:
        path = self.index_path
        if not path.exists():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self._dir / f"index.corrupt-{stamp}.json"
        try:
            os.replace(path, target)
            _chmod(target, 0o600)
        except OSError:
            pass
        self._last_error = f"corrupt_index:{reason}"

    # -- profile files -----------------------------------------------------

    def _profile_file_ok(self, profile: dict[str, str]) -> bool:
        path = self._dir / profile["file"]
        if not path.is_file():
            return False
        return RegionConfigStore(path).last_error is None

    def _valid_profiles(self) -> list[dict[str, str]]:
        return [profile for profile in self._index["profiles"] if self._profile_file_ok(profile)]

    def _active_profile(self) -> dict[str, str]:
        for profile in self._index["profiles"]:
            if profile["profile_id"] == self._index["active_profile_id"]:
                return profile
        raise CaptureError("profile_not_found", self._index.get("active_profile_id", ""))

    def _write_profile_regions(
        self, filename: str, regions: tuple[RecognitionRegion, ...]
    ) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        _chmod(self._dir, 0o700)
        path = self._dir / filename
        store = RegionConfigStore(path)
        store.set_regions(None, RecognitionRegionSet(tuple(regions)))
        _chmod(path, 0o600)

    # -- persistence helpers ----------------------------------------------

    def _write_index(self, index: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        _chmod(self._dir, 0o700)
        self._atomic_write_json(self.index_path, index)
        self._index = index

    def _atomic_write_json(self, path: Path, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            _chmod(Path(tmp_name), 0o600)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        _chmod(path, 0o600)

    # -- public API --------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "active_profile_id": self._index["active_profile_id"],
            "profiles": [
                {"profile_id": profile["profile_id"], "label": profile["label"]}
                for profile in self._valid_profiles()
            ],
            "max_profiles": MAX_REGION_PROFILES,
            "last_error": self._last_error,
        }

    def select(self, profile_id: str) -> bool:
        if not isinstance(profile_id, str) or not profile_id:
            return False
        target = next(
            (profile for profile in self._index["profiles"] if profile["profile_id"] == profile_id),
            None,
        )
        if target is None or not self._profile_file_ok(target):
            return False
        if self._index["active_profile_id"] != profile_id:
            self._index["active_profile_id"] = profile_id
            self._write_index(self._index)
        return True

    def add(self) -> dict[str, Any]:
        if len(self._index["profiles"]) >= MAX_REGION_PROFILES:
            raise CaptureError("too_many_profiles", f">= {MAX_REGION_PROFILES}")
        regions = (region_from_roi(DEFAULT_ROI, region_id=uuid.uuid4().hex),)
        profile_id = uuid.uuid4().hex
        filename = f"{PROFILE_FILE_PREFIX}{profile_id}{PROFILE_FILE_SUFFIX}"
        # 1) write the new profile file first, 2) then reference it in the index.
        self._write_profile_regions(filename, regions)
        label = self._next_label()
        self._index["profiles"].append(
            {"profile_id": profile_id, "label": label, "file": filename}
        )
        self._index["next_label_number"] = self._next_label_number()
        self._index["active_profile_id"] = profile_id
        self._write_index(self._index)
        return self.payload()

    def delete(self, profile_id: str) -> dict[str, Any]:
        profiles = self._index["profiles"]
        target = next((p for p in profiles if p["profile_id"] == profile_id), None)
        if target is None:
            raise CaptureError("profile_not_found", str(profile_id))
        if len(profiles) <= 1:
            raise CaptureError("cannot_delete_last_profile", str(profile_id))

        remaining = [p for p in profiles if p["profile_id"] != profile_id]
        new_active = self._index["active_profile_id"]
        if new_active == profile_id:
            index = profiles.index(target)
            if index + 1 < len(profiles):
                new_active = profiles[index + 1]["profile_id"]
            else:
                new_active = profiles[index - 1]["profile_id"]
        new_index = {
            "version": PROFILE_INDEX_VERSION,
            "active_profile_id": new_active,
            "next_label_number": self._index.get("next_label_number", len(profiles) + 1),
            "profiles": remaining,
        }
        # 1) commit a valid index without the deleted profile, 2) then unlink.
        self._write_index(new_index)
        try:
            (self._dir / target["file"]).unlink()
        except OSError:
            pass  # orphan file is acceptable; index is authoritative
        self._ensure_active_valid()
        return self.payload()
