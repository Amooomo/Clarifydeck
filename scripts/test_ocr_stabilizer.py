#!/usr/bin/env python3
"""Phase 2G tests: OCR output stabilization.

Run:
    python3 scripts/test_ocr_stabilizer.py
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for candidate in (str(ROOT), str(SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from ocr.stabilizer import (  # noqa: E402
    OCRStabilizer,
    StabilizerConfigError,
    build_candidate_text,
    normalize_line,
)


@dataclass
class Line:
    text: str
    confidence: float | None


def _emit_texts(events):
    return [event.text for event in events if event.kind == "text"]


def _kinds(events):
    return [event.kind for event in events]


class ConsensusTest(unittest.TestCase):
    def test_one_frame_no_emit(self) -> None:
        stabilizer = OCRStabilizer()
        self.assertEqual(stabilizer.observe([Line("A", 0.9)], 1, 0.0), [])

    def test_two_identical_frames_emit(self) -> None:
        stabilizer = OCRStabilizer()
        stabilizer.observe([Line("A", 0.9)], 1, 0.0)
        events = stabilizer.observe([Line("A", 0.9)], 2, 1.0)
        self.assertEqual(_emit_texts(events), ["A"])
        self.assertEqual(stabilizer.last_emitted_text, "A")

    def test_duplicate_stable_text_suppressed(self) -> None:
        stabilizer = OCRStabilizer()
        stabilizer.observe([Line("A", 0.9)], 1, 0.0)
        stabilizer.observe([Line("A", 0.9)], 2, 1.0)
        events = stabilizer.observe([Line("A", 0.9)], 3, 2.0)
        self.assertEqual(events, [])
        self.assertEqual(stabilizer.stats().duplicate_suppressed, 1)

    def test_aabb_emits_a_then_b(self) -> None:
        stabilizer = OCRStabilizer()
        emits = []
        for seq, text in enumerate(["A", "A", "B", "B"], start=1):
            emits += _emit_texts(stabilizer.observe([Line(text, 0.9)], seq, float(seq)))
        self.assertEqual(emits, ["A", "B"])

    def test_aba_emits_a(self) -> None:
        stabilizer = OCRStabilizer()
        emits = []
        for seq, text in enumerate(["A", "B", "A"], start=1):
            emits += _emit_texts(stabilizer.observe([Line(text, 0.9)], seq, float(seq)))
        self.assertEqual(emits, ["A"])

    def test_abc_emits_none(self) -> None:
        stabilizer = OCRStabilizer()
        emits = []
        for seq, text in enumerate(["A", "B", "C"], start=1):
            emits += _emit_texts(stabilizer.observe([Line(text, 0.9)], seq, float(seq)))
        self.assertEqual(emits, [])

    def test_direct_transition_no_clear_between(self) -> None:
        stabilizer = OCRStabilizer()
        emits = []
        for seq, text in enumerate(["A", "A", "B", "B"], start=1):
            for event in stabilizer.observe([Line(text, 0.9)], seq, float(seq)):
                emits.append((event.kind, event.text))
        self.assertEqual(emits, [("text", "A"), ("text", "B")])


class ConfidenceTest(unittest.TestCase):
    def test_low_confidence_line_dropped(self) -> None:
        stabilizer = OCRStabilizer(min_line_confidence=0.70)
        self.assertEqual(stabilizer.observe([Line("A", 0.5)], 1, 0.0), [])
        self.assertEqual(stabilizer.stats().low_conf_lines_dropped, 1)

    def test_confidence_at_threshold_accepted(self) -> None:
        stabilizer = OCRStabilizer(min_line_confidence=0.70)
        stabilizer.observe([Line("A", 0.70)], 1, 0.0)
        events = stabilizer.observe([Line("A", 0.70)], 2, 1.0)
        self.assertEqual(_emit_texts(events), ["A"])

    def test_missing_confidence_rejected(self) -> None:
        stabilizer = OCRStabilizer()
        self.assertEqual(stabilizer.observe([Line("A", None)], 1, 0.0), [])
        self.assertEqual(stabilizer.stats().low_conf_lines_dropped, 1)

    def test_mixed_confidence_keeps_eligible(self) -> None:
        stabilizer = OCRStabilizer(min_line_confidence=0.70)
        stabilizer.observe([Line("A", 0.9), Line("noise", 0.2)], 1, 0.0)
        events = stabilizer.observe([Line("A", 0.9), Line("noise", 0.2)], 2, 1.0)
        self.assertEqual(_emit_texts(events), ["A"])


class NormalizationTest(unittest.TestCase):
    def test_utf8_chinese_preserved(self) -> None:
        stabilizer = OCRStabilizer()
        text = "这座寺院，似乎会聚集无处可去的人。"
        stabilizer.observe([Line(text, 0.9)], 1, 0.0)
        events = stabilizer.observe([Line(text, 0.9)], 2, 1.0)
        self.assertEqual(_emit_texts(events), [text])

    def test_punctuation_and_ellipsis_preserved(self) -> None:
        stabilizer = OCRStabilizer()
        text = "幻影……?"
        stabilizer.observe([Line(text, 0.9)], 1, 0.0)
        events = stabilizer.observe([Line(text, 0.9)], 2, 1.0)
        self.assertEqual(_emit_texts(events), [text])

    def test_nfc_normalization(self) -> None:
        self.assertEqual(normalize_line("e\u0301"), "\u00e9")

    def test_inner_repeated_spaces_collapsed(self) -> None:
        self.assertEqual(normalize_line("a   b\t\tc"), "a b c")

    def test_crlf_normalized(self) -> None:
        self.assertEqual(normalize_line("a\r\nb\rc"), "a\nb\nc")

    def test_empty_lines_removed(self) -> None:
        text = build_candidate_text([Line("A", 0.9), Line("   ", 0.9), Line("B", 0.9)])
        self.assertEqual(text, "A\nB")

    def test_line_order_preserved(self) -> None:
        text = build_candidate_text([Line("first", 0.9), Line("second", 0.9)])
        self.assertEqual(text, "first\nsecond")


class StaleTimeoutTest(unittest.TestCase):
    def _stable(self) -> OCRStabilizer:
        stabilizer = OCRStabilizer(stale_timeout_sec=2.0)
        stabilizer.observe([Line("A", 0.9)], 1, 0.0)
        stabilizer.observe([Line("A", 0.9)], 2, 0.5)
        return stabilizer

    def test_empty_does_not_immediately_clear(self) -> None:
        stabilizer = self._stable()
        self.assertEqual(stabilizer.observe([], 3, 1.0), [])

    def test_stale_timeout_emits_one_clear(self) -> None:
        stabilizer = self._stable()
        events = stabilizer.observe([], 3, 3.0)
        self.assertEqual(_kinds(events), ["clear"])

    def test_repeated_empty_no_spam(self) -> None:
        stabilizer = self._stable()
        stabilizer.observe([], 3, 3.0)
        self.assertEqual(stabilizer.observe([], 4, 4.0), [])
        self.assertEqual(stabilizer.observe([], 5, 5.0), [])
        self.assertEqual(stabilizer.stats().clear_emits, 1)

    def test_new_text_after_clear_emits(self) -> None:
        stabilizer = self._stable()
        stabilizer.observe([], 3, 3.0)
        stabilizer.observe([Line("B", 0.9)], 4, 4.0)
        events = stabilizer.observe([Line("B", 0.9)], 5, 5.0)
        self.assertEqual(_emit_texts(events), ["B"])


class TickTest(unittest.TestCase):
    """Phase 2H: stabilizer tick for skipped (unchanged-ROI) frames."""

    def _stable(self) -> OCRStabilizer:
        stabilizer = OCRStabilizer(stale_timeout_sec=2.0)
        stabilizer.observe([Line("A", 0.9)], 1, 0.0)
        stabilizer.observe([Line("A", 0.9)], 2, 1.0)
        return stabilizer

    def test_tick_preserves_stable_text(self) -> None:
        stabilizer = self._stable()
        for tick_time in (2.0, 4.0, 6.0, 10.0):
            self.assertEqual(stabilizer.tick(tick_time), [])
        self.assertEqual(stabilizer.last_emitted_text, "A")

    def test_tick_does_not_add_history_or_raw_frames(self) -> None:
        stabilizer = self._stable()
        before_frames = stabilizer.stats().raw_frames
        before_history = stabilizer.history_length
        stabilizer.tick(5.0)
        stabilizer.tick(6.0)
        self.assertEqual(stabilizer.stats().raw_frames, before_frames)
        self.assertEqual(stabilizer.history_length, before_history)

    def test_tick_after_empty_allows_clear(self) -> None:
        stabilizer = self._stable()
        stabilizer.observe([], 3, 1.5)
        events = stabilizer.tick(4.0)
        self.assertEqual(_kinds(events), ["clear"])

    def test_tick_before_any_observation_no_clear(self) -> None:
        stabilizer = OCRStabilizer()
        self.assertEqual(stabilizer.tick(10.0), [])


class LifecycleTest(unittest.TestCase):
    def test_reset_clears_state(self) -> None:
        stabilizer = OCRStabilizer()
        stabilizer.observe([Line("A", 0.9)], 1, 0.0)
        stabilizer.observe([Line("A", 0.9)], 2, 1.0)
        stabilizer.reset()
        self.assertIsNone(stabilizer.last_emitted_text)
        self.assertEqual(stabilizer.history_length, 0)
        self.assertEqual(stabilizer.stats().raw_frames, 0)
        stabilizer.observe([Line("A", 0.9)], 3, 2.0)
        events = stabilizer.observe([Line("A", 0.9)], 4, 3.0)
        self.assertEqual(_emit_texts(events), ["A"])

    def test_history_bounded(self) -> None:
        stabilizer = OCRStabilizer(consensus_required=1, history_size=3)
        for seq, text in enumerate(["A", "B", "C", "D"], start=1):
            stabilizer.observe([Line(text, 0.9)], seq, float(seq))
        self.assertEqual(stabilizer.history_length, 3)

    def test_invalid_confidence_threshold_rejected(self) -> None:
        for bad in (-0.1, 1.1, "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(StabilizerConfigError) as ctx:
                    OCRStabilizer(min_line_confidence=bad)
                self.assertEqual(ctx.exception.code, "invalid_min_line_confidence")

    def test_invalid_consensus_rejected(self) -> None:
        with self.assertRaises(StabilizerConfigError) as ctx:
            OCRStabilizer(consensus_required=0)
        self.assertEqual(ctx.exception.code, "invalid_consensus_required")

    def test_history_smaller_than_consensus_rejected(self) -> None:
        with self.assertRaises(StabilizerConfigError) as ctx:
            OCRStabilizer(consensus_required=3, history_size=2)
        self.assertEqual(ctx.exception.code, "invalid_history_size")

    def test_invalid_stale_timeout_rejected(self) -> None:
        for bad in (0.0, -1.0, "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(StabilizerConfigError) as ctx:
                    OCRStabilizer(stale_timeout_sec=bad)
                self.assertEqual(ctx.exception.code, "invalid_stale_timeout")

    def test_stats_counters(self) -> None:
        stabilizer = OCRStabilizer()
        stabilizer.observe([Line("A", 0.9)], 1, 0.0)
        stabilizer.observe([Line("A", 0.9)], 2, 1.0)
        stabilizer.observe([Line("A", 0.9)], 3, 2.0)
        stats = stabilizer.stats()
        self.assertEqual(stats.raw_frames, 3)
        self.assertEqual(stats.eligible_candidates, 3)
        self.assertEqual(stats.stable_text_emits, 1)
        self.assertEqual(stats.duplicate_suppressed, 1)


class DependencyIsolationTest(unittest.TestCase):
    def test_stabilizer_imports_no_native_deps(self) -> None:
        source = (ROOT / "ocr" / "stabilizer.py").read_text(encoding="utf-8")
        for forbidden in (
            "import numpy",
            "import cv2",
            "import onnxruntime",
            "import rapidocr",
            "from capture",
            "import capture",
        ):
            self.assertNotIn(forbidden, source)


class LiveIntegrationTest(unittest.TestCase):
    def _run_live(self, extra_args):
        import argparse

        import ocr_test
        from ocr import OCRConfig, OCRRuntime
        from ocr.runtime import _RapidOCREngine

        class FakeRaw:
            def __call__(self, image):
                class Result:
                    boxes = None
                    txts = ("稳定字幕",)
                    scores = (0.95,)

                return Result()

        model_dir = Path(tempfile.mkdtemp(prefix="clarifydeck-stab-")) / "ppocrv6"
        model_dir.mkdir(parents=True)
        for name in ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx"):
            (model_dir / name).write_bytes(b"x")
        import json

        (model_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "format_version": 2,
                    "engine": "rapidocr",
                    "family": "PP-OCRv6",
                    "model_type": "small",
                    "engine_type": "onnxruntime",
                    "files": {
                        "det": {"path": "PP-OCRv6_det_small.onnx"},
                        "rec": {"path": "PP-OCRv6_rec_small.onnx"},
                    },
                    "dictionary": {"mode": "embedded"},
                }
            ),
            encoding="utf-8",
        )
        runtime = OCRRuntime(OCRConfig(model_dir=model_dir), engine_factory=lambda m, c: _RapidOCREngine(FakeRaw()))
        args = argparse.Namespace(
            live=False,
            mock=True,
            no_ocr=False,
            fps=4.0,
            duration_sec=1.4,
            app_id=None,
            json=False,
            debug=False,
            debug_roi_copy=None,
            replay_roi=None,
            ort_intra_threads=None,
            ort_inter_threads=None,
            opencv_threads=None,
            det_limit_side_len=736,
            det_limit_type="min",
        )
        for key, value in extra_args.items():
            setattr(args, key, value)
        stabilizer = OCRStabilizer() if getattr(args, "stable_output", False) else None
        diagnostic = ocr_test.OCRDiagnostic(args, runtime, stabilizer=stabilizer)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = asyncio.run(diagnostic.run_live())
        return code, out.getvalue(), runtime, stabilizer

    def test_stable_output_emits_and_suppresses(self) -> None:
        code, output, runtime, stabilizer = self._run_live({"stable_output": True})
        self.assertEqual(code, 0)
        self.assertIn("[ocr-stable] emit=text", output)
        self.assertIn("稳定字幕", output)
        self.assertGreaterEqual(stabilizer.stats().duplicate_suppressed, 1)
        self.assertEqual(runtime.engine_init_count, 1)

    def test_raw_path_unchanged_without_flag(self) -> None:
        code, output, _runtime, stabilizer = self._run_live({"stable_output": False})
        self.assertEqual(code, 0)
        self.assertNotIn("[ocr-stable]", output)
        self.assertIsNone(stabilizer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
