#!/usr/bin/env python3
"""Phase 2F.1 device ABI + plugin-local runtime probe.

Activates ``runtime/ocr/site-packages`` (when present) and reports the
interpreter ABI, glibc, native OCR dependency status and source paths, ONNX
execution providers, and model asset status. Config-only: starts no capture,
producer, renderer or OCR worker.

Run:
    PYTHONNOUSERSITE=1 /usr/bin/python3 scripts/ocr_runtime_probe.py \
        --model-dir models/ppocrv6
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ocr import (  # noqa: E402
    BundleError,
    activate_plugin_ocr_runtime,
    probe_report_lines,
    probe_runtime,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck OCR runtime probe")
    parser.add_argument("--model-dir", default=None, help="model directory containing manifest.json")
    parser.add_argument("--plugin-root", default=None, help="override plugin root for the runtime bundle")
    parser.add_argument("--require-bundle", action="store_true", help="fail if the plugin-local bundle is absent")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    plugin_root = Path(args.plugin_root).expanduser() if args.plugin_root else None
    activated = None
    try:
        activated = activate_plugin_ocr_runtime(plugin_root, require=args.require_bundle)
    except BundleError as exc:
        print(f"[ocr-probe] bundle_error={exc.code} detail={exc}")
        return 1

    model_dir = Path(args.model_dir).expanduser() if args.model_dir else None
    info = probe_runtime(model_dir)
    info["runtime_site_packages"] = str(activated) if activated else None
    info["runtime_activated"] = activated is not None

    if args.json:
        print(json.dumps(info, ensure_ascii=False, sort_keys=True))
        return 0 if info.get("compatible") else 1

    for line in probe_report_lines(info):
        print(line)
    return 0 if info.get("compatible") else 1


if __name__ == "__main__":
    raise SystemExit(main())
