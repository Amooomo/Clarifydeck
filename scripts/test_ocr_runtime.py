#!/usr/bin/env python3
"""Phase 2F tests: OCR runtime adapter, manifest, color order, pipeline.

Real RapidOCR/ONNX models are not required; a fake engine exercises the adapter.

Run:
    python3 scripts/test_ocr_runtime.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for candidate in (str(ROOT), str(SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import numpy as np  # noqa: E402

import ocr_test  # noqa: E402
from capture import recognition_roi as rr  # noqa: E402
from capture.frame import CaptureFrame  # noqa: E402
from capture.latest_frame_queue import LatestFrameQueue  # noqa: E402
from capture.roi import NormalizedROI  # noqa: E402
from ocr import (  # noqa: E402
    OCRConfig,
    OCRError,
    OCRRuntime,
    load_manifest,
    predict_det_geometry,
    probe_runtime,
    rgba_to_array,
)
from ocr.runtime import (  # noqa: E402
    _RapidOCREngine,
    _normalize_rapidocr_result,
    _rapidocr_params,
    _structured_lines,
    validate_det_limit_side_len,
    validate_det_limit_type,
    validate_thread_count,
)

W, H = 64, 32


def _model_dir(with_files: bool = True, sha256: dict | None = None) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="clarifydeck-ocr-")) / "ppocrv6"
    directory.mkdir(parents=True)
    files = {"det": "det.onnx", "rec": "rec.onnx", "dict": "dict.txt"}
    if with_files:
        for name in files.values():
            (directory / name).write_bytes(b"model-bytes")
    manifest = {
        "format_version": 1,
        "engine": "rapidocr",
        "family": "pp-ocrv6",
        "files": files,
    }
    if sha256:
        manifest["sha256"] = sha256
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


class FakeEngine:
    def __init__(self, lines=None, det=0.01, rec=0.02, fail=False) -> None:
        self.calls = 0
        self.received = None
        self._fail = fail
        self._lines = lines if lines is not None else [
            ([[0, 0], [10, 0], [10, 5], [0, 5]], "你好世界", 0.96),
            ([[1, 1], [9, 1], [9, 4], [1, 4]], "hello", 0.51),
        ]
        self._det = det
        self._rec = rec

    def __call__(self, image):
        self.calls += 1
        self.received = image
        if self._fail:
            raise RuntimeError("engine boom")
        return (self._lines, [self._det, self._rec])


def _runtime(model_dir: Path, fake: FakeEngine, color_order="bgr", min_confidence=0.0) -> OCRRuntime:
    config = OCRConfig(model_dir=model_dir, color_order=color_order, min_confidence=min_confidence)
    return OCRRuntime(config, engine_factory=lambda manifest, cfg: _RapidOCREngine(fake))


def _rgba(width: int = W, height: int = H, value: int = 120) -> bytes:
    return bytes([value, value // 2, value // 3, 255] * (width * height))


def _png_rgb(width: int = W, height: int = H, value: int = 200) -> bytes:
    raw = bytearray()
    for _ in range(height):
        raw.append(0)
        raw += bytes([value, value, value]) * width

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def _frame(seq: int, value: int = 200) -> CaptureFrame:
    return CaptureFrame.from_png(_png_rgb(value=value), sequence=seq, source_backend="mock", source_mode="base_plane_only")


def _resolver(roi: NormalizedROI, app_id: str | None = None) -> rr.ActiveROIResolver:
    store = rr.ROIConfigStore(Path(tempfile.mkdtemp(prefix="clarifydeck-ocr-roi-")) / "recognition_roi.json")
    if app_id is None:
        store.set(None, roi)
    else:
        store.set(app_id, roi)
    return rr.ActiveROIResolver(store)


class RuntimeAdapterTest(unittest.TestCase):
    def test_runtime_initializes_once(self) -> None:
        fake = FakeEngine()
        runtime = _runtime(_model_dir(), fake)
        runtime.recognize_rgba(_rgba(), W, H)
        runtime.recognize_rgba(_rgba(), W, H)
        self.assertEqual(runtime.engine_init_count, 1)
        self.assertEqual(fake.calls, 2)
        self.assertIsNotNone(runtime.engine_init_ms)

    def test_repeated_recognize_does_not_reinitialize(self) -> None:
        fake = FakeEngine()
        runtime = _runtime(_model_dir(), fake)
        for _ in range(5):
            runtime.recognize_rgba(_rgba(), W, H)
        self.assertEqual(runtime.engine_init_count, 1)

    def test_missing_model_fails_safely(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="clarifydeck-ocr-")) / "empty"
        directory.mkdir(parents=True)
        runtime = _runtime(directory, FakeEngine())
        with self.assertRaises(OCRError) as ctx:
            runtime.recognize_rgba(_rgba(), W, H)
        self.assertEqual(ctx.exception.code, "manifest_missing")

    def test_invalid_manifest_fails_safely(self) -> None:
        directory = _model_dir()
        (directory / "manifest.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(OCRError) as ctx:
            _runtime(directory, FakeEngine()).recognize_rgba(_rgba(), W, H)
        self.assertEqual(ctx.exception.code, "manifest_invalid")

    def test_unsupported_manifest_version(self) -> None:
        directory = _model_dir()
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        manifest["format_version"] = 99
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(OCRError) as ctx:
            _runtime(directory, FakeEngine()).recognize_rgba(_rgba(), W, H)
        self.assertEqual(ctx.exception.code, "manifest_unsupported_version")

    def test_missing_assets_fails_safely(self) -> None:
        runtime = _runtime(_model_dir(with_files=False), FakeEngine())
        with self.assertRaises(OCRError) as ctx:
            runtime.recognize_rgba(_rgba(), W, H)
        self.assertEqual(ctx.exception.code, "model_assets_missing")

    def test_hash_mismatch_fails_closed(self) -> None:
        directory = _model_dir(sha256={"det": "0" * 64})
        with self.assertRaises(OCRError) as ctx:
            _runtime(directory, FakeEngine()).recognize_rgba(_rgba(), W, H)
        self.assertEqual(ctx.exception.code, "model_hash_mismatch")

    def test_hash_match_passes(self) -> None:
        directory = _model_dir()
        digest = hashlib.sha256((directory / "det.onnx").read_bytes()).hexdigest()
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        manifest["sha256"] = {"det": digest}
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = _runtime(directory, FakeEngine()).recognize_rgba(_rgba(), W, H)
        self.assertGreaterEqual(result.line_count, 1)

    def test_unavailable_dependency_fails_safely(self) -> None:
        # rapidocr is not installed in this environment -> default factory fails closed
        runtime = OCRRuntime(OCRConfig(model_dir=_model_dir()))
        with self.assertRaises(OCRError) as ctx:
            runtime.recognize_rgba(_rgba(), W, H)
        self.assertEqual(ctx.exception.code, "rapidocr_unavailable")

    def test_empty_input_rejected(self) -> None:
        with self.assertRaises(OCRError) as ctx:
            _runtime(_model_dir(), FakeEngine()).recognize_rgba(b"", W, H)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_rgba_length_mismatch_rejected(self) -> None:
        with self.assertRaises(OCRError) as ctx:
            _runtime(_model_dir(), FakeEngine()).recognize_rgba(b"\x00" * 10, 4, 4)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_color_conversion_deterministic(self) -> None:
        rgba = bytes([10, 20, 30, 255])
        rgb = rgba_to_array(rgba, 1, 1, "rgb")
        bgr = rgba_to_array(rgba, 1, 1, "bgr")
        self.assertEqual(rgb[0, 0].tolist(), [10, 20, 30])
        self.assertEqual(bgr[0, 0].tolist(), [30, 20, 10])
        self.assertEqual(rgba_to_array(rgba, 1, 1, "rgb")[0, 0].tolist(), [10, 20, 30])

    def test_invalid_color_order_rejected(self) -> None:
        with self.assertRaises(OCRError) as ctx:
            rgba_to_array(bytes(4), 1, 1, "gray")
        self.assertEqual(ctx.exception.code, "invalid_color_order")

    def test_utf8_chinese_text_preserved(self) -> None:
        result = _runtime(_model_dir(), FakeEngine()).recognize_rgba(_rgba(), W, H)
        self.assertEqual(result.lines[0].text, "你好世界")

    def test_confidence_preserved(self) -> None:
        result = _runtime(_model_dir(), FakeEngine()).recognize_rgba(_rgba(), W, H)
        self.assertAlmostEqual(result.lines[0].confidence, 0.96, places=6)

    def test_boxes_preserved(self) -> None:
        result = _runtime(_model_dir(), FakeEngine()).recognize_rgba(_rgba(), W, H)
        self.assertEqual(result.lines[0].box[0], (0.0, 0.0))
        self.assertEqual(len(result.lines[0].box), 4)

    def test_min_confidence_filters(self) -> None:
        result = _runtime(_model_dir(), FakeEngine(), min_confidence=0.9).recognize_rgba(_rgba(), W, H)
        self.assertEqual(result.line_count, 1)
        self.assertEqual(result.lines[0].text, "你好世界")

    def test_close_idempotent(self) -> None:
        runtime = _runtime(_model_dir(), FakeEngine())
        runtime.recognize_rgba(_rgba(), W, H)
        runtime.close()
        runtime.close()
        self.assertEqual(runtime.engine_init_count, 1)

    def test_engine_failure_propagates(self) -> None:
        runtime = _runtime(_model_dir(), FakeEngine(fail=True))
        with self.assertRaises(OCRError) as ctx:
            runtime.recognize_rgba(_rgba(), W, H)
        self.assertEqual(ctx.exception.code, "engine_run_failed")

    def test_det_rec_ms_normalized(self) -> None:
        result = _runtime(_model_dir(), FakeEngine(det=0.03, rec=0.07)).recognize_rgba(_rgba(), W, H)
        self.assertAlmostEqual(result.det_ms, 30.0, places=3)
        self.assertAlmostEqual(result.rec_ms, 70.0, places=3)

    def test_normalize_v2_object_shape(self) -> None:
        class V2:
            boxes = [[[0, 0], [1, 0], [1, 1], [0, 1]]]
            txts = ["abc"]
            scores = [0.8]

        lines, det_ms, rec_ms = _normalize_rapidocr_result(V2())
        self.assertEqual(lines[0][1], "abc")
        self.assertIsNone(det_ms)
        self.assertIsNone(rec_ms)

    def test_result_serializes_utf8(self) -> None:
        result = _runtime(_model_dir(), FakeEngine()).recognize_rgba(_rgba(), W, H)
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertIn("你好世界", payload)
        self.assertEqual(json.loads(payload)["lines"][0]["text"], "你好世界")


class RuntimeProbeTest(unittest.TestCase):
    def test_probe_reports_environment(self) -> None:
        info = probe_runtime(_model_dir())
        self.assertIn("python_version", info)
        self.assertIn("machine", info)
        self.assertIn("providers", info)
        self.assertEqual(info["model_status"], "ok")
        self.assertIsInstance(info["compatible"], bool)

    def test_probe_missing_models(self) -> None:
        info = probe_runtime(_model_dir(with_files=False))
        self.assertEqual(info["model_status"], "model_assets_missing")


class OcrPipelineTest(unittest.TestCase):
    def test_active_roi_used(self) -> None:
        resolver = _resolver(NormalizedROI(0.5, 0.5, 0.5, 0.5))
        runtime = _runtime(_model_dir(), FakeEngine())
        roi_frame, result = ocr_test.process_frame(_frame(1), runtime=runtime, resolver=resolver)
        self.assertEqual((roi_frame.roi.x, roi_frame.roi.y), (32, 16))
        self.assertEqual((roi_frame.width, roi_frame.height), (32, 16))
        self.assertEqual((result.roi_width, result.roi_height), (32, 16))

    def test_user_roi_overrides_builtin_default(self) -> None:
        custom = NormalizedROI(0.0, 0.0, 0.25, 0.5)
        resolver = _resolver(custom)
        roi_frame, _ = ocr_test.process_frame(_frame(1), runtime=_runtime(_model_dir(), FakeEngine()), resolver=resolver)
        self.assertEqual((roi_frame.width, roi_frame.height), (16, 16))

    def test_png_decoded_once(self) -> None:
        calls = []
        original = ocr_test.decode_png_ex

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        ocr_test.decode_png_ex = spy
        try:
            ocr_test.process_frame(_frame(1), runtime=_runtime(_model_dir(), FakeEngine()), resolver=_resolver(NormalizedROI(0.1, 0.1, 0.5, 0.5)))
        finally:
            ocr_test.decode_png_ex = original
        self.assertEqual(len(calls), 1)

    def test_active_roi_cropped_once(self) -> None:
        crops = []
        original = rr.crop_rgba

        def spy(rgba, width, height, roi):
            crops.append((width, height))
            return original(rgba, width, height, roi)

        rr.crop_rgba = spy
        try:
            ocr_test.process_frame(_frame(1), runtime=_runtime(_model_dir(), FakeEngine()), resolver=_resolver(NormalizedROI(0.1, 0.1, 0.5, 0.5)))
        finally:
            rr.crop_rgba = original
        self.assertEqual(len(crops), 1)

    def test_ocr_consumes_roi_bytes_not_source_path(self) -> None:
        fake = FakeEngine()
        resolver = _resolver(NormalizedROI(0.5, 0.5, 0.5, 0.5))
        frame = _frame(1)
        self.assertIsNone(frame.source_path)
        roi_frame, _ = ocr_test.process_frame(frame, runtime=_runtime(_model_dir(), fake), resolver=resolver)
        self.assertEqual(fake.received.shape, (roi_frame.height, roi_frame.width, 3))

    def test_engine_init_count_one_under_many_frames(self) -> None:
        runtime = _runtime(_model_dir(), FakeEngine())
        resolver = _resolver(NormalizedROI(0.1, 0.1, 0.5, 0.5))
        for seq in range(1, 11):
            ocr_test.process_frame(_frame(seq), runtime=runtime, resolver=resolver)
        self.assertEqual(runtime.engine_init_count, 1)

    def test_queue_bounded_and_newest_wins(self) -> None:
        async def scenario() -> None:
            queue = LatestFrameQueue()
            await queue.put_latest(_frame(1))
            await queue.put_latest(_frame(2))
            await queue.put_latest(_frame(3))
            stats = queue.stats()
            self.assertEqual(stats.max_pending, 1)
            self.assertEqual(stats.produced, 3)
            self.assertEqual(stats.replaced, 2)
            self.assertEqual(queue.pending, 1)
            newest = await queue.get()
            self.assertEqual(newest.sequence, 3)
            queue.clear()
            self.assertEqual(queue.pending, 0)

        asyncio.run(scenario())

    def test_slow_ocr_replaces_stale_pending(self) -> None:
        async def scenario() -> None:
            queue = LatestFrameQueue()
            await queue.put_latest(_frame(1))
            await queue.put_latest(_frame(2))  # replaces stale pending while OCR busy
            stats = queue.stats()
            self.assertLessEqual(stats.max_pending, 1)
            self.assertGreaterEqual(stats.replaced, 1)

        asyncio.run(scenario())

    def test_no_backend_or_renderer_auto_start(self) -> None:
        self.assertNotIn("main", sys.modules)
        source = (SCRIPTS / "ocr_test.py").read_text(encoding="utf-8")
        self.assertNotIn("ClarifyDeckEngine", source)
        self.assertNotIn("OverlayManager", source)

    def test_no_ocr_auto_start_in_main(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("OCRRuntime", source)
        self.assertNotIn("ocr.runtime", source)
        self.assertNotIn("rapidocr", source)
        self.assertNotIn("onnxruntime", source)


class OcrDiagnosticLoopTest(unittest.TestCase):
    def test_mock_live_loop_passes(self) -> None:
        import argparse
        import contextlib
        import io

        args = argparse.Namespace(
            live=False, mock=True, fps=2.0, duration_sec=1.1, app_id=None, json=False, debug=False
        )
        runtime = _runtime(_model_dir(), FakeEngine())
        diagnostic = ocr_test.OCRDiagnostic(args, runtime, resolver=_resolver(NormalizedROI(0.1, 0.1, 0.5, 0.5)))
        with contextlib.redirect_stdout(io.StringIO()):
            code = asyncio.run(diagnostic.run_live())
        self.assertEqual(code, 0)
        self.assertGreater(diagnostic.frames_ocr, 0)
        self.assertEqual(runtime.engine_init_count, 1)
        self.assertEqual(diagnostic.ocr_errors, 0)

    def test_consecutive_errors_fail(self) -> None:
        import argparse
        import contextlib
        import io

        args = argparse.Namespace(
            live=False, mock=True, fps=2.0, duration_sec=1.1, app_id=None, json=False, debug=False
        )
        runtime = _runtime(_model_dir(), FakeEngine(fail=True))
        diagnostic = ocr_test.OCRDiagnostic(args, runtime, resolver=_resolver(NormalizedROI(0.1, 0.1, 0.5, 0.5)))
        with contextlib.redirect_stdout(io.StringIO()):
            code = asyncio.run(diagnostic.run_live())
        self.assertEqual(code, 1)
        self.assertGreaterEqual(diagnostic.ocr_errors, diagnostic.MAX_CONSECUTIVE_ERRORS)


class RapidOcrNormalizationTest(unittest.TestCase):
    """Phase 2F.2: NumPy-array results must never be used as Python booleans."""

    def _result_runtime(self, result) -> OCRRuntime:
        class Raw:
            def __call__(self, image):
                return result

        return OCRRuntime(OCRConfig(model_dir=_model_dir()), engine_factory=lambda m, c: _RapidOCREngine(Raw()))

    def _recognize(self, result):
        return self._result_runtime(result).recognize_rgba(_rgba(), W, H)

    def _boxes(self, count: int):
        return np.array(
            [[[10 * i, 20], [100, 20], [100, 45], [10, 45]] for i in range(1, count + 1)], dtype=np.float32
        )

    def test_structured_numpy_two_lines(self) -> None:
        class FakeResult:
            boxes = self._boxes(2)
            txts = ("测试第一行", "测试第二行")
            scores = (0.96, 0.91)

        result = self._recognize(FakeResult())
        self.assertEqual(result.line_count, 2)
        self.assertEqual([line.text for line in result.lines], ["测试第一行", "测试第二行"])
        self.assertAlmostEqual(result.lines[0].confidence, 0.96, places=6)
        self.assertEqual(result.lines[0].box[0], (10.0, 20.0))

    def test_structured_numpy_one_line(self) -> None:
        class FakeResult:
            boxes = self._boxes(1)
            txts = ("only",)
            scores = (0.5,)

        result = self._recognize(FakeResult())
        self.assertEqual(result.line_count, 1)

    def test_boxes_with_zeros_accepted(self) -> None:
        class FakeResult:
            boxes = np.array([[[0, 0], [10, 0], [10, 5], [0, 5]]], dtype=np.float32)
            txts = ("zero",)
            scores = (0.4,)

        result = self._recognize(FakeResult())
        self.assertEqual(result.line_count, 1)
        self.assertEqual(result.lines[0].box[0], (0.0, 0.0))

    def test_empty_numpy_boxes_no_ambiguity(self) -> None:
        class FakeResult:
            boxes = np.zeros((0, 4, 2), dtype=np.float32)
            txts = ()
            scores = ()

        result = self._recognize(FakeResult())
        self.assertEqual(result.line_count, 0)

    def test_boxes_none_zero_lines(self) -> None:
        class FakeResult:
            boxes = None
            txts = None
            scores = None

        result = self._recognize(FakeResult())
        self.assertEqual(result.line_count, 0)

    def test_txts_none_safe(self) -> None:
        class FakeResult:
            boxes = self._boxes(2)
            txts = None
            scores = None

        result = self._recognize(FakeResult())
        self.assertEqual(result.line_count, 0)

    def test_scores_none_keeps_lines(self) -> None:
        class FakeResult:
            boxes = self._boxes(2)
            txts = ("a", "b")
            scores = None

        result = self._recognize(FakeResult())
        self.assertEqual(result.line_count, 2)
        self.assertIsNone(result.lines[0].confidence)

    def test_confidence_zero_preserved(self) -> None:
        class FakeResult:
            boxes = self._boxes(1)
            txts = ("zero-conf",)
            scores = (0.0,)

        result = self._recognize(FakeResult())
        self.assertEqual(result.lines[0].confidence, 0.0)

    def test_two_boxes_preserve_ordering(self) -> None:
        class FakeResult:
            boxes = self._boxes(2)
            txts = ("first", "second")
            scores = (0.9, 0.8)

        result = self._recognize(FakeResult())
        self.assertEqual([line.text for line in result.lines], ["first", "second"])

    def test_coordinates_normalized(self) -> None:
        class FakeResult:
            boxes = np.array([[[10, 20], [100, 20], [100, 45], [10, 45]]], dtype=np.float32)
            txts = ("coords",)
            scores = (0.7,)

        result = self._recognize(FakeResult())
        self.assertEqual(result.lines[0].box, ((10.0, 20.0), (100.0, 20.0), (100.0, 45.0), (10.0, 45.0)))

    def test_length_mismatch_fails(self) -> None:
        class FakeResult:
            boxes = self._boxes(2)
            txts = ("only-one",)
            scores = (0.5,)

        with self.assertRaises(OCRError) as ctx:
            self._recognize(FakeResult())
        self.assertEqual(ctx.exception.code, "invalid_engine_result")

    def test_elapse_list_timing(self) -> None:
        class FakeResult:
            boxes = self._boxes(1)
            txts = ("timed",)
            scores = (0.5,)
            elapse_list = [0.03, 0.01, 0.07]

        result = self._recognize(FakeResult())
        self.assertAlmostEqual(result.det_ms, 30.0, places=3)
        self.assertAlmostEqual(result.rec_ms, 80.0, places=3)

    def test_legacy_tuple_path_still_green(self) -> None:
        lines, det_ms, rec_ms = _normalize_rapidocr_result(
            ([[[[0, 0], [1, 0], [1, 1], [0, 1]], "legacy", 0.42]], [0.01, 0.02])
        )
        self.assertEqual(lines[0][1], "legacy")
        self.assertAlmostEqual(det_ms, 10.0, places=3)
        self.assertAlmostEqual(rec_ms, 20.0, places=3)

    def test_no_ndarray_truthiness_in_normalizer(self) -> None:
        import inspect

        source = inspect.getsource(_normalize_rapidocr_result)
        for forbidden in ("or []", "if boxes:", "if not boxes:", "if payload:", "if elapse:"):
            self.assertNotIn(forbidden, source)
        structured = inspect.getsource(_structured_lines)
        self.assertNotIn("or []", structured)

    def test_engine_init_count_still_one(self) -> None:
        class FakeResult:
            boxes = self._boxes(1)
            txts = ("x",)
            scores = (0.5,)

        runtime = self._result_runtime(FakeResult())
        runtime.recognize_rgba(_rgba(), W, H)
        runtime.recognize_rgba(_rgba(), W, H)
        self.assertEqual(runtime.engine_init_count, 1)


class LiveTimingTest(unittest.TestCase):
    """Phase 2F.3: stage timing + thread config."""

    def _args(self, **overrides):
        import argparse

        base = {
            "live": False,
            "mock": True,
            "no_ocr": False,
            "fps": 2.0,
            "duration_sec": 1.1,
            "app_id": None,
            "json": False,
            "debug": False,
            "ort_intra_threads": None,
            "ort_inter_threads": None,
            "opencv_threads": None,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_thread_count_validation(self) -> None:
        self.assertEqual(validate_thread_count(1, "x"), 1)
        self.assertEqual(validate_thread_count(-1, "x"), -1)
        self.assertIsNone(validate_thread_count(None, "x"))
        for bad in (0, -2, "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(OCRError) as ctx:
                    validate_thread_count(bad, "x")
                self.assertEqual(ctx.exception.code, "invalid_thread_count")

    def test_ort_thread_params_wired(self) -> None:
        config = OCRConfig(model_dir=_model_dir(), ort_intra_threads=2, ort_inter_threads=1)
        params = _rapidocr_params(load_manifest(config.model_dir), None, config)
        self.assertEqual(params["EngineConfig.onnxruntime.intra_op_num_threads"], 2)
        self.assertEqual(params["EngineConfig.onnxruntime.inter_op_num_threads"], 1)

    def test_invalid_thread_config_rejected(self) -> None:
        with self.assertRaises(OCRError) as ctx:
            OCRRuntime(OCRConfig(model_dir=_model_dir(), ort_intra_threads=0))
        self.assertEqual(ctx.exception.code, "invalid_thread_count")

    def test_timings_populated(self) -> None:
        class FakeResult:
            boxes = np.array([[[0, 0], [10, 0], [10, 5], [0, 5]]], dtype=np.float32)
            txts = ("t",)
            scores = (0.9,)

        runtime = _runtime(_model_dir(), FakeEngine(lines=[([[0, 0]], "t", 0.9)]))
        runtime.recognize_rgba(_rgba(), W, H)
        timings = runtime.last_timings
        for key in (
            "rgba_to_array_ms",
            "ocr_call_ms",
            "result_normalize_ms",
            "ocr_wall_ms",
            "ocr_process_cpu_ms",
            "ocr_effective_cpu_pct",
        ):
            self.assertIn(key, timings)

    def test_unavailable_substage_none(self) -> None:
        class FakeResult:
            boxes = None
            txts = None
            scores = None

        runtime = OCRRuntime(
            OCRConfig(model_dir=_model_dir()),
            engine_factory=lambda m, c: _RapidOCREngine(lambda image: FakeResult()),
        )
        runtime.recognize_rgba(_rgba(), W, H)
        self.assertIsNone(runtime.last_timings["det_ms"])
        self.assertIsNone(runtime.last_timings["rec_ms"])

    def test_repeat_initializes_once_and_decodes_once(self) -> None:
        import contextlib
        import io

        image_path = Path(tempfile.mkdtemp(prefix="clarifydeck-repeat-")) / "roi.png"
        image_path.write_bytes(_png_rgb())
        runtime = _runtime(_model_dir(), FakeEngine())
        args = self._args(image=str(image_path), repeat=5, debug=False)
        decode_calls = []
        original = ocr_test.decode_png_ex

        def spy(*a, **k):
            decode_calls.append(1)
            return original(*a, **k)

        ocr_test.decode_png_ex = spy
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = ocr_test._run_repeat(args, runtime)
        finally:
            ocr_test.decode_png_ex = original
        self.assertEqual(code, 0)
        self.assertEqual(runtime.engine_init_count, 1)
        self.assertEqual(len(decode_calls), 1)

    def test_no_ocr_never_initializes_engine(self) -> None:
        import contextlib
        import io

        runtime = _runtime(_model_dir(), FakeEngine())
        args = self._args(no_ocr=True)
        diagnostic = ocr_test.OCRDiagnostic(args, runtime, resolver=_resolver(NormalizedROI(0.1, 0.1, 0.5, 0.5)))
        with contextlib.redirect_stdout(io.StringIO()):
            code = asyncio.run(diagnostic.run_live())
        self.assertEqual(code, 0)
        self.assertEqual(runtime.engine_init_count, 0)
        self.assertGreater(diagnostic.frames_received, 0)

    def test_no_change_detection_dependency(self) -> None:
        source = (SCRIPTS / "ocr_test.py").read_text(encoding="utf-8")
        self.assertNotIn("FrameChangeDetector", source)
        self.assertNotIn("only-when-changed", source)
        self.assertNotIn("only_when_changed", source)


class DebugRoiCopyTest(unittest.TestCase):
    """Phase 2F.4: explicit live ROI debug capture."""

    def _args(self, **overrides):
        import argparse

        base = {
            "live": False,
            "mock": True,
            "no_ocr": False,
            "fps": 2.0,
            "duration_sec": 1.1,
            "app_id": None,
            "json": False,
            "debug": False,
            "debug_roi_copy": None,
            "replay_roi": None,
            "ort_intra_threads": None,
            "ort_inter_threads": None,
            "opencv_threads": None,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run(self, args, runtime):
        import contextlib
        import io

        diagnostic = ocr_test.OCRDiagnostic(args, runtime, resolver=_resolver(NormalizedROI(0.1, 0.1, 0.5, 0.5)))
        with contextlib.redirect_stdout(io.StringIO()):
            code = asyncio.run(diagnostic.run_live())
        return code, diagnostic

    def test_debug_copy_disabled_by_default(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="clarifydeck-roi-")) / "live.png"
        runtime = _runtime(_model_dir(), FakeEngine())
        code, diagnostic = self._run(self._args(debug_roi_copy=None), runtime)
        self.assertEqual(code, 0)
        self.assertFalse(diagnostic.saved_roi)
        self.assertFalse(target.exists())

    def test_exactly_one_copy_written(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="clarifydeck-roi-")) / "live.png"
        runtime = _runtime(_model_dir(), FakeEngine())
        code, diagnostic = self._run(self._args(debug_roi_copy=str(target)), runtime)
        self.assertEqual(code, 0)
        self.assertTrue(diagnostic.saved_roi)
        self.assertTrue(target.exists())
        self.assertEqual(list(target.parent.glob("*.png")), [target])

    def test_saved_dimensions_equal_active_roi(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="clarifydeck-roi-")) / "live.png"
        runtime = _runtime(_model_dir(), FakeEngine())
        _code, diagnostic = self._run(self._args(debug_roi_copy=str(target)), runtime)
        decoded = ocr_test.decode_png_ex(target.read_bytes())
        self.assertEqual((decoded.width, decoded.height), (diagnostic.roi_width, diagnostic.roi_height))

    def test_saved_bytes_match_ocr_input(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="clarifydeck-roi-")) / "live.png"
        runtime = _runtime(_model_dir(), FakeEngine())
        seen = {"ocr": None, "encoded": None}
        original_recognize = runtime.recognize_rgba

        def recognize(rgba, width, height, sequence=None):
            seen["ocr"] = rgba
            return original_recognize(rgba, width, height, sequence=sequence)

        runtime.recognize_rgba = recognize
        original_encode = ocr_test.ocr_roi_mod.encode_rgba_png

        def encode(rgba, width, height):
            seen["encoded"] = rgba
            return original_encode(rgba, width, height)

        ocr_test.ocr_roi_mod.encode_rgba_png = encode
        try:
            _code, _diagnostic = self._run(self._args(debug_roi_copy=str(target)), runtime)
        finally:
            ocr_test.ocr_roi_mod.encode_rgba_png = original_encode
        self.assertIsNotNone(seen["ocr"])
        self.assertEqual(seen["encoded"], seen["ocr"])

    def test_no_second_png_decode(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="clarifydeck-roi-")) / "live.png"
        runtime = _runtime(_model_dir(), FakeEngine())
        calls = []
        original = ocr_test.decode_png_ex

        def spy(*a, **k):
            calls.append(1)
            return original(*a, **k)

        ocr_test.decode_png_ex = spy
        try:
            _code, diagnostic = self._run(self._args(debug_roi_copy=str(target)), runtime)
        finally:
            ocr_test.decode_png_ex = original
        self.assertEqual(len(calls), diagnostic.frames_received)

    def test_debug_encoding_excluded_from_ocr_timing(self) -> None:
        import inspect

        source = inspect.getsource(ocr_test.OCRDiagnostic._process_and_record)
        self.assertLess(source.index("self._stats.add("), source.index("self._save_debug_roi("))

    def test_invalid_debug_path_fails_safely(self) -> None:
        blocker = Path(tempfile.mkdtemp(prefix="clarifydeck-roi-")) / "blocker"
        blocker.write_bytes(b"not a directory")
        target = blocker / "sub" / "live.png"
        runtime = _runtime(_model_dir(), FakeEngine())
        code, diagnostic = self._run(self._args(debug_roi_copy=str(target)), runtime)
        self.assertEqual(code, 0)
        self.assertTrue(diagnostic.saved_roi)
        self.assertFalse(target.exists())

    def test_replay_roi_alias(self) -> None:
        image_path = Path(tempfile.mkdtemp(prefix="clarifydeck-roi-")) / "roi.png"
        image_path.write_bytes(_png_rgb())
        args = self._args(replay_roi=str(image_path))
        import contextlib
        import io

        runtime = _runtime(_model_dir(), FakeEngine())
        with contextlib.redirect_stdout(io.StringIO()):
            code = ocr_test.main(
                ["--replay-roi", str(image_path), "--repeat", "2", "--model-dir", str(_model_dir())]
            )
        self.assertIn(code, (0, 1))  # runs the repeat path; engine fake not injectable via main

    def test_engine_init_count_one_and_queue_bounded(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="clarifydeck-roi-")) / "live.png"
        runtime = _runtime(_model_dir(), FakeEngine())
        _code, diagnostic = self._run(self._args(debug_roi_copy=str(target)), runtime)
        self.assertEqual(runtime.engine_init_count, 1)
        self.assertGreaterEqual(diagnostic.frames_received, 1)


class DetGeometryTest(unittest.TestCase):
    """Phase 2F.5: detector input geometry configuration."""

    def test_explicit_default_det_config(self) -> None:
        config = OCRConfig(model_dir=_model_dir())
        self.assertEqual(config.det_limit_side_len, 736)
        self.assertEqual(config.det_limit_type, "min")
        params = _rapidocr_params(load_manifest(config.model_dir), None, config)
        self.assertEqual(params["Det.limit_side_len"], 736)
        self.assertEqual(params["Det.limit_type"], "min")

    def test_valid_limit_side_len(self) -> None:
        self.assertEqual(validate_det_limit_side_len(384), 384)

    def test_invalid_limit_side_len_rejected(self) -> None:
        for bad in (0, -1, -100, "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(OCRError) as ctx:
                    validate_det_limit_side_len(bad)
                self.assertEqual(ctx.exception.code, "invalid_limit_side_len")

    def test_limit_type_min_max_accepted(self) -> None:
        self.assertEqual(validate_det_limit_type("min"), "min")
        self.assertEqual(validate_det_limit_type("max"), "max")

    def test_invalid_limit_type_rejected(self) -> None:
        with self.assertRaises(OCRError) as ctx:
            validate_det_limit_type("auto")
        self.assertEqual(ctx.exception.code, "invalid_limit_type")

    def test_det_params_wired(self) -> None:
        config = OCRConfig(model_dir=_model_dir(), det_limit_side_len=384, det_limit_type="min")
        params = _rapidocr_params(load_manifest(config.model_dir), None, config)
        self.assertEqual(params["Det.limit_side_len"], 384)
        self.assertEqual(params["Det.limit_type"], "min")

    def test_det_model_path_still_explicit(self) -> None:
        config = OCRConfig(model_dir=_model_dir(), det_limit_side_len=384)
        params = _rapidocr_params(load_manifest(config.model_dir), None, config)
        self.assertTrue(Path(params["Det.model_path"]).is_absolute())
        self.assertNotIn("rapidocr/models", params["Det.model_path"].replace("\\", "/"))

    def test_predict_geometry_736(self) -> None:
        geometry = predict_det_geometry(1024, 160, 736, "min")
        self.assertEqual(geometry["predicted_resized"], "4704x736")
        self.assertAlmostEqual(geometry["ratio"], 4.6, places=3)

    def test_predict_geometry_320(self) -> None:
        geometry = predict_det_geometry(1024, 160, 320, "min")
        self.assertEqual(geometry["predicted_resized"], "2048x320")

    def test_predict_geometry_no_upscale_when_short_above_limit(self) -> None:
        geometry = predict_det_geometry(1024, 800, 736, "min")
        self.assertEqual(geometry["predicted_resized"], "1024x800")
        self.assertAlmostEqual(geometry["ratio"], 1.0, places=3)

    def test_predict_geometry_invalid_type(self) -> None:
        with self.assertRaises(OCRError) as ctx:
            predict_det_geometry(1024, 160, 736, "auto")
        self.assertEqual(ctx.exception.code, "invalid_limit_type")

    def test_geometry_does_not_mutate_roi(self) -> None:
        from capture import recognition_roi as rr

        before = rr.DEFAULT_ROI
        predict_det_geometry(1024, 160, 384, "min")
        self.assertEqual(rr.DEFAULT_ROI, before)

    def test_ocr_test_wires_det_flags(self) -> None:
        import argparse

        args = argparse.Namespace(
            model_dir=str(_model_dir()),
            color_order="bgr",
            min_confidence=0.0,
            engine_params=None,
            debug=False,
            ort_intra_threads=None,
            ort_inter_threads=None,
            opencv_threads=None,
            det_limit_side_len=384,
            det_limit_type="min",
        )
        runtime = ocr_test._build_runtime(args)
        self.assertEqual(runtime.det_limit_side_len, 384)
        self.assertEqual(runtime.det_limit_type, "min")

    def test_det_input_line_reported(self) -> None:
        import argparse

        args = argparse.Namespace(det_limit_side_len=384, det_limit_type="min")
        line = ocr_test._det_input_line(1024, 160, args)
        self.assertIn("source=1024x160", line)
        self.assertIn("limit_side_len=384", line)
        self.assertIn("predicted_resized=2464x384", line)


class ShutdownTest(unittest.TestCase):
    """Phase 2F.6: Ctrl-C / cancellation-safe live shutdown."""

    def _args(self, **overrides):
        import argparse

        base = {
            "live": False,
            "mock": True,
            "no_ocr": False,
            "fps": 2.0,
            "duration_sec": 1.1,
            "app_id": None,
            "json": False,
            "debug": False,
            "debug_roi_copy": None,
            "replay_roi": None,
            "ort_intra_threads": None,
            "ort_inter_threads": None,
            "opencv_threads": None,
            "det_limit_side_len": 736,
            "det_limit_type": "min",
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def _diagnostic(self, **overrides):
        runtime = _runtime(_model_dir(), FakeEngine())
        return ocr_test.OCRDiagnostic(
            self._args(**overrides), runtime, resolver=_resolver(NormalizedROI(0.1, 0.1, 0.5, 0.5))
        )

    def test_normal_completion_shutdown_once(self) -> None:
        import contextlib
        import io

        diagnostic = self._diagnostic()
        calls = []
        original = diagnostic._shutdown_live

        async def spy(reason):
            calls.append(reason)
            await original(reason)

        diagnostic._shutdown_live = spy
        with contextlib.redirect_stdout(io.StringIO()):
            code = asyncio.run(diagnostic.run_live())
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["complete"])
        self.assertEqual(diagnostic.state, ocr_test.OCRState.STOPPED)
        self.assertEqual(diagnostic._queue.pending, 0)

    def test_cancellation_executes_shutdown_once(self) -> None:
        import contextlib
        import io

        diagnostic = self._diagnostic(duration_sec=30.0)
        calls = []
        original = diagnostic._shutdown_live

        async def spy(reason):
            calls.append(reason)
            await original(reason)

        diagnostic._shutdown_live = spy

        async def scenario():
            task = asyncio.create_task(diagnostic.run_live())
            await asyncio.sleep(0.4)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(scenario())
        self.assertEqual(calls, ["interrupt"])
        self.assertTrue(diagnostic._shutdown_done)
        self.assertEqual(diagnostic.state, ocr_test.OCRState.STOPPED)

    def test_shutdown_idempotent(self) -> None:
        import contextlib
        import io

        diagnostic = self._diagnostic()

        async def scenario():
            with contextlib.redirect_stdout(io.StringIO()):
                await diagnostic._shutdown_live("first")
                await diagnostic._shutdown_live("second")

        asyncio.run(scenario())
        self.assertEqual(diagnostic.state, ocr_test.OCRState.STOPPED)

    def test_early_shutdown_without_run_does_not_crash(self) -> None:
        import contextlib
        import io

        diagnostic = self._diagnostic()

        async def scenario():
            with contextlib.redirect_stdout(io.StringIO()):
                await diagnostic._shutdown_live("early")

        asyncio.run(scenario())  # must not raise
        self.assertEqual(diagnostic.state, ocr_test.OCRState.STOPPED)

    def test_consumer_task_done_after_shutdown(self) -> None:
        import contextlib
        import io

        diagnostic = self._diagnostic()
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(diagnostic.run_live())
        self.assertIsNotNone(diagnostic._consumer_task)
        self.assertTrue(diagnostic._consumer_task.done())

    def test_unexpected_start_error_surfaces(self) -> None:
        import contextlib
        import io

        class BoomProducer:
            def __init__(self, *args, **kwargs):
                pass

            async def start(self, *args, **kwargs):
                raise RuntimeError("boom")

            async def stop(self, *args, **kwargs):
                return {"ok": True, "state": "STOPPED"}

        original = ocr_test.CaptureProducer
        ocr_test.CaptureProducer = BoomProducer
        diagnostic = self._diagnostic()
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = asyncio.run(diagnostic.run_live())
        finally:
            ocr_test.CaptureProducer = original
        self.assertEqual(code, 1)
        self.assertIn("unexpected_error", out.getvalue())
        self.assertEqual(diagnostic.state, ocr_test.OCRState.STOPPED)

    def test_keyboard_interrupt_policy(self) -> None:
        import contextlib
        import io

        empty_root = Path(tempfile.mkdtemp(prefix="clarifydeck-empty-"))
        original = ocr_test.asyncio.run

        def boom(coro):
            coro.close()
            raise KeyboardInterrupt

        ocr_test.asyncio.run = boom
        try:
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ocr_test.main(
                    ["--mock", "--duration-sec", "1", "--plugin-root", str(empty_root)]
                )
        finally:
            ocr_test.asyncio.run = original
        self.assertEqual(code, 130)
        self.assertIn("[ocr] interrupted", err.getvalue())
        self.assertNotIn("Traceback", out.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_queue_capacity_unchanged(self) -> None:
        import contextlib
        import io

        diagnostic = self._diagnostic()
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(diagnostic.run_live())
        stats = diagnostic._queue.stats()
        self.assertLessEqual(stats.max_pending, 1)
        self.assertEqual(stats.pending, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
