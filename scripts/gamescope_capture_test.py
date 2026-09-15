#!/usr/bin/env python3
"""Phase 2A manual test: one-shot gamescope base-plane capture.

Imports the production capture module; does not duplicate the protocol code and
does not touch overlay state.

Usage:
    python3 scripts/gamescope_capture_test.py --probe
    python3 scripts/gamescope_capture_test.py --mode base_plane_only \
        --output /tmp/clarifydeck-base-plane.png
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture import gamescope_capture  # noqa: E402


def probe_ok(payload: dict) -> bool:
    """Probe passes only when the protocol is usable for base-plane capture."""
    version = payload.get("version")
    return bool(
        payload.get("available") is True
        and isinstance(version, int)
        and version >= gamescope_capture.MIN_SCREENSHOT_VERSION
        and payload.get("screenshot_supported") is True
        and payload.get("base_plane_only_supported") is True
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck gamescope capture test")
    parser.add_argument(
        "--mode",
        default="base_plane_only",
        choices=sorted(gamescope_capture.SCREENSHOT_TYPES),
    )
    parser.add_argument("--output", default=None)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument(
        "--frame",
        action="store_true",
        help="capture one frame into memory (CaptureFrame) and print metadata only",
    )
    parser.add_argument(
        "--debug-copy",
        default=None,
        help="write a durable copy from the in-memory frame bytes",
    )
    args = parser.parse_args(argv)

    capture = gamescope_capture.GamescopeCapture(logger=lambda m: print(m, file=sys.stderr))
    debug_copy = Path(args.debug_copy) if args.debug_copy else None
    ok = False
    try:
        if args.probe:
            result = capture.probe()
            ok = probe_ok(result)
            payload = {"ok": ok, **result}
        elif args.frame:
            frame = capture.capture_frame(
                mode=args.mode, timeout=args.timeout, debug_copy=debug_copy
            )
            ok = True
            payload = {
                "ok": True,
                "backend": frame.source_backend,
                "mode": frame.source_mode,
                "sequence": frame.sequence,
                "width": frame.width,
                "height": frame.height,
                "format": frame.format,
                "bytes": len(frame.encoded_bytes),
                "source_path": frame.source_path,
                "debug_copy": str(debug_copy) if debug_copy else None,
            }
        else:
            payload = capture.capture(
                output=Path(args.output) if args.output else None,
                mode=args.mode,
                timeout=args.timeout,
                debug_copy=debug_copy,
            )
            ok = bool(payload.get("ok"))
    except gamescope_capture.CaptureError as exc:
        payload = {"ok": False, "error": exc.code, "mode": args.mode}
        ok = False

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
