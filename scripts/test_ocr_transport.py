#!/usr/bin/env python3
"""Phase 2I.1 tests: StableTextEvent transport protocol v1.

Run:
    python3 scripts/test_ocr_transport.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ocr.stabilizer import StableTextEvent  # noqa: E402
from ocr.transport import (  # noqa: E402
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    StableTextEnvelope,
    TransportError,
    decode_envelope,
    encode_envelope,
    envelope_from_event,
)


def _text_event(text="hello", confidence=0.9, source_seq=42, ts=12345.678):
    return StableTextEvent(kind="text", text=text, confidence=confidence, source_seq=source_seq, timestamp_monotonic=ts)


def _clear_event(ts=12348.001):
    return StableTextEvent(kind="clear", text="", confidence=None, source_seq=None, timestamp_monotonic=ts)


class RoundTripTest(unittest.TestCase):
    def test_text_round_trip(self) -> None:
        envelope = envelope_from_event(17, _text_event())
        decoded = decode_envelope(encode_envelope(envelope))
        self.assertEqual(decoded.version, PROTOCOL_VERSION)
        self.assertEqual(decoded.event_seq, 17)
        self.assertEqual(decoded.kind, "text")
        self.assertEqual(decoded.text, "hello")
        self.assertAlmostEqual(decoded.confidence, 0.9)
        self.assertEqual(decoded.source_seq, 42)
        self.assertAlmostEqual(decoded.timestamp_monotonic, 12345.678)

    def test_clear_round_trip(self) -> None:
        envelope = envelope_from_event(18, _clear_event())
        decoded = decode_envelope(encode_envelope(envelope))
        self.assertEqual(decoded.kind, "clear")
        self.assertEqual(decoded.text, "")
        self.assertIsNone(decoded.confidence)
        self.assertIsNone(decoded.source_seq)

    def test_unicode_round_trip(self) -> None:
        text = "这座寺院，似乎会聚集无处可去的人。"
        envelope = envelope_from_event(1, _text_event(text=text))
        decoded = decode_envelope(encode_envelope(envelope))
        self.assertEqual(decoded.text, text)

    def test_event_seq_starts_at_one(self) -> None:
        decoded = decode_envelope(encode_envelope(envelope_from_event(1, _text_event())))
        self.assertEqual(decoded.event_seq, 1)

    def test_event_seq_increments(self) -> None:
        for seq in (1, 2, 3, 100):
            decoded = decode_envelope(encode_envelope(envelope_from_event(seq, _text_event())))
            self.assertEqual(decoded.event_seq, seq)

    def test_envelope_shape(self) -> None:
        payload = json.loads(encode_envelope(envelope_from_event(1, _text_event())))
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["type"], "stable_text")
        self.assertEqual(set(payload), {"v", "type", "event_seq", "kind", "text", "confidence", "source_seq", "timestamp_monotonic"})


class ValidationTest(unittest.TestCase):
    def _base(self, **overrides):
        payload = {
            "v": 1,
            "type": "stable_text",
            "event_seq": 1,
            "kind": "text",
            "text": "hi",
            "confidence": 0.9,
            "source_seq": 3,
            "timestamp_monotonic": 1.0,
        }
        payload.update(overrides)
        return json.dumps(payload)

    def _expect(self, code, line) -> None:
        with self.assertRaises(TransportError) as ctx:
            decode_envelope(line)
        self.assertEqual(ctx.exception.code, code)

    def test_unknown_version_rejected(self) -> None:
        self._expect("unsupported_version", self._base(v=99))

    def test_unknown_type_rejected(self) -> None:
        self._expect("unsupported_type", self._base(type="other"))

    def test_unknown_kind_rejected(self) -> None:
        self._expect("unsupported_kind", self._base(kind="other"))

    def test_invalid_json_rejected(self) -> None:
        self._expect("invalid_json", "{not json")

    def test_non_object_json_rejected(self) -> None:
        self._expect("invalid_object", "[1, 2, 3]")

    def test_missing_field_rejected(self) -> None:
        self._expect("unsupported_type", json.dumps({"v": 1, "event_seq": 1, "kind": "text"}))

    def test_empty_text_rejected(self) -> None:
        self._expect("invalid_text", self._base(text=""))

    def test_invalid_confidence_rejected(self) -> None:
        for value in (None, 1.5, -0.1, "x", True):
            with self.subTest(value=value):
                self._expect("invalid_confidence", self._base(confidence=value))

    def test_nan_inf_timestamp_rejected(self) -> None:
        for value in (float("nan"), float("inf"), -1.0):
            with self.subTest(value=value):
                line = json.dumps(
                    {
                        "v": 1,
                        "type": "stable_text",
                        "event_seq": 1,
                        "kind": "text",
                        "text": "hi",
                        "confidence": 0.9,
                        "source_seq": 3,
                        "timestamp_monotonic": value,
                    }
                )
                self._expect("invalid_timestamp", line)

    def test_invalid_source_seq_rejected(self) -> None:
        for value in (None, -1, "x", True):
            with self.subTest(value=value):
                self._expect("invalid_source_seq", self._base(source_seq=value))

    def test_invalid_event_seq_rejected(self) -> None:
        for value in (0, -1, "x", True):
            with self.subTest(value=value):
                self._expect("invalid_event_seq", self._base(event_seq=value))

    def test_malformed_clear_rejected(self) -> None:
        self._expect("invalid_text", self._base(kind="clear", text="not empty"))
        self._expect("invalid_confidence", self._base(kind="clear", text="", confidence=0.5))
        self._expect("invalid_source_seq", self._base(kind="clear", text="", confidence=None, source_seq=3))

    def test_valid_clear_accepted(self) -> None:
        decoded = decode_envelope(self._base(kind="clear", text="", confidence=None, source_seq=None))
        self.assertEqual(decoded.kind, "clear")

    def test_oversized_line_rejected(self) -> None:
        line = json.dumps(
            {
                "v": 1,
                "type": "stable_text",
                "event_seq": 1,
                "kind": "text",
                "text": "a" * (MAX_LINE_BYTES + 10),
                "confidence": 0.9,
                "source_seq": 1,
                "timestamp_monotonic": 1.0,
            }
        )
        self._expect("line_too_large", line)

    def test_non_string_line_rejected(self) -> None:
        self._expect("invalid_line", 123)


class EnvelopeFromEventTest(unittest.TestCase):
    def test_rejects_unknown_kind(self) -> None:
        event = StableTextEvent(kind="bogus", text="x", confidence=0.5, source_seq=1, timestamp_monotonic=1.0)
        with self.assertRaises(TransportError) as ctx:
            envelope_from_event(1, event)
        self.assertEqual(ctx.exception.code, "unsupported_kind")


if __name__ == "__main__":
    unittest.main(verbosity=2)
