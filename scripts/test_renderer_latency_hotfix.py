#!/usr/bin/env python3
"""Phase 2N.3.1 regression test: renderer latency diagnostics must not crash.

Loads ``overlay/renderer.py`` with a stubbed X11/cairo library so the module can
be imported without a display, then executes ``OverlayRenderer.log_region_latency``
for real. This fails with ``NameError: name 'time' is not defined`` on the 2N.3
checkpoint and passes once the missing ``import time`` is restored.

Run:
    python3 scripts/test_renderer_latency_hotfix.py
"""

from __future__ import annotations

import contextlib
import ctypes.util
import importlib.util
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_renderer():
    """Import overlay/renderer.py with load_lib forced to a real system library."""
    original = ctypes.util.find_library
    fake = "kernel32" if sys.platform == "win32" else "c"
    ctypes.util.find_library = lambda name: fake  # type: ignore[assignment]
    try:
        spec = importlib.util.spec_from_file_location(
            "clarifydeck_renderer_hotfix_test", ROOT / "overlay" / "renderer.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    finally:
        ctypes.util.find_library = original  # type: ignore[assignment]


class RendererLatencyHotfixTest(unittest.TestCase):
    def test_log_region_latency_executes_without_nameerror(self) -> None:
        module = _load_renderer()
        renderer = module.OverlayRenderer(None, 100, 100, 24, 0.55, False)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            renderer.log_region_latency("A", 5, 1000.0, 900.0)
        output = buffer.getvalue()
        self.assertIn("[latency]", output)
        self.assertIn("renderer_ms=", output)
        self.assertIn("frame_age_at_render_ms=", output)

    def test_log_region_latency_captured_only(self) -> None:
        module = _load_renderer()
        renderer = module.OverlayRenderer(None, 100, 100, 24, 0.55, False)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            renderer.log_region_latency("B", 8, None, 900.0)
        output = buffer.getvalue()
        self.assertIn("frame_age_at_render_ms=", output)
        self.assertNotIn("renderer_ms=", output)

    def test_log_region_latency_noop_without_timestamps(self) -> None:
        module = _load_renderer()
        renderer = module.OverlayRenderer(None, 100, 100, 24, 0.55, False)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            renderer.log_region_latency("A", None, None, None)
        self.assertEqual(buffer.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
