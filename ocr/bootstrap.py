"""Plugin-local OCR runtime bootstrap (Phase 2F.1).

Prepends the offline ``runtime/ocr/site-packages`` bundle to ``sys.path`` for
OCR diagnostic processes only. It never runs pip, never downloads, never mutates
global site-packages or PATH, and is never imported by ``main.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from .bundle import BundleError, load_bundle_manifest, validate_bundle_compat, validate_bundle_manifest

RUNTIME_SUBDIR = Path("runtime") / "ocr"
SITE_PACKAGES_SUBDIR = RUNTIME_SUBDIR / "site-packages"
BUNDLE_MANIFEST_NAME = "bundle_manifest.json"

_ACTIVATED: Optional[Path] = None


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def runtime_root(plugin_root_override: Optional[Path] = None) -> Path:
    root = Path(plugin_root_override) if plugin_root_override else plugin_root()
    return root / RUNTIME_SUBDIR


def runtime_site_packages(plugin_root_override: Optional[Path] = None) -> Path:
    return runtime_root(plugin_root_override) / "site-packages"


def bundle_manifest_path(plugin_root_override: Optional[Path] = None) -> Path:
    return runtime_root(plugin_root_override) / BUNDLE_MANIFEST_NAME


def activate_plugin_ocr_runtime(
    plugin_root_override: Optional[Path] = None,
    require: bool = False,
) -> Optional[Path]:
    """Insert the plugin-local site-packages into ``sys.path`` once.

    Returns the activated path, or ``None`` when the bundle is absent and
    ``require`` is False. Raises ``BundleError`` when ``require`` is True and the
    bundle or its manifest is missing/invalid/target-mismatched.
    """
    global _ACTIVATED
    site_packages = runtime_site_packages(plugin_root_override)
    if not site_packages.is_dir():
        if require:
            raise BundleError("runtime_bundle_missing", str(site_packages))
        return None

    manifest_path = bundle_manifest_path(plugin_root_override)
    if manifest_path.is_file():
        manifest = load_bundle_manifest(manifest_path)
        validate_bundle_manifest(manifest)
        validate_bundle_compat(manifest)
    elif require:
        raise BundleError("bundle_manifest_missing", str(manifest_path))

    if _ACTIVATED != site_packages:
        sys.path.insert(0, str(site_packages))
        _ACTIVATED = site_packages
    return site_packages
