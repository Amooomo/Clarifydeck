#!/usr/bin/env python3
"""Phase 2N.6 tests: Gamescope PipeWire capture backend prototype.

Deterministic, no real GStreamer/PipeWire: the Gst adapter is faked and NV12
conversion is exercised on tiny synthetic buffers. Verifies backend selection,
lazy import safety, lifecycle/retry/health, one-frame-per-tick pull semantics,
NV12 conversion correctness, and screenshot isolation.

Run:
    python3 scripts/test_pipewire_capture.py
"""

from __future__ import annotations

import builtins
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import capture.backend as backend_mod  # noqa: E402
import capture.pipewire_capture as pw  # noqa: E402
from capture.backend import (  # noqa: E402
    CAPTURE_BACKEND_ENV,
    PipeWireCaptureBackend,
    ScreenshotCaptureBackend,
    normalize_backend_name,
    resolve_capture_backend,
)
from capture.errors import CaptureError  # noqa: E402
from capture.frame import CaptureFrame, DecodedFrame  # noqa: E402
from capture.gamescope_capture import GamescopeCapture  # noqa: E402
from capture.recognition_regions import RecognitionRegion  # noqa: E402
from ocr.multi_region import MultiRegionOCRCoordinator  # noqa: E402


def _rgba(width, height, value=128):
    return bytes([value, value, value, 255]) * (width * height)


def _frame(width=8, height=4, value=64):
    return pw.PipeWireFrame(
        width=width,
        height=height,
        rgba=_rgba(width, height, value),
        format="NV12",
        source_width=width,
        source_height=height,
        captured_monotonic=123.0,
        map_ms=1.5,
        conversion_ms=2.5,
    )


class FakeAdapter:
    def __init__(self, *, fail_attempts=0, frame=None, error=None):
        self.fail_attempts = fail_attempts
        self.attempts = 0
        self.started = False
        self.stop_calls = 0
        self.frame = frame if frame is not None else _frame()
        self.error = error
        self.pull_calls = 0
        self.pull_timeouts = []
        self.start_info = {
            "startup_ms": 12.5,
            "source_width": self.frame.source_width,
            "source_height": self.frame.source_height,
            "source_format": self.frame.format,
        }

    def start_attempt(self):
        self.attempts += 1
        if self.attempts <= self.fail_attempts:
            raise CaptureError("pipewire_start_failed", "target not found")
        self.started = True
        return dict(self.start_info)

    def stop(self):
        self.stop_calls += 1
        self.started = False

    def poll_error(self):
        return self.error

    def try_pull_sample(self, timeout):
        self.pull_calls += 1
        self.pull_timeouts.append(timeout)
        return self.frame

    def status(self):
        return {"backend": "pipewire", "state": "PLAYING" if self.started else "STOPPED"}


class BackendSelectionTest(unittest.TestCase):
    def test_default_is_screenshot(self) -> None:
        self.assertEqual(normalize_backend_name(None), "screenshot")
        self.assertIsInstance(resolve_capture_backend(None, env={}), ScreenshotCaptureBackend)

    def test_env_selects_pipewire(self) -> None:
        env = {CAPTURE_BACKEND_ENV: "pipewire"}
        self.assertIsInstance(resolve_capture_backend(None, env=env), PipeWireCaptureBackend)

    def test_explicit_overrides_env(self) -> None:
        env = {CAPTURE_BACKEND_ENV: "pipewire"}
        self.assertIsInstance(resolve_capture_backend("screenshot", env=env), ScreenshotCaptureBackend)

    def test_aliases(self) -> None:
        self.assertEqual(normalize_backend_name("gamescope"), "screenshot")
        self.assertEqual(normalize_backend_name("gst"), "pipewire")

    def test_invalid_selector_rejected(self) -> None:
        with self.assertRaises(CaptureError) as ctx:
            normalize_backend_name("nonsense")
        self.assertEqual(ctx.exception.code, "invalid_capture_backend")


