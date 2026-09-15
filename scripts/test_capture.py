#!/usr/bin/env python3
"""Phase 2A capture isolation tests.

The Linux-only Wayland client is mocked so these tests run anywhere. They cover
metadata validation, empty frames, unsupported protocol, base_plane_only
unavailability (no silent full_composition fallback), timeouts, write failures
and the no-capture-at-boot invariant.

Run:
    python3 scripts/test_capture.py
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib.util
import io
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture import gamescope_capture  # noqa: E402
from capture.gamescope_capture import CaptureError, GamescopeCapture  # noqa: E402


def _png(width: int, height: int) -> bytes:
    raw = bytearray()
    for _ in range(height):
        raw.append(0)
        raw += bytes([0, 0, 0]) * width

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


class FakeClient:
    def __init__(self, version: int = 6, fail: str = "", write: bool = True) -> None:
        self.control_version = version
        self.features = []
        self.screenshot_path = None
        self._fail = fail
        self._write = write
        self.calls = 0
        self.closed = False

    def connect(self) -> None:
        if self._fail == "connect":
            raise CaptureError("gamescope_socket_not_found")

    def take_screenshot(self, path: str, type_id: int, timeout: float) -> str:
        self.calls += 1
        if self._fail == "timeout":
            raise CaptureError("capture_timeout")
        if self._write:
            Path(path).write_bytes(_png(1280, 800))
        return path

    def close(self) -> None:
        self.closed = True


class CaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_client = GamescopeCapture._client

    def tearDown(self) -> None:
        GamescopeCapture._client = self._orig_client
        self._tmp.cleanup()

    def _install(self, client: FakeClient) -> None:
        GamescopeCapture._client = lambda self: client  # type: ignore[assignment]

    def test_valid_frame_metadata(self) -> None:
        self._install(FakeClient(version=6))
        capture = GamescopeCapture(display="gamescope-0")
        result = capture.capture_base_plane(output=Path(self._tmp.name) / "a.png")
        self.assertTrue(result["ok"])
        self.assertEqual(result["width"], 1280)
        self.assertEqual(result["height"], 800)
        self.assertEqual(result["mode"], "base_plane_only")

    def test_explicit_output_persists(self) -> None:
        self._install(FakeClient(version=6))
        out = Path(self._tmp.name) / "explicit.png"
        result = GamescopeCapture().capture_base_plane(output=out)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 0)
        self.assertFalse(result["output_owned"])
        self.assertEqual(Path(result["output"]), out)

    def test_internal_output_persists_and_is_owned(self) -> None:
        self._install(FakeClient(version=6))
        result = GamescopeCapture().capture_base_plane()
        out = Path(result["output"])
        self.assertTrue(out.exists())
        self.assertTrue(result["output_owned"])

    def test_cli_persists_explicit_output(self) -> None:
        self._install(FakeClient(version=6))
        path = ROOT / "scripts" / "gamescope_capture_test.py"
        spec = importlib.util.spec_from_file_location("gamescope_capture_test_cli", path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)  # type: ignore[union-attr]
        out = Path(self._tmp.name) / "case-a.png"
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["--mode", "base_plane_only", "--output", str(out)])
        self.assertEqual(code, 0)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 0)

    def test_empty_frame_is_invalid(self) -> None:
        client = FakeClient(version=6, write=False)
        self._install(client)
        capture = GamescopeCapture()
        with self.assertRaises(CaptureError) as ctx:
            capture.capture_base_plane(output=Path(self._tmp.name) / "empty.png")
        self.assertEqual(ctx.exception.code, "invalid_frame")

    def test_unsupported_protocol_fails_closed(self) -> None:
        client = FakeClient(version=2)
        self._install(client)
        capture = GamescopeCapture()
        with self.assertRaises(CaptureError) as ctx:
            capture.capture_base_plane(output=Path(self._tmp.name) / "v2.png")
        self.assertEqual(ctx.exception.code, "gamescope_protocol_incompatible")
        self.assertEqual(client.calls, 0)  # never sent a request
        self.assertTrue(client.closed)

    def test_base_plane_only_unavailable_does_not_fallback(self) -> None:
        client = FakeClient(version=2)
        self._install(client)
        capture = GamescopeCapture()
        with self.assertRaises(CaptureError) as ctx:
            capture.capture(output=Path(self._tmp.name) / "b.png", mode="base_plane_only")
        self.assertEqual(ctx.exception.code, "gamescope_protocol_incompatible")
        self.assertEqual(client.calls, 0)

    def test_timeout_fails_closed(self) -> None:
        client = FakeClient(version=6, fail="timeout")
        self._install(client)
        capture = GamescopeCapture()
        with self.assertRaises(CaptureError) as ctx:
            capture.capture_base_plane(output=Path(self._tmp.name) / "t.png", timeout=0.2)
        self.assertEqual(ctx.exception.code, "capture_timeout")
        self.assertEqual(client.calls, 1)  # single attempt, no retry loop
        self.assertTrue(client.closed)

    def test_write_failure_is_structured(self) -> None:
        client = FakeClient(version=6, write=False)
        self._install(client)
        capture = GamescopeCapture()
        with self.assertRaises(CaptureError) as ctx:
            capture.capture_base_plane(output=Path(self._tmp.name) / "w.png")
        self.assertEqual(ctx.exception.code, "invalid_frame")
        self.assertTrue(client.closed)

    def test_probe_reports_support(self) -> None:
        self._install(FakeClient(version=6))
        capture = GamescopeCapture()
        result = capture.probe()
        self.assertTrue(result["available"])
        self.assertTrue(result["base_plane_only_supported"])

    def test_probe_failure_is_structured(self) -> None:
        self._install(FakeClient(fail="connect"))
        capture = GamescopeCapture()
        result = capture.probe()
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "gamescope_socket_not_found")


class ProbeVerdictTest(unittest.TestCase):
    """The CLI must report PASS/exit 0 only for a usable probe result."""

    @classmethod
    def setUpClass(cls) -> None:
        path = ROOT / "scripts" / "gamescope_capture_test.py"
        spec = importlib.util.spec_from_file_location("gamescope_capture_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        cls.cli = module

    def _payload(self, **overrides) -> dict:
        base = {
            "available": True,
            "version": 6,
            "screenshot_supported": True,
            "base_plane_only_supported": True,
        }
        base.update(overrides)
        return base

    def test_probe_ok_success(self) -> None:
        self.assertTrue(self.cli.probe_ok(self._payload()))

    def test_probe_ok_available_false(self) -> None:
        self.assertFalse(self.cli.probe_ok(self._payload(available=False)))

    def test_probe_ok_screenshot_false(self) -> None:
        self.assertFalse(self.cli.probe_ok(self._payload(screenshot_supported=False)))

    def test_probe_ok_base_plane_false(self) -> None:
        self.assertFalse(self.cli.probe_ok(self._payload(base_plane_only_supported=False)))

    def test_probe_ok_version_too_low(self) -> None:
        self.assertFalse(self.cli.probe_ok(self._payload(version=2)))

    def test_cli_probe_exit_zero_on_success(self) -> None:
        from capture import gamescope_capture as gc

        payload = self._payload()
        original = gc.GamescopeCapture.probe
        gc.GamescopeCapture.probe = lambda self, timeout=3.0: payload  # type: ignore[assignment]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = self.cli.main(["--probe"])
        finally:
            gc.GamescopeCapture.probe = original
        self.assertEqual(code, 0)

    def test_cli_probe_exit_nonzero_on_failure(self) -> None:
        from capture import gamescope_capture as gc

        payload = {"available": False, "error": "gamescope_control_not_found"}
        original = gc.GamescopeCapture.probe
        gc.GamescopeCapture.probe = lambda self, timeout=3.0: payload  # type: ignore[assignment]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = self.cli.main(["--probe"])
        finally:
            gc.GamescopeCapture.probe = original
        self.assertNotEqual(code, 0)


class GeneratedHelperRegressionTest(unittest.TestCase):
    """Regression: never resolve generated static-inline Wayland helpers."""

    def test_source_does_not_use_generated_helpers(self) -> None:
        src = (ROOT / "capture" / "gamescope_capture.py").read_text(encoding="utf-8")
        for bad in ("wl_display_get_registry", "wl_registry_add_listener", "wl_registry_bind"):
            self.assertNotIn(bad, src)
        for good in (
            "wl_proxy_marshal_array_constructor",
            "wl_proxy_marshal_array_constructor_versioned",
            "wl_proxy_add_listener",
        ):
            self.assertIn(good, src)

    def test_wl_interface_layout(self) -> None:
        from capture.gamescope_capture import _WlInterface

        self.assertEqual(_WlInterface.name.offset, 0)
        self.assertEqual(_WlInterface.version.offset, 8)
        self.assertEqual(_WlInterface.method_count.offset, 12)
        self.assertEqual(_WlInterface.methods.offset, 16)
        self.assertEqual(_WlInterface.event_count.offset, 24)
        self.assertEqual(_WlInterface.events.offset, 32)

    def test_wl_argument_size(self) -> None:
        from capture.gamescope_capture import _WlArgument

        self.assertEqual(ctypes.sizeof(_WlArgument), 8)


class CallbackBindingTest(unittest.TestCase):
    """Regression: ctypes Structure callback fields must be CFUNCTYPE instances."""

    def _new_client(self):
        from capture.gamescope_capture import _WaylandClient

        client = object.__new__(_WaylandClient)
        client._callback_error = None
        client._init_callback_wrappers()
        return client

    def test_callback_wrappers_are_cfunctiontype(self) -> None:
        from capture import gamescope_capture as gc

        client = self._new_client()
        self.assertIsInstance(client._registry_global_cb, gc._RegistryGlobalFn)
        self.assertIsInstance(client._registry_remove_cb, gc._RegistryRemoveFn)
        self.assertIsInstance(client._feature_support_cb, gc._FeatureSupportFn)
        self.assertIsInstance(client._active_display_cb, gc._ActiveDisplayFn)
        self.assertIsInstance(client._screenshot_taken_cb, gc._ScreenshotTakenFn)
        self.assertIsInstance(client._app_perf_cb, gc._AppPerfFn)
        self.assertIsInstance(client._registry_listener, gc._RegistryListener)
        self.assertIsInstance(client._control_listener, gc._ControlListener)
        self.assertIsInstance(client._registry_listener.global_, gc._RegistryGlobalFn)
        self.assertIsInstance(client._registry_listener.global_remove, gc._RegistryRemoveFn)
        self.assertIsInstance(client._control_listener.screenshot_taken, gc._ScreenshotTakenFn)

    def test_callback_lifetime_survives_gc(self) -> None:
        import gc

        client = self._new_client()
        registry_ref = client._registry_global_cb
        screenshot_ref = client._screenshot_taken_cb
        gc.collect()
        self.assertIs(client._registry_global_cb, registry_ref)
        self.assertIs(client._screenshot_taken_cb, screenshot_ref)
        self.assertTrue(callable(client._registry_global_cb))
        self.assertTrue(callable(client._screenshot_taken_cb))


class NoBootCaptureTest(unittest.TestCase):
    def test_main_does_not_capture_at_boot(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("async def _main(self)")
        end = source.index("async def _unload(self)")
        boot_body = source[start:end]
        self.assertNotIn("capture", boot_body.lower())
        self.assertNotIn("GamescopeCapture", boot_body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
