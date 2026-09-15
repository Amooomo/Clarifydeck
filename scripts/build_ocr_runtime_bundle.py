#!/usr/bin/env python3
"""Phase 2F.1 / 2F.1a offline OCR runtime bundle builder.

Acquires binary-only CPython 3.13 x86_64 Linux wheels (RapidOCR + ONNX Runtime +
their dependency tree), validating against the full compatible manylinux tag set
(2.17 .. 2.28 + manylinux2014) and stable-ABI tags (cp313/abi3/none), then
optionally installs them into ``runtime/ocr/site-packages`` with pip's
cross-target mode and writes ``runtime/ocr/bundle_manifest.json``.

Nothing here executes Linux native extensions on Windows. Use ``--dry-run`` to
validate an existing wheel set without touching the network.

Run (Linux build host / Docker / WSL):
    python3 scripts/build_ocr_runtime_bundle.py \
        --onnxruntime-version 1.30.0 \
        --platform manylinux_2_28_x86_64 \
        --install --check-models
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ocr.bundle import (  # noqa: E402
    BUNDLE_COMPAT_PINS,
    BUILD_HOST_SDIST_PACKAGES,
    DEFAULT_DEVICE_GLIBC,
    DEFAULT_MINIMUM_MANYLINUX,
    BundleTarget,
    _wheel_name_version,
    build_bundle_manifest,
    is_target_compatible_wheel,
    load_bundle_manifest,
    manylinux_ceiling,
    normalize_dist_name,
    parse_python_version,
    resolver_abis,
    resolver_platforms,
    validate_bundle_compat,
    validate_bundle_manifest,
    wheel_tags,
)
from ocr.runtime import load_manifest, validate_assets  # noqa: E402

ANTLR_DIST = "antlr4-python3-runtime"


def _resolver(args) -> dict:
    python_version = parse_python_version(args.python_version)
    maximum = manylinux_ceiling(args.platform_tag)
    abis = resolver_abis(python_version)
    if args.abi and args.abi not in abis:
        abis = [args.abi, *abis]
    return {
        "python_version": python_version,
        "abis": abis,
        "platforms": resolver_platforms(args.arch, DEFAULT_MINIMUM_MANYLINUX, maximum),
        "arch": args.arch,
        "glibc": DEFAULT_DEVICE_GLIBC,
        "minimum_manylinux": DEFAULT_MINIMUM_MANYLINUX,
        "maximum_manylinux": maximum,
    }


def _print_diagnostics(args, resolver: dict) -> None:
    python_version = resolver["python_version"]
    print(f"[bundle] target_python={python_version[0]}.{python_version[1]}")
    print("[bundle] implementation=cp")
    print(f"[bundle] abis={','.join(resolver['abis'])}")
    print(f"[bundle] platforms={','.join(resolver['platforms'])}")
    print(f"[bundle] target_arch={resolver['arch']}")
    glibc = resolver["glibc"]
    print(f"[bundle] device_glibc={glibc[0]}.{glibc[1]}")
    floor = resolver["minimum_manylinux"]
    ceiling = resolver["maximum_manylinux"]
    print(f"[bundle] minimum_manylinux_glibc={floor[0]}.{floor[1]}")
    print(f"[bundle] maximum_manylinux_glibc={ceiling[0]}.{ceiling[1]}")


def _target(args) -> BundleTarget:
    return BundleTarget(
        python=args.python_version,
        abi=args.abi,
        arch=args.arch,
        platform=args.platform_tag,
    )


def _requirements(args) -> list[str]:
    return [
        f"rapidocr=={args.rapidocr_version}",
        f"onnxruntime=={args.onnxruntime_version}",
        f"omegaconf=={args.omegaconf_version}",
        "numpy>=1.19.5,<3.0.0",
    ]


def _antlr_wheel_path(wheel_dir: Path, version: str):
    candidates = sorted(wheel_dir.glob(f"antlr4_python3_runtime-{version}-*.whl"))
    return candidates[0] if candidates else None


def _validate_antlr_wheel(path: Path, version: str) -> bool:
    if not path.is_file():
        return False
    name, wheel_version = _wheel_name_version(path.name)
    if normalize_dist_name(name) != normalize_dist_name(ANTLR_DIST):
        return False
    if wheel_version != version:
        return False
    try:
        tags = wheel_tags(path.name)
    except Exception:
        return False
    return any(
        python_tag.startswith("py3") and abi_tag == "none" and platform_tag == "any"
        for python_tag, abi_tag, platform_tag in tags
    )


def _prepare_antlr(args, wheel_dir: Path) -> int:
    """Build the antlr4-python3-runtime wheel on the build host (pure Python).

    PyPI only publishes an sdist for 4.9.3; this is the single sanctioned
    build-host-only wheel conversion. No compiler should be involved.
    """
    existing = _antlr_wheel_path(wheel_dir, args.antlr_version)
    if existing is not None:
        if _validate_antlr_wheel(existing, args.antlr_version):
            print(f"[bundle] antlr_wheel_reused={existing.name}")
            return 0
        print(f"[bundle] antlr_wheel_invalid={existing.name} rebuilding")
        existing.unlink(missing_ok=True)

    command = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--wheel-dir",
        str(wheel_dir),
        f"{ANTLR_DIST}=={args.antlr_version}",
    ]
    print(f"[bundle] prepare_antlr: {' '.join(command)}")
    if subprocess.run(command).returncode != 0:
        print("[bundle] antlr_prepare=FAIL")
        return 1
    built = _antlr_wheel_path(wheel_dir, args.antlr_version)
    if built is None or not _validate_antlr_wheel(built, args.antlr_version):
        print("[bundle] antlr_prepare=FAIL no valid wheel produced")
        return 1
    print(f"[bundle] antlr_prepare=PASS {built.name}")
    return 0


def _download_command(args, wheel_dir: Path, resolver: dict) -> list[str]:
    command = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--no-cache-dir"]
    for platform_tag in resolver["platforms"]:
        command += ["--platform", platform_tag]
    command += ["--python-version", args.python_version, "--implementation", "cp"]
    for abi in resolver["abis"]:
        command += ["--abi", abi]
    command += ["--find-links", str(wheel_dir), "--dest", str(wheel_dir), *_requirements(args)]
    return command


def _download(args, wheel_dir: Path, resolver: dict) -> int:
    wheel_dir.mkdir(parents=True, exist_ok=True)
    command = _download_command(args, wheel_dir, resolver)
    print(f"[bundle] download: {' '.join(command)}")
    return 0 if subprocess.run(command).returncode == 0 else 1


def _provenance(wheels, args) -> dict:
    provenance: dict = {}
    for wheel in wheels:
        if wheel.name.lower().startswith("antlr4_python3_runtime-"):
            provenance[normalize_dist_name(ANTLR_DIST)] = {
                "source": "PyPI sdist",
                "source_version": args.antlr_version,
                "built_on": "Linux build host",
            }
    return provenance


def _collect_wheels(wheel_dir: Path, resolver: dict) -> tuple[list[Path], list[str]]:
    wheels = sorted(wheel_dir.glob("*.whl"))
    invalid = [
        wheel.name
        for wheel in wheels
        if not is_target_compatible_wheel(
            wheel.name,
            python_version=resolver["python_version"],
            arch=resolver["arch"],
            glibc=resolver["glibc"],
            minimum_manylinux=resolver["minimum_manylinux"],
            maximum_bundle_floor=resolver["maximum_manylinux"],
        )
    ]
    return wheels, invalid


def _install_command(args, wheel_dir: Path, site_packages: Path, resolver: dict) -> list[str]:
    wheels = sorted(str(wheel) for wheel in wheel_dir.glob("*.whl"))
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(wheel_dir),
        "--only-binary=:all:",
        "--no-deps",
        "--target",
        str(site_packages),
    ]
    for platform_tag in resolver["platforms"]:
        command += ["--platform", platform_tag]
    command += ["--python-version", args.python_version, "--implementation", "cp"]
    for abi in resolver["abis"]:
        command += ["--abi", abi]
    command += wheels
    return command


def _install(args, wheel_dir: Path, site_packages: Path, resolver: dict) -> int:
    site_packages.mkdir(parents=True, exist_ok=True)
    command = _install_command(args, wheel_dir, site_packages, resolver)
    print(f"[bundle] install: {' '.join(command)}")
    return 0 if subprocess.run(command).returncode == 0 else 1


def _report_sizes(wheel_dir: Path, site_packages: Path, models_dir: Path) -> None:
    wheels = sorted(wheel_dir.glob("*.whl"), key=lambda p: p.stat().st_size, reverse=True)
    total = sum(wheel.stat().st_size for wheel in wheels)
    print(f"[bundle] wheel_bytes={total} wheel_count={len(wheels)}")
    for wheel in wheels[:10]:
        print(f"[bundle]   {wheel.stat().st_size:>12} {wheel.name}")
    if site_packages.is_dir():
        site_total = sum(path.stat().st_size for path in site_packages.rglob("*") if path.is_file())
        print(f"[bundle] site_packages_bytes={site_total}")
    model_total = 0
    if models_dir.is_dir():
        model_total = sum(path.stat().st_size for path in models_dir.iterdir() if path.is_file())
    print(f"[bundle] model_bytes={model_total}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the offline ClarifyDeck OCR runtime bundle")
    parser.add_argument("--python-version", default="3.13")
    parser.add_argument("--abi", default="cp313")
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--platform", dest="platform_tag", default="manylinux_2_28_x86_64")
    parser.add_argument("--onnxruntime-version", default="1.30.0")
    parser.add_argument("--rapidocr-version", default="3.9.2")
    parser.add_argument("--omegaconf-version", default=BUNDLE_COMPAT_PINS["omegaconf"])
    parser.add_argument("--antlr-version", default=BUNDLE_COMPAT_PINS["antlr4-python3-runtime"])
    parser.add_argument("--wheel-dir", default=str(ROOT / "runtime" / "ocr" / "wheels"))
    parser.add_argument("--out", default=str(ROOT / "runtime" / "ocr"))
    parser.add_argument("--models-dir", default=str(ROOT / "models" / "ppocrv6"))
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-models", action="store_true")
    parser.add_argument("--print-target-tags", action="store_true")
    parser.add_argument("--prepare-antlr", dest="prepare_antlr", action="store_true", default=True)
    parser.add_argument("--no-prepare-antlr", dest="prepare_antlr", action="store_false")
    parser.add_argument("--clean-site-packages", action="store_true", help="remove site-packages before install")
    args = parser.parse_args(argv)

    resolver = _resolver(args)
    _print_diagnostics(args, resolver)
    if args.print_target_tags:
        print(f"[bundle] target_tags={json.dumps({'abis': resolver['abis'], 'platforms': resolver['platforms']})}")

    target = _target(args)
    wheel_dir = Path(args.wheel_dir)
    out_dir = Path(args.out)
    models_dir = Path(args.models_dir)

    if not args.skip_download and not args.dry_run:
        wheel_dir.mkdir(parents=True, exist_ok=True)
        # Ordering matters: prepare the local ANTLR wheel first, then expose the
        # same wheelhouse to the main resolver via --find-links. Never clear the
        # wheelhouse after this point.
        if args.prepare_antlr and _prepare_antlr(args, wheel_dir) != 0:
            print("[bundle] dependency_bundle=FAIL")
            return 1
        prebuilt = _antlr_wheel_path(wheel_dir, args.antlr_version)
        print(f"[bundle] prebuilt_local_wheel={prebuilt if prebuilt else None}")
        print(f"[bundle] resolver_find_links={wheel_dir}")
        if _download(args, wheel_dir, resolver) != 0:
            print("[bundle] dependency_bundle=FAIL")
            return 1

    wheels, invalid = _collect_wheels(wheel_dir, resolver)
    if not wheels:
        print(f"[bundle] no wheels found in {wheel_dir}")
        print("[bundle] dependency_bundle=FAIL")
        return 1
    if invalid:
        print(f"[bundle] invalid_target_wheels={invalid}")
        print("[bundle] dependency_bundle=FAIL")
        return 1
    print(f"[bundle] dependency_bundle=PASS wheels={len(wheels)}")

    provenance = _provenance(wheels, args)
    manifest = build_bundle_manifest(
        wheels,
        target,
        onnxruntime_version=args.onnxruntime_version,
        rapidocr_version=args.rapidocr_version,
        resolver={
            "resolver_python": args.python_version,
            "resolver_abis": resolver["abis"],
            "resolver_platforms": resolver["platforms"],
            "device_glibc": ".".join(str(part) for part in resolver["glibc"]),
            "bundle_manylinux_floor": ".".join(str(part) for part in resolver["minimum_manylinux"]),
            "bundle_manylinux_ceiling": ".".join(str(part) for part in resolver["maximum_manylinux"]),
        },
        provenance=provenance,
    )
    manifest_path = out_dir / "bundle_manifest.json"
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        print(f"[bundle] wrote {manifest_path} packages={len(manifest['packages'])}")
        written = load_bundle_manifest(manifest_path)
        validate_bundle_manifest(written, target)
        validate_bundle_compat(written)
        print("[bundle] manifest_validated compat_pins_ok")

    if args.install and not args.dry_run:
        site_packages = out_dir / "site-packages"
        if args.clean_site_packages and site_packages.is_dir():
            import shutil

            shutil.rmtree(site_packages)
            print(f"[bundle] cleaned {site_packages}")
        if _install(args, wheel_dir, site_packages, resolver) != 0:
            print("[bundle] install=FAIL")
            return 1
        print("[bundle] install=PASS")

    model_ok = True
    if args.check_models:
        try:
            model_manifest = load_manifest(models_dir)
            validate_assets(model_manifest)
            print(f"[bundle] model_assets=PASS family={model_manifest.family} files={model_manifest.files}")
        except Exception as exc:
            model_ok = False
            print(f"[bundle] model_assets=FAIL error={exc}")

    _report_sizes(wheel_dir, out_dir / "site-packages", models_dir)
    print(f"[bundle] done dependency_bundle=PASS model_assets={'PASS' if model_ok else 'FAIL'}")
    return 0 if model_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