class LazyImportSafetyTest(unittest.TestCase):
    def test_module_has_no_top_level_gi_import(self) -> None:
        source = (ROOT / "capture" / "pipewire_capture.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.lstrip()
            if stripped == line and (line.startswith("import gi") or line.startswith("from gi")):
                self.fail(f"top-level gi import: {line!r}")
        self.assertIn("def load_gst", source)

    def test_missing_gi_is_clean_capability_failure(self) -> None:
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "gi" or name.startswith("gi."):
                raise ImportError("no gi on this host")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            with self.assertRaises(CaptureError) as ctx:
                pw.load_gst()
            self.assertEqual(ctx.exception.code, "pipewire_unavailable")
        finally:
            builtins.__import__ = real_import

    def test_construct_adapter_does_not_import_gst(self) -> None:
        adapter = pw.create_gst_adapter()
        self.assertIsInstance(adapter, pw.GstPipeWireAdapter)


class PipeWireLifecycleTest(unittest.TestCase):
    def _backend(self, adapter, **kwargs):
        return PipeWireCaptureBackend(adapter=adapter, retry_interval=0.0, **kwargs)

    def test_start_lifecycle(self) -> None:
        adapter = FakeAdapter()
        backend = self._backend(adapter)
        status = backend.start()
        self.assertEqual(status["state"], "PLAYING")
        self.assertEqual(adapter.attempts, 1)
        self.assertTrue(adapter.started)
        self.assertEqual(status["source_format"], "NV12")

    def test_stop_lifecycle(self) -> None:
        adapter = FakeAdapter()
        backend = self._backend(adapter)
        backend.start()
        backend.stop()
        self.assertGreaterEqual(adapter.stop_calls, 1)
        self.assertEqual(backend.status()["state"], "STOPPED")

    def test_bounded_retry_then_success(self) -> None:
        adapter = FakeAdapter(fail_attempts=3)
        backend = self._backend(adapter, retry_window=5.0)
        status = backend.start()
        self.assertEqual(status["state"], "PLAYING")
        self.assertEqual(adapter.attempts, 4)

    def test_bounded_failure_no_infinite_loop(self) -> None:
        adapter = FakeAdapter(fail_attempts=999)
        backend = self._backend(adapter, retry_window=0.0)
        with self.assertRaises(CaptureError) as ctx:
            backend.start()
        self.assertEqual(ctx.exception.code, "pipewire_unavailable")
        self.assertEqual(adapter.attempts, 1)

    def test_retry_cancellation_on_stop(self) -> None:
        adapter = FakeAdapter(fail_attempts=999)
        backend = PipeWireCaptureBackend(adapter=adapter, retry_window=100.0, retry_interval=0.0)
        backend._sleep = lambda _seconds: backend.stop()
        with self.assertRaises(CaptureError) as ctx:
            backend.start()
        self.assertEqual(ctx.exception.code, "pipewire_start_cancelled")

    def test_bus_error_fails_capture(self) -> None:
        adapter = FakeAdapter()
        backend = self._backend(adapter)
        backend.start()
        adapter.error = "bus_error:target not found"
        with self.assertRaises(CaptureError) as ctx:
            backend.capture_frame(timeout=0.1)
        self.assertEqual(ctx.exception.code, "pipewire_stream_error")
        self.assertEqual(backend.status()["state"], "FAILED")

    def test_eos_fails_capture(self) -> None:
        adapter = FakeAdapter()
        backend = self._backend(adapter)
        backend.start()
        adapter.error = "eos"
        with self.assertRaises(CaptureError) as ctx:
            backend.capture_frame(timeout=0.1)
        self.assertEqual(ctx.exception.code, "pipewire_stream_error")

    def test_no_frame_before_start(self) -> None:
        backend = PipeWireCaptureBackend(adapter=FakeAdapter())
        with self.assertRaises(CaptureError) as ctx:
            backend.capture_frame(timeout=0.1)
        self.assertEqual(ctx.exception.code, "pipewire_not_started")


class PullSemanticsTest(unittest.TestCase):
    def test_one_pull_per_capture_tick(self) -> None:
        adapter = FakeAdapter()
        backend = PipeWireCaptureBackend(adapter=adapter)
        backend.start()
        for _ in range(5):
            backend.capture_frame(timeout=0.25)
        self.assertEqual(adapter.pull_calls, 5)
        self.assertEqual(backend.status()["frames_pulled"], 5)
        self.assertEqual(adapter.pull_timeouts, [0.25] * 5)

    def test_no_unbounded_python_queue(self) -> None:
        adapter = FakeAdapter()
        backend = PipeWireCaptureBackend(adapter=adapter)
        backend.start()
        for _ in range(20):
            backend.capture_frame(timeout=0.1)
        # No internal frame history/queue grows with pulls.
        self.assertEqual(backend.status()["frames_pulled"], 20)
        self.assertFalse(hasattr(backend, "_queue"))
        self.assertFalse(hasattr(backend, "_frames"))

    def test_dynamic_dimensions_and_timestamp(self) -> None:
        frame = _frame(width=640, height=360)
        adapter = FakeAdapter(frame=frame)
        backend = PipeWireCaptureBackend(adapter=adapter)
        backend.start()
        decoded = backend.capture_frame(timeout=0.1)
        self.assertEqual((decoded.width, decoded.height), (640, 360))
        self.assertEqual(len(decoded.rgba), 640 * 360 * 4)
        self.assertIsInstance(decoded.captured_monotonic, float)
        self.assertGreater(decoded.captured_monotonic, 0.0)
        self.assertEqual(decoded.source_format, "NV12")
        self.assertEqual(decoded.source_backend, "pipewire")
        self.assertEqual(decoded.conversion_ms, 2.5)


class ScreenshotIsolationTest(unittest.TestCase):
    def test_screenshot_function_not_invoked_under_pipewire(self) -> None:
        calls = {"count": 0}
        original = GamescopeCapture.capture_frame

        def forbidden(self, *args, **kwargs):
            calls["count"] += 1
            raise AssertionError("screenshot path must not run under PipeWire")

        GamescopeCapture.capture_frame = forbidden
        try:
            adapter = FakeAdapter()
            backend = PipeWireCaptureBackend(adapter=adapter)
            backend.start()
            decoded = backend.capture_frame(timeout=0.1)
            self.assertEqual(decoded.source_backend, "pipewire")
        finally:
            GamescopeCapture.capture_frame = original
        self.assertEqual(calls["count"], 0)

    def test_screenshot_backend_unchanged(self) -> None:
        class FakeCapture:
            def __init__(self):
                self.calls = []

            def capture_frame(self, mode="base_plane_only", timeout=5.0):
                self.calls.append((mode, timeout))
                return "screenshot-frame"

        fake = FakeCapture()
        backend = ScreenshotCaptureBackend(capture=fake)
        backend.start()
        self.assertEqual(backend.capture_frame("base_plane_only", 1.5), "screenshot-frame")
        self.assertEqual(fake.calls, [("base_plane_only", 1.5)])
        backend.stop()
        self.assertEqual(backend.name, "screenshot")


class FakeStructure:
    def __init__(self, values):
        self._values = values

    def get_value(self, key):
        return self._values[key]


class FakeCaps:
    def __init__(self, values):
        self._values = values

    def get_structure(self, _index):
        return FakeStructure(self._values)


class FakeBuffer:
    def __init__(self, data, pts=0):
        self._data = data
        self.pts = pts

    def map(self, _flags):
        return SimpleNamespace(data=self._data)

    def unmap(self, _info):
        return None

    def get_video_meta(self):
        return None


class FakeSample:
    def __init__(self, caps, buffer):
        self._caps = caps
        self._buffer = buffer

    def get_caps(self):
        return self._caps

    def get_buffer(self):
        return self._buffer


class _AdapterHarness(pw.GstPipeWireAdapter):
    def __init__(self):
        super().__init__()
        self._Gst = SimpleNamespace(
            MapFlags=SimpleNamespace(READ=1),
            CLOCK_TIME_NONE=(1 << 64) - 1,
        )


class _FakeBus:
    def __init__(self, messages=None):
        self._messages = list(messages or [])

    def pop_filtered(self, _mask):
        return self._messages.pop(0) if self._messages else None


class _FakeAppsink:
    def __init__(self, sample):
        self._sample = sample
        self.pulls = 0

    def try_pull_sample(self, timeout_ns):
        self.pulls += 1
        return self._sample


class _NoPullAppsink:
    """Mimics a PyGObject appsink when the GstApp override is not loaded."""


class _FakePipeline:
    def __init__(self, appsink, bus):
        self._appsink = appsink
        self._bus = bus
        self.states = []

    def get_by_name(self, _name):
        return self._appsink

    def get_bus(self):
        return self._bus

    def set_state(self, state):
        self.states.append(state)


class _FakeGst:
    class State:
        PLAYING = "PLAYING"
        NULL = "NULL"

    class MessageType:
        ERROR = 1
        EOS = 2

    class MapFlags:
        READ = 1

    CLOCK_TIME_NONE = (1 << 64) - 1

    def __init__(self, pipeline):
        self._pipeline = pipeline
        self._initialized = True
        self.descriptions = []

    def is_initialized(self):
        return self._initialized

    def init(self, _argv):
        self._initialized = True

    def parse_launch(self, description):
        self.descriptions.append(description)
        return self._pipeline


class GstAppNamespaceTest(unittest.TestCase):
    def _install_fake_gi(self, gst, gstapp=None):
        fake_gi = types.ModuleType("gi")
        calls: list = []

        def require_version(name, version):
            calls.append((name, version))

        fake_gi.require_version = require_version
        fake_repo = types.ModuleType("gi.repository")
        fake_repo.Gst = gst
        fake_repo.GstVideo = SimpleNamespace()
        fake_repo.GstApp = gstapp if gstapp is not None else SimpleNamespace()
        fake_gi.repository = fake_repo

        previous = {key: sys.modules.get(key) for key in ("gi", "gi.repository")}
        sys.modules["gi"] = fake_gi
        sys.modules["gi.repository"] = fake_repo

        def cleanup():
            for key, value in previous.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value

        self.addCleanup(cleanup)
        return calls

    def test_load_gst_requires_and_returns_gstapp(self) -> None:
        gst = _FakeGst(_FakePipeline(_NoPullAppsink(), _FakeBus()))
        gstapp = SimpleNamespace()
        calls = self._install_fake_gi(gst, gstapp=gstapp)
        result = pw.load_gst()
        self.assertEqual(len(result), 3)
        self.assertIs(result[0], gst)
        self.assertIs(result[2], gstapp)
        self.assertIn(("Gst", "1.0"), calls)
        self.assertIn(("GstVideo", "1.0"), calls)
        self.assertIn(("GstApp", "1.0"), calls)

    def test_start_attempt_uses_appsink_pull_and_stores_gstapp(self) -> None:
        caps = FakeCaps({"width": 2, "height": 2, "format": "NV12"})
        buffer = FakeBuffer(bytes([16, 16, 16, 16, 128, 128]))
        sample = FakeSample(caps, buffer)
        appsink = _FakeAppsink(sample)
        pipeline = _FakePipeline(appsink, _FakeBus())
        gst = _FakeGst(pipeline)
        gstapp = SimpleNamespace()
        self._install_fake_gi(gst, gstapp=gstapp)
        adapter = pw.GstPipeWireAdapter()
        info = adapter.start_attempt()
        self.assertEqual(info["state"], "PLAYING")
        self.assertIs(adapter._GstApp, gstapp)
        self.assertEqual(appsink.pulls, 1)
        self.assertEqual(info["source_width"], 2)
        self.assertEqual(info["source_height"], 2)
        self.assertIn("target-object=gamescope", gst.descriptions[0])

    def test_appsink_without_pull_method_fails_cleanly(self) -> None:
        pipeline = _FakePipeline(_NoPullAppsink(), _FakeBus())
        gst = _FakeGst(pipeline)
        self._install_fake_gi(gst)
        adapter = pw.GstPipeWireAdapter()
        with self.assertRaises(CaptureError) as ctx:
            adapter.start_attempt()
        self.assertEqual(ctx.exception.code, "pipewire_appsink_unavailable")

    def test_backend_logs_start_failure_diagnostic(self) -> None:
        logs: list = []
        adapter = FakeAdapter(fail_attempts=999)
        backend = PipeWireCaptureBackend(adapter=adapter, retry_window=0.0, logger=logs.append)
        with self.assertRaises(CaptureError):
            backend.start()
        self.assertTrue(any("[capture-pipewire]" in line and "start" in line for line in logs))
        self.assertTrue(any("pipewire_start_failed" in line for line in logs))


class Nv12ConversionTest(unittest.TestCase):
    def test_tightly_packed_black_and_white(self) -> None:
        black = bytes([16, 16, 16, 16, 128, 128])
        rgba = pw.nv12_to_rgba(black, 2, 2)
        self.assertEqual(len(rgba), 2 * 2 * 4)
        self.assertEqual(list(rgba[:4]), [0, 0, 0, 255])
        white = bytes([235, 235, 235, 235, 128, 128])
        rgba = pw.nv12_to_rgba(white, 2, 2)
        self.assertEqual(list(rgba[:4]), [255, 255, 255, 255])

    def test_non_default_dimensions(self) -> None:
        width, height = 4, 2
        y = bytes([16] * (width * height))
        uv = bytes([128, 128] * (width // 2))
        rgba = pw.nv12_to_rgba(y + uv, width, height)
        self.assertEqual(len(rgba), width * height * 4)
        self.assertEqual(rgba, bytes([0, 0, 0, 255]) * (width * height))

    def test_stride_and_offset_handling(self) -> None:
        width, height = 2, 2
        y_stride, uv_stride, uv_offset = 4, 4, 8
        data = bytearray(uv_offset + uv_stride * (height // 2))
        for row in range(height):
            data[row * y_stride] = 16
            data[row * y_stride + 1] = 16
        data[uv_offset] = 128
        data[uv_offset + 1] = 128
        rgba = pw.nv12_to_rgba(
            bytes(data), width, height, y_stride=y_stride, uv_stride=uv_stride, uv_offset=uv_offset
        )
        self.assertEqual(rgba, bytes([0, 0, 0, 255]) * (width * height))

    def test_bad_buffer_size_rejected(self) -> None:
        with self.assertRaises(CaptureError) as ctx:
            pw.nv12_to_rgba(bytes([0, 0]), 2, 2)
        self.assertEqual(ctx.exception.code, "invalid_frame")

    def test_odd_height_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            pw.nv12_to_rgba(bytes(32), 4, 3)

    def test_stride_smaller_than_width_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            pw.nv12_to_rgba(bytes(32), 4, 2, y_stride=2)

    def test_all_backends_agree(self) -> None:
        data = bytes([100, 120, 140, 160, 90, 90, 200, 200])
        expected = pw._nv12_to_rgba_python(data, 2, 2, 2, 2, 4)
        self.assertEqual(pw.nv12_to_rgba(data, 2, 2), expected)


class SampleConversionTest(unittest.TestCase):
    def test_nv12_sample_to_frame(self) -> None:
        adapter = _AdapterHarness()
        caps = FakeCaps({"width": 2, "height": 2, "format": "NV12"})
        buffer = FakeBuffer(bytes([16, 16, 16, 16, 128, 128]))
        frame = adapter._sample_to_frame(FakeSample(caps, buffer))
        self.assertEqual((frame.width, frame.height), (2, 2))
        self.assertEqual(frame.format, "NV12")
        self.assertEqual(frame.rgba, bytes([0, 0, 0, 255]) * 4)
        self.assertIsInstance(frame.map_ms, float)
        self.assertIsInstance(frame.conversion_ms, float)

    def test_unsupported_format_rejected(self) -> None:
        adapter = _AdapterHarness()
        caps = FakeCaps({"width": 2, "height": 2, "format": "BGRx"})
        buffer = FakeBuffer(bytes(16))
        with self.assertRaises(CaptureError) as ctx:
            adapter._sample_to_frame(FakeSample(caps, buffer))
        self.assertEqual(ctx.exception.code, "pipewire_unsupported_format")

    def test_missing_caps_rejected(self) -> None:
        adapter = _AdapterHarness()
        buffer = FakeBuffer(bytes(16))
        with self.assertRaises(CaptureError) as ctx:
            adapter._sample_to_frame(FakeSample(None, buffer))
        self.assertEqual(ctx.exception.code, "pipewire_missing_caps")


class _RegionRuntime:
    def __init__(self):
        self.calls = []

    def recognize_rgba(self, rgba, width, height, sequence=None):
        self.calls.append((width, height, sequence))
        return SimpleNamespace(lines=(), sequence=sequence, elapsed_ms=1.0)


class MultiRegionOneFrameTest(unittest.TestCase):
    def test_one_decoded_frame_feeds_all_regions(self) -> None:
        runtime = _RegionRuntime()
        coordinator = MultiRegionOCRCoordinator(runtime)
        regions = [
            RecognitionRegion(region_id="A", x=0.0, y=0.0, w=0.5, h=0.5),
            RecognitionRegion(region_id="B", x=0.5, y=0.5, w=0.5, h=0.5),
        ]
        frame = DecodedFrame(
            width=64,
            height=32,
            format="rgba",
            rgba=_rgba(64, 32),
            captured_monotonic=100.0,
            captured_wall_time=100.0,
            source_backend="pipewire",
            source_mode="gamescope_pipewire",
            sequence=1,
            decode_ms=2.0,
            conversion_ms=2.0,
        )
        coordinator.process_frame(frame, regions)
        # Exactly one crop/OCR per region from the same decoded frame.
        self.assertEqual(len(runtime.calls), 2)
        self.assertEqual(coordinator.stats.frames_processed, 1)
        self.assertEqual(coordinator.stats.crops, 2)


class WorkerCommandForwardingTest(unittest.TestCase):
    def test_capture_backend_flag_forwarded(self) -> None:
        from backend.ocr_worker import OCRWorkerManager

        manager = OCRWorkerManager()
        command = manager.build_command(
            sys.executable,
            fps=1.0,
            capture_backend="pipewire",
        )
        self.assertEqual(command.count("--capture-backend"), 1)
        self.assertEqual(command[command.index("--capture-backend") + 1], "pipewire")

    def test_capture_backend_default_omitted(self) -> None:
        from backend.ocr_worker import OCRWorkerManager

        manager = OCRWorkerManager()
        command = manager.build_command(sys.executable, fps=1.0)
        self.assertNotIn("--capture-backend", command)

    def test_env_backend_resolution(self) -> None:
        from backend.ocr_worker import OCRWorkerManager

        self.assertEqual(OCRWorkerManager._resolve_capture_backend("pipewire"), "pipewire")
        self.assertEqual(OCRWorkerManager._resolve_capture_backend("screenshot"), "screenshot")
        self.assertIsNone(OCRWorkerManager._resolve_capture_backend("bogus"))


class ManagerJournalMirrorTest(unittest.TestCase):
    def test_capture_backend_lines_are_mirrored(self) -> None:
        from backend.ocr_worker import OCRWorkerManager

        class _Stream:
            def __init__(self, lines):
                self._lines = list(lines)

            def readline(self, limit=-1):
                return self._lines.pop(0) if self._lines else b""

            def close(self):
                pass

        captured: list = []
        manager = OCRWorkerManager(logger=captured.append)
        manager._proc = SimpleNamespace(
            stderr=_Stream(
                [
                    b"[capture-pipewire] backend=pipewire target=gamescope state=PLAYING\n",
                    b"[capture-backend] error backend=pipewire code=pipewire_unavailable detail=x\n",
                    b"unrelated line\n",
                ]
            )
        )
        manager._read_stderr()
        self.assertTrue(any("[capture-pipewire]" in line for line in captured))
        self.assertTrue(any("[capture-backend]" in line for line in captured))
        self.assertFalse(any("unrelated line" in line for line in captured))


class ImportSafetyTest(unittest.TestCase):
    def test_capture_modules_do_not_load_gi(self) -> None:
        import importlib

        for name in (
            "capture.frame",
            "capture.change_detector",
            "capture.pipewire_capture",
            "capture.backend",
        ):
            importlib.import_module(name)
        self.assertNotIn("gi", sys.modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
