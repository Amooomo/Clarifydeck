"""Plugin-local OCR runtime bundle model (Phase 2F.1 / 2F.1a).

Describes the offline wheel bundle under ``runtime/ocr/`` and validates it
against the running interpreter. No native modules are imported or executed
here: bundle validation is pure metadata work.

Wheel compatibility is centralized in ``is_target_compatible_wheel`` so the
resolver policy, the validator and the build manifest cannot drift apart.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:  # build-host-only dependency; never required at Steam Deck runtime
    from packaging.utils import parse_wheel_filename as _parse_wheel_filename
except Exception:  # pragma: no cover - fallback path is unit-tested
    _parse_wheel_filename = None

BUNDLE_FORMAT_VERSION = 1
MAX_BUNDLE_MANIFEST_BYTES = 512 * 1024

REQUIRED_PACKAGES = (
    "numpy",
    "rapidocr",
    "onnxruntime",
    "opencv-python",
    "pyclipper",
    "shapely",
    "Pillow",
    "PyYAML",
    "omegaconf",
)

# Phase 2F.1d: RapidOCR 3.9.2 assigns pathlib.Path values into its OmegaConf
# config; OmegaConf 2.0.0 raises UnsupportedValueType for PosixPath. Pin the
# known-compatible pair instead of patching third-party source.
BUNDLE_COMPAT_PINS = {
    "omegaconf": "2.3.1",
    "antlr4-python3-runtime": "4.9.3",
}
BUNDLE_REJECTED_VERSIONS = {
    "omegaconf": ("2.0.0", "2.2.1"),
}
# PyPI publishes only an sdist for this release; it may be converted to a wheel
# on the Linux build host (pure Python, no compiler).
BUILD_HOST_SDIST_PACKAGES = ("antlr4-python3-runtime",)

DEFAULT_TARGET_PYTHON = (3, 13)
DEFAULT_TARGET_ARCH = "x86_64"
DEFAULT_DEVICE_GLIBC = (2, 41)
DEFAULT_MINIMUM_MANYLINUX = (2, 17)
DEFAULT_MAXIMUM_MANYLINUX = (2, 28)

_FOREIGN_TOKENS = (
    "win_amd64",
    "win32",
    "macosx",
    "darwin",
    "musllinux",
    "aarch64",
    "i686",
    "arm64",
)


class BundleError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class BundleTarget:
    python: str
    abi: str
    arch: str
    platform: str


@dataclass(frozen=True)
class BundlePackage:
    name: str
    version: str
    wheel: str
    sha256: Optional[str] = None
    wheel_sha256: Optional[str] = None
    python_tags: tuple[str, ...] = ()
    abi_tags: tuple[str, ...] = ()
    platform_tags: tuple[str, ...] = ()
    source: Optional[str] = None
    source_version: Optional[str] = None
    built_on: Optional[str] = None


@dataclass(frozen=True)
class BundleManifest:
    format_version: int
    target: BundleTarget
    packages: tuple[BundlePackage, ...]
    onnxruntime_version: Optional[str]
    rapidocr_version: Optional[str]
    path: Path
    resolver: dict = field(default_factory=dict)


# -- target helpers -----------------------------------------------------------


def _normalize_arch(machine: str) -> str:
    machine = (machine or "").lower()
    if machine in ("amd64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    return machine


def _abi_from_soabi(soabi: Optional[str]) -> str:
    if soabi and soabi.startswith("cpython-"):
        parts = soabi.split("-")
        if len(parts) >= 2 and parts[1].isdigit():
            return "cp" + parts[1]
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def running_target() -> BundleTarget:
    soabi = sysconfig.get_config_var("SOABI")
    return BundleTarget(
        python=f"{sys.version_info.major}.{sys.version_info.minor}",
        abi=_abi_from_soabi(soabi),
        arch=_normalize_arch(platform.machine()),
        platform="",
    )


def parse_python_version(value) -> tuple[int, int]:
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return int(value[0]), int(value[1])
    text = str(value).strip()
    match = re.match(r"^(\d+)\.(\d+)$", text)
    if not match:
        raise ValueError(f"invalid python version: {value!r}")
    return int(match.group(1)), int(match.group(2))


def manylinux_ceiling(platform_tag: str) -> tuple[int, int]:
    """Derive the maximum manylinux glibc floor from a primary platform tag."""
    tag = (platform_tag or "").strip().lower()
    if tag.startswith("manylinux2014"):
        return (2, 17)
    match = re.match(r"manylinux_(\d+)_(\d+)_", tag)
    if match:
        return int(match.group(1)), int(match.group(2))
    return DEFAULT_MAXIMUM_MANYLINUX


def resolver_platforms(
    arch: str = DEFAULT_TARGET_ARCH,
    minimum: tuple[int, int] = DEFAULT_MINIMUM_MANYLINUX,
    maximum: tuple[int, int] = DEFAULT_MAXIMUM_MANYLINUX,
) -> list[str]:
    platforms = [f"manylinux_2_{minor}_{arch}" for minor in range(maximum[1], minimum[1] - 1, -1)]
    if minimum[1] <= 17:
        platforms.append(f"manylinux2014_{arch}")
    return platforms


def resolver_abis(python_version: tuple[int, int] = DEFAULT_TARGET_PYTHON) -> list[str]:
    return [f"cp{python_version[0]}{python_version[1]}", "abi3", "none"]


# -- wheel tags ---------------------------------------------------------------


def _manual_tags(filename: str) -> list[tuple[str, str, str]]:
    stem = filename[:-4]
    parts = stem.split("-")
    if len(parts) < 5:
        raise ValueError(f"not a wheel filename: {filename}")
    python_tags = parts[-3].split(".")
    abi_tags = parts[-2].split(".")
    platform_tags = parts[-1].split(".")
    return [(python, abi, plat) for python in python_tags for abi in abi_tags for plat in platform_tags]


def wheel_tags(filename: str) -> list[tuple[str, str, str]]:
    if not filename.lower().endswith(".whl"):
        raise ValueError(f"not a wheel filename: {filename}")
    if _parse_wheel_filename is not None:
        try:
            _name, _version, _build, tags = _parse_wheel_filename(filename)
            parsed = [(tag.interpreter, tag.abi, tag.platform) for tag in tags]
            if parsed:
                return parsed
        except Exception:
            pass
    return _manual_tags(filename)


def _platform_ok(
    platform_tag: str,
    arch: str,
    minimum: tuple[int, int],
    maximum: tuple[int, int],
) -> bool:
    platform_tag = platform_tag.lower()
    if platform_tag == "any":
        return True
    if any(token in platform_tag for token in _FOREIGN_TOKENS):
        return False
    if platform_tag == f"manylinux2014_{arch}":
        return True
    match = re.match(r"manylinux_(\d+)_(\d+)_([a-z0-9_]+)$", platform_tag)
    if not match:
        return False
    if match.group(3) != arch:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    if major != minimum[0]:
        return False
    return minimum[1] <= minor <= maximum[1]


def _python_abi_ok(python_tag: str, abi_tag: str, python_version: tuple[int, int]) -> bool:
    python_tag = python_tag.lower()
    abi_tag = abi_tag.lower()
    if python_tag.startswith("pp") or abi_tag.startswith("pp"):
        return False  # PyPy
    major, minor = python_version

    if python_tag == f"py{major}" or python_tag.startswith(f"py{major}."):
        return abi_tag in ("none", "abi3")
    if python_tag.startswith("cp"):
        digits = python_tag[2:]
        if not digits.isdigit():
            return False
        wheel_major = int(digits[0])
        wheel_minor = int(digits[1:]) if len(digits) > 1 else 0
        if wheel_major != major:
            return False
        if abi_tag == "abi3":
            return wheel_minor <= minor
        return abi_tag == f"cp{wheel_major}{wheel_minor}" and wheel_minor == minor
    return False


def is_target_compatible_wheel(
    filename: str,
    *,
    python_version: tuple[int, int] = DEFAULT_TARGET_PYTHON,
    arch: str = DEFAULT_TARGET_ARCH,
    glibc: tuple[int, int] = DEFAULT_DEVICE_GLIBC,
    minimum_manylinux: tuple[int, int] = DEFAULT_MINIMUM_MANYLINUX,
    maximum_bundle_floor: tuple[int, int] = DEFAULT_MAXIMUM_MANYLINUX,
) -> bool:
    """Centralized wheel compatibility predicate (single source of policy)."""
    lower = filename.lower()
    if not lower.endswith(".whl"):
        return False
    if any(token in lower for token in _FOREIGN_TOKENS):
        return False
    try:
        tags = wheel_tags(filename)
    except ValueError:
        return False
    if not tags:
        return False
    for python_tag, abi_tag, platform_tag in tags:
        if not _platform_ok(platform_tag, arch, minimum_manylinux, maximum_bundle_floor):
            continue
        if not _python_abi_ok(python_tag, abi_tag, python_version):
            continue
        return True
    return False


def wheel_matches_target(filename: str, target: BundleTarget) -> bool:
    try:
        python_version = parse_python_version(target.python)
    except ValueError:
        return False
    return is_target_compatible_wheel(filename, python_version=python_version, arch=target.arch)


def normalize_dist_name(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def wheel_tag_summary(filename: str) -> dict:
    try:
        tags = wheel_tags(filename)
    except ValueError:
        tags = []
    return {
        "python_tags": sorted({tag[0] for tag in tags}),
        "abi_tags": sorted({tag[1] for tag in tags}),
        "platform_tags": sorted({tag[2] for tag in tags}),
    }


# -- manifest -----------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_bundle_manifest(path: Path) -> BundleManifest:
    path = Path(path)
    if not path.is_file():
        raise BundleError("bundle_manifest_missing", str(path))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BundleError("bundle_manifest_unreadable", str(exc)) from exc
    if len(raw) > MAX_BUNDLE_MANIFEST_BYTES:
        raise BundleError("bundle_manifest_too_large", f"{len(raw)} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError("bundle_manifest_invalid", str(exc)) from exc
    if not isinstance(data, dict) or data.get("format_version") != BUNDLE_FORMAT_VERSION:
        raise BundleError("bundle_manifest_invalid", "format_version")
    target_raw = data.get("target")
    if not isinstance(target_raw, dict):
        raise BundleError("bundle_manifest_invalid", "target")
    target = BundleTarget(
        python=str(target_raw.get("python", "")),
        abi=str(target_raw.get("abi", "")),
        arch=str(target_raw.get("arch", "")),
        platform=str(target_raw.get("platform", "")),
    )
    if not target.python or not target.abi or not target.arch:
        raise BundleError("bundle_manifest_invalid", "target fields")
    packages: list[BundlePackage] = []
    for entry in data.get("packages") or []:
        if not isinstance(entry, dict):
            raise BundleError("bundle_manifest_invalid", "package entry")
        name = entry.get("name")
        version = entry.get("version")
        wheel = entry.get("wheel")
        if not all(isinstance(value, str) and value for value in (name, version, wheel)):
            raise BundleError("bundle_manifest_invalid", "package fields")
        packages.append(
            BundlePackage(
                name=name,
                version=version,
                wheel=wheel,
                sha256=entry.get("sha256") if isinstance(entry.get("sha256"), str) else None,
                wheel_sha256=entry.get("wheel_sha256") if isinstance(entry.get("wheel_sha256"), str) else None,
                python_tags=tuple(entry.get("python_tags") or ()),
                abi_tags=tuple(entry.get("abi_tags") or ()),
                platform_tags=tuple(entry.get("platform_tags") or ()),
                source=entry.get("source") if isinstance(entry.get("source"), str) else None,
                source_version=entry.get("source_version") if isinstance(entry.get("source_version"), str) else None,
                built_on=entry.get("built_on") if isinstance(entry.get("built_on"), str) else None,
            )
        )
    if not packages:
        raise BundleError("bundle_manifest_invalid", "packages empty")
    resolver = data.get("resolver") if isinstance(data.get("resolver"), dict) else {}
    return BundleManifest(
        format_version=BUNDLE_FORMAT_VERSION,
        target=target,
        packages=tuple(packages),
        onnxruntime_version=data.get("onnxruntime_version"),
        rapidocr_version=data.get("rapidocr_version"),
        path=path,
        resolver=resolver,
    )


def validate_bundle_manifest(manifest: BundleManifest, target: Optional[BundleTarget] = None) -> None:
    expected = target or running_target()
    actual = manifest.target
    if actual.python != expected.python:
        raise BundleError("bundle_target_mismatch", f"python {actual.python} != {expected.python}")
    if actual.abi != expected.abi:
        raise BundleError("bundle_target_mismatch", f"abi {actual.abi} != {expected.abi}")
    if actual.arch != expected.arch:
        raise BundleError("bundle_target_mismatch", f"arch {actual.arch} != {expected.arch}")


def validate_bundle_compat(manifest: BundleManifest) -> None:
    """Enforce the Phase 2F.1d OmegaConf/ANTLR compatibility pins."""
    versions = {normalize_dist_name(package.name): package.version for package in manifest.packages}
    for name, rejected in BUNDLE_REJECTED_VERSIONS.items():
        actual = versions.get(normalize_dist_name(name))
        if actual in rejected:
            raise BundleError("bundle_package_rejected", f"{name} {actual}")
    for name, pinned in BUNDLE_COMPAT_PINS.items():
        actual = versions.get(normalize_dist_name(name))
        if actual is None:
            raise BundleError("bundle_package_missing", name)
        if actual != pinned:
            raise BundleError("bundle_package_mismatch", f"{name} {actual} != {pinned}")


def build_bundle_manifest(
    wheels,
    target: BundleTarget,
    *,
    onnxruntime_version: Optional[str] = None,
    rapidocr_version: Optional[str] = None,
    resolver: Optional[dict] = None,
    provenance: Optional[dict] = None,
) -> dict:
    """Deterministic manifest dict from validated wheel paths."""
    provenance = provenance or {}
    packages = []
    for wheel in sorted(Path(wheel) for wheel in wheels):
        name, version = _wheel_name_version(wheel.name)
        tags = wheel_tag_summary(wheel.name)
        digest = sha256_file(wheel)
        entry = {
            "name": name,
            "version": version,
            "wheel": wheel.name,
            "sha256": digest,
            "wheel_sha256": digest,
            "python_tags": tags["python_tags"],
            "abi_tags": tags["abi_tags"],
            "platform_tags": tags["platform_tags"],
        }
        extra = provenance.get(normalize_dist_name(name))
        if isinstance(extra, dict):
            entry.update(extra)
        packages.append(entry)
    return {
        "format_version": BUNDLE_FORMAT_VERSION,
        "target": {
            "python": target.python,
            "abi": target.abi,
            "arch": target.arch,
            "platform": target.platform,
        },
        "onnxruntime_version": onnxruntime_version,
        "rapidocr_version": rapidocr_version,
        "compat_pins": dict(BUNDLE_COMPAT_PINS),
        "resolver": resolver or {},
        "packages": packages,
    }


def _wheel_name_version(filename: str) -> tuple[str, str]:
    parts = filename[:-4].split("-")
    if len(parts) < 2:
        return filename, ""
    return parts[0], parts[1]
