#!/usr/bin/env python3
"""Phase 2I.1 tests: backend OCR transport receiver + import safety.

Run:
    python3 scripts/test_backend_ocr_transport.py
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for candidate in (str(ROOT), str(SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from backend.ocr_transport import OCRTransportReceiver  # noqa: E402
from ocr.stabilizer import StableTextEvent  # noqa: E402
from ocr.transport import encode_envelope, envelope_from_event  # noqa: E402

NATIVE_MODULES = ("rapidocr", "onnxruntime", "numpy", "cv2", "omegaconf", "antlr4")


def _text_line(seq, text="hello", confidence=0.9, source_seq=3, ts=1.0):
    event = StableTextEvent(kind="text", text=text, confidence=confidence, source_seq=source_seq, timestamp_monotonic=ts)
    return encode_envelope(envelope_from_event(seq, event))


def _clear_line(seq, ts=2.0):
    event = StableTextEvent(kind="clear", text="", confidence=None, source_seq=None, timestamp_monotonic=ts)
    return encode_envelope(envelope_from_event(seq, event))


class ImportSafetyTest(unittest.TestCase):
    def test_transport_imports_no_native_deps(self) -> None:
        code = (
            "import sys; import backend.ocr_transport; import ocr.transport; "
            f"bad=[m for m in {NATIVE_MODULES!r} if m in sys.modules]; "
            "print('BAD' if bad else 'OK', bad)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True
        )
        self.assertIn("OK", result.stdout, msg=result.stdout + result.stderr)
        self.assertNotIn("BAD", result.stdout)

    def test_modules_do_not_reference_native_imports(self) -> None:
        for relative in ("backend/ocr_transport.py", "ocr/transport.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            for module in NATIVE_MODULES:
                self.assertNotIn(f"import {module}", source, msg=f"{relative} imports {module}")

    def test_main_imports_no_ocr_native(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        for module in ("numpy", "onnxruntime", "rapidocr", "cv2"):
            self.assertNotIn(f"import {module}", source)


class ReceiverTest(unittest.TestCase):
    def test_first_event_accepted(self) -> None:
        receiver = OCRTransportReceiver(session_id="s1")
        self.assertTrue(receiver.handle_line(_text_line(1)))
        self.assertEqual(receiver.state().last_event_seq, 1)
        self.assertEqual(receiver.state().kind, "text")
        self.assertEqual(receiver.state().text, "hello")
        self.assertEqual(receiver.status()["state"], "text")

    def test_increasing_event_seq_accepted(self) -> None:
        receiver = OCRTransportReceiver()
        self.assertTrue(receiver.handle_line(_text_line(1, text="a")))
        self.assertTrue(receiver.handle_line(_text_line(2, text="b")))
        self.assertEqual(receiver.state().text, "b")

    def test_duplicate_event_seq_rejected(self) -> None:
        receiver = OCRTransportReceiver()
        receiver.handle_line(_text_line(1, text="a"))
        self.assertFalse(receiver.handle_line(_text_line(1, text="b")))
        self.assertEqual(receiver.state().text, "a")
        self.assertEqual(receiver.status()["transport_out_of_order"], 1)

    def test_out_of_order_rejected(self) -> None:
        receiver = OCRTransportReceiver()
        receiver.handle_line(_text_line(5, text="a"))
        self.assertFalse(receiver.handle_line(_text_line(2, text="b")))
        self.assertEqual(receiver.state().last_event_seq, 5)

    def test_new_session_resets_boundary(self) -> None:
        receiver = OCRTransportReceiver(session_id="s1")
        receiver.handle_line(_text_line(5, text="a"))
        receiver.begin_session(session_id="s2")
        self.assertEqual(receiver.state().last_event_seq, 0)
        self.assertTrue(receiver.handle_line(_text_line(1, text="b")))
        self.assertEqual(receiver.state().worker_session_id, "s2")

    def test_rejected_line_does_not_overwrite_state(self) -> None:
        receiver = OCRTransportReceiver()
        receiver.handle_line(_text_line(1, text="good"))
        self.assertFalse(receiver.handle_line("{not json"))
        self.assertEqual(receiver.state().text, "good")
        self.assertEqual(receiver.status()["transport_messages_rejected"], 1)

    def test_clear_updates_state(self) -> None:
        receiver = OCRTransportReceiver()
        receiver.handle_line(_text_line(1, text="good"))
        self.assertTrue(receiver.handle_line(_clear_line(2)))
        self.assertEqual(receiver.state().kind, "clear")
        self.assertEqual(receiver.state().text, "")
        self.assertEqual(receiver.status()["state"], "clear")

    def test_status_fields_present(self) -> None:
        receiver = OCRTransportReceiver()
        receiver.handle_line(_text_line(1))
        status = receiver.status()
        for key in (
            "state",
            "worker_session_id",
            "last_event_seq",
            "last_kind",
            "last_text",
            "last_confidence",
            "last_source_seq",
            "last_timestamp_monotonic",
            "transport_messages_received",
            "transport_messages_rejected",
            "transport_out_of_order",
            "transport_text_events",
            "transport_clear_events",
            "last_transport_error",
        ):
            self.assertIn(key, status)

    def test_counters_accumulate(self) -> None:
        receiver = OCRTransportReceiver()
        receiver.handle_line(_text_line(1))
        receiver.handle_line(_clear_line(2))
        receiver.handle_line("bad")
        status = receiver.status()
        self.assertEqual(status["transport_messages_received"], 3)
        self.assertEqual(status["transport_messages_rejected"], 1)
        self.assertEqual(status["transport_text_events"], 1)
        self.assertEqual(status["transport_clear_events"], 1)


class EndToEndHarnessTest(unittest.TestCase):
    def test_event_encode_receive_clear(self) -> None:
        receiver = OCRTransportReceiver(session_id="harness")
        event = StableTextEvent(kind="text", text="示例字幕", confidence=0.93, source_seq=42, timestamp_monotonic=12345.678)
        line = encode_envelope(envelope_from_event(1, event))
        self.assertTrue(receiver.handle_line(line))
        state = receiver.state()
        self.assertEqual(state.text, "示例字幕")
        self.assertAlmostEqual(state.confidence, 0.93)
        self.assertEqual(state.source_seq, 42)

        clear = StableTextEvent(kind="clear", text="", confidence=None, source_seq=None, timestamp_monotonic=12348.0)
        self.assertTrue(receiver.handle_line(encode_envelope(envelope_from_event(2, clear))))
        self.assertEqual(receiver.state().kind, "clear")
        self.assertEqual(receiver.state().text, "")


class DiagnosticFlagTest(unittest.TestCase):
    def test_emit_stable_jsonl_default_off(self) -> None:
        source = (SCRIPTS / "ocr_test.py").read_text(encoding="utf-8")
        self.assertIn('"--emit-stable-jsonl"', source)
        self.assertIn('action="store_true"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
