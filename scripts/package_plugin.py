#!/usr/bin/env python3
"""Phase 2O.1: deterministic production plugin packager (explicit allowlist).

The developer repository stays complete (source, tests, docs, build tooling).
This script defines the *production artifact* boundary: only runtime-required
files are staged, so development caches, frontend dependencies, tests, docs and
source maps never ship.

Runtime dependency map (evidence: imports + Decky conventions):

    main.py, overlay_manager.py, backend_leader.py   Decky entrypoint + backend
    backend/            transport/worker/delivery/parent-death Python modules
    capture/            capture backends, producer, ROI, regions, profiles
    ocr/                runtime, stabilizer, transport, bundle bootstrap
    overlay/            protocol, presentation, renderer
    scripts/ocr_worker.py, scripts/ocr_test.py   production worker flow
    runtime/ocr/        offline OCR site-packages bundle + manifest
    models/ppocrv6/     PP-OCRv6 det/rec ONNX models + manifest
    lib/, share/, bin/  bundled native/runtime resources (retained as high-risk)
    defaults/, py_modules/   Decky packaging conventions
    dist/               built frontend bundle (source maps excluded)

Usage:
    python3 scripts/package_plugin.py --source . --out out --dry-run
    python3 scripts/package_plugin.py --source . --out out
    python3 scripts/package_plugin.py --source . --out out --zip
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

PLUGIN_NAME = "ClarifyDeck"

# Root-level runtime files (Decky entrypoint + root Python modules).
ALLOWLIST_FILES = (
    "main.py",
    "overlay_manager.py",
    "backend_leader.py",
    "plugin.json",
    "package.json",
    "LICENSE",
    "README.md",
)

# Runtime directories. Anything not listed here (node_modules, .git,
# .pnpm-store, .conda, src, .vscode, docs, tests, build tooling) is excluded.
ALLOWLIST_DIRS = (
    "backend",
    "capture",
    "ocr",
    "overlay",
    "scripts",
    "runtime",
    "models",
    "lib",
    "share",
    "bin",
    "defaults",
    "py_modules",
    "dist",
)

# Production worker flow: only these scripts ship.
SCRIPTS_KEEP = {"ocr_worker.py", "ocr_test.py", "__init__.py"}

# Directory names never staged, wherever they appear under an allowlisted dir.
EXCLUDE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
}

# Build-only directories that happen to live under an allowlisted dir.
EXCLUDE_RELATIVE_DIRS = {
    "backend/src",
    "runtime/ocr/wheels",
}

# Build-only files under allowlisted dirs.
EXCLUDE_RELATIVE_FILES = {
    "backend/Dockerfile",
    "backend/Makefile",
    "backend/entrypoint.sh",
}

# Suffixes that are never runtime-required.
EXCLUDE_FILE_SUFFIXES = (".pyc", ".pyo", ".map")


def _is_excluded_file(rel: str) -> bool:
    if rel in EXCLUDE_RELATIVE_FILES:
        return True
    if rel.endswith(EXCLUDE_FILE_SUFFIXES):
        return True
    return False


def iter_production_files(source_root: Path) -> list[str]:
    """Return the sorted POSIX-relative paths of the production artifact."""
    source_root = Path(source_root)
    selected: list[str] = []

    for name in ALLOWLIST_FILES:
        path = source_root / name
        if path.is_file():
            selected.append(name)

    for dirname in ALLOWLIST_DIRS:
        root = source_root / dirname
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_dir():
                continue
            rel = path.relative_to(source_root).as_posix()
            if any(part in EXCLUDE_DIR_NAMES for part in Path(rel).parts):
                continue
            if any(rel == excluded or rel.startswith(excluded + "/") for excluded in EXCLUDE_RELATIVE_DIRS):
                continue
            if _is_excluded_file(rel):
                continue
            if dirname == "scripts":
                parent = path.parent.relative_to(source_root).as_posix()
                if parent != "scripts" or path.name not in SCRIPTS_KEEP:
                    continue
            selected.append(rel)

    return sorted(set(selected))


def build_manifest(source_root: Path) -> dict:
    source_root = Path(source_root)
    files = iter_production_files(source_root)
    total = 0
    entries = []
    for rel in files:
        try:
            size = (source_root / rel).stat().st_size
        except OSError:
            size = 0
        total += size
        entries.append({"path": rel, "bytes": size})
    return {
        "plugin": PLUGIN_NAME,
        "file_count": len(files),
        "total_bytes": total,
        "files": entries,
    }


def stage(source_root: Path, dest_root: Path) -> dict:
    source_root = Path(source_root)
    dest_root = Path(dest_root)
    files = iter_production_files(source_root)
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    for rel in files:
        src = source_root / rel
        dst = dest_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    manifest = build_manifest(source_root)
    manifest["dest"] = str(dest_root)
    return manifest


def make_zip(stage_root: Path, zip_path: Path) -> dict:
    stage_root = Path(stage_root)
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(stage_root.rglob("*")):
            if not path.is_file():
                continue
            archive.write(path, path.relative_to(stage_root).as_posix())
            count += 1
    return {"zip": str(zip_path), "file_count": count, "bytes": zip_path.stat().st_size}


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ClarifyDeck production plugin packager")
    parser.add_argument("--source", default=".", help="repository root to package")
    parser.add_argument("--out", default="out", help="output root (default: out)")
    parser.add_argument("--dry-run", action="store_true", help="compute the manifest only")
    parser.add_argument("--zip", action="store_true", help="also produce <PluginName>.zip")
    parser.add_argument("--json", action="store_true", help="print the manifest as JSON")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    source_root = Path(args.source).resolve()
    out_root = Path(args.out).resolve()

    if args.dry_run:
        manifest = build_manifest(source_root)
        if args.json:
            sys.stdout.write(json.dumps(manifest, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(
                f"[package] plugin={manifest['plugin']} files={manifest['file_count']} "
                f"bytes={manifest['total_bytes']}\n"
            )
        return 0

    dest_root = out_root / PLUGIN_NAME
    manifest = stage(source_root, dest_root)
    sys.stdout.write(
        f"[package] staged={dest_root} files={manifest['file_count']} "
        f"bytes={manifest['total_bytes']}\n"
    )
    if args.zip:
        result = make_zip(dest_root, out_root / f"{PLUGIN_NAME}.zip")
        sys.stdout.write(
            f"[package] zip={result['zip']} files={result['file_count']} bytes={result['bytes']}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
