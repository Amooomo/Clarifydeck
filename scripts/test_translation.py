#!/usr/bin/env python3
"""Phase 2J.1 tests: provider-agnostic translation foundation.

Deterministic, pure-stdlib tests. No network, no provider SDK, no OCR runtime.

Run:
    python3 scripts/test_translation.py
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.translation import (  # noqa: E402
    TranslationCoordinator,
    TranslationError,
    TranslationInput,
    TranslationResult,
    validate_translation_input,
)

HEAVY_MODULES = (
    "rapidocr",
    "onnxruntime",
    "numpy",
    "cv2",
    "omegaconf",
    "antlr4",
    "requests",
    "httpx",
)


class FakeProvider:
    """Deterministic test-only provider with call recording and failure control."""

    def __init__(self) -> None:
        self.calls: list = []
        self.fail = False
        self.result_text = None

    @property
    def name(self) -> str:
        return "fake"

    def translate(self, text, *, source_language, target_language):
        self.calls.append(
            {
                "text": text,
                "source_language": source_language,
                "target_language": target_language,
            }
        )
        if self.fail:
            raise RuntimeError("provider boom")
        translated = self.result_text if self.result_text is not None else f"[{target_language}] {text}"
        return TranslationResult(translated_text=translated, provider_name=self.name)


def make_input(
    session: str = "s1",
    seq: int = 1,
    kind: str = "text",
    text: str = "hello",
    target: str = "zh",
    source_language=None,
    confidence: float | None = 0.9,
    source_seq: int | None = 7,
    timestamp: float | None = 12.5,
) -> TranslationInput:
    return TranslationInput(
        worker_session_id=session,
        source_event_seq=seq,
        kind=kind,
        text=text,
        confidence=confidence,
        source_seq=source_seq,
        timestamp_monotonic=timestamp,
        source_language=source_language,
        target_language=target,
    )


class TextEventTest(unittest.TestCase):
    def test_t1_text_event_success(self) -> None:
        provider = FakeProvider()
        coordinator = TranslationCoordinator(provider)
        coordinator.begin_session("s1")

        outcome = coordinator.consume(make_input(seq=1, text="hello", target="zh"))

        self.assertTrue(outcome.accepted)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["target_language"], "zh")
        event = coordinator.latest_event()
        self.assertIsNotNone(event)
        self.assertEqual(event.translation_event_seq, 1)
        self.assertEqual(event.kind, "text")
        self.assertEqual(event.worker_session_id, "s1")
        self.assertEqual(event.source_event_seq, 1)
        self.assertEqual(event.source_text, "hello")
        self.assertEqual(event.translated_text, "[zh] hello")
        self.assertEqual(event.provider_name, "fake")
        self.assertEqual(event.source_confidence, 0.9)
        self.assertEqual(event.source_seq, 7)
        self.assertEqual(event.source_timestamp_monotonic, 12.5)

    def test_t9_identical_text_distinct_identity_not_deduplicated(self) -> None:
        provider = FakeProvider()
        coordinator = TranslationCoordinator(provider)
        coordinator.begin_session("s1")

        coordinator.consume(make_input(seq=1, text="same"))
        coordinator.consume(make_input(seq=2, text="same"))

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(coordinator.status()["translation_event_seq"], 2)
        self.assertEqual(coordinator.latest_event().source_event_seq, 2)

    def test_t8_exact_text_preservation(self) -> None:
        provider = FakeProvider()
        provider.result_text = "  [zh]  多行\n第二行!  "
        coordinator = TranslationCoordinator(provider)
        coordinator.begin_session("s1")

        source = "  hello  \n世界! "
        outcome = coordinator.consume(make_input(seq=1, text=source))

        self.assertTrue(outcome.accepted)
        event = coordinator.latest_event()
        self.assertEqual(event.source_text, source)
        self.assertEqual(event.translated_text, "  [zh]  多行\n第二行!  ")


class ClearEventTest(unittest.TestCase):
    def test_t2_clear_bypasses_provider(self) -> None:
        provider = FakeProvider()
        coordinator = TranslationCoordinator(provider)
        coordinator.begin_session("s1")

        outcome = coordinator.consume(make_input(seq=1, kind="clear", text=""))

        self.assertTrue(outcome.accepted)
        self.assertEqual(len(provider.calls), 0)
        event = coordinator.latest_event()
        self.assertEqual(event.kind, "clear")
        self.assertEqual(event.translated_text, "")
        self.assertEqual(event.source_text, "")
        self.assertIsNone(event.provider_name)
        self.assertIsNone(event.detected_source_language)
        self.assertEqual(event.translation_event_seq, 1)


class OrderingTest(unittest.TestCase):
    def test_t3_duplicate_source_seq_rejected(self) -> None:
        provider = FakeProvider()
        coordinator = TranslationCoordinator(provider)
        coordinator.begin_session("s1")

        coordinator.consume(make_input(seq=1, text="first"))
        outcome = coordinator.consume(make_input(seq=1, text="second"))

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "stale_or_duplicate")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(coordinator.latest_event().source_text, "first")
        self.assertEqual(coordinator.status()["translation_event_seq"], 1)

    def test_t4_out_of_order_rejected(self) -> None:
        provider = FakeProvider()
        coordinator = TranslationCoordinator(provider)
        coordinator.begin_session("s1")

        coordinator.consume(make_input(seq=2, text="second"))
        outcome = coordinator.consume(make_input(seq=1, text="first"))

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "stale_or_duplicate")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(coordinator.latest_event().source_event_seq, 2)
        self.assertEqual(coordinator.status()["translation_event_seq"], 1)


class SessionTest(unittest.TestCase):
    def test_t5_session_mismatch_rejected(self) -> None:
        provider = FakeProvider()
        coordinator = TranslationCoordinator(provider)
        coordinator.begin_session("A")

        outcome = coordinator.consume(make_input(session="B", seq=1))

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "session_mismatch")
        self.assertEqual(len(provider.calls), 0)
        self.assertIsNone(coordinator.latest_event())

    def test_t6_new_session_resets_authority(self) -> None:
        provider = FakeProvider()
        coordinator = TranslationCoordinator(provider)
        coordinator.begin_session("A")
        coordinator.consume(make_input(session="A", seq=5, text="a"))
        self.assertIsNotNone(coordinator.latest_event())

        coordinator.begin_session("B")
        self.assertIsNone(coordinator.latest_event())
        self.assertEqual(coordinator.status()["translation_event_seq"], 0)
        self.assertEqual(coordinator.status()["last_committed_source_event_seq"], 0)

        coordinator.consume(make_input(session="B", seq=1, text="b"))
        self.assertEqual(coordinator.status()["translation_event_seq"], 1)
        self.assertEqual(coordinator.latest_event().worker_session_id, "B")

        stale = coordinator.consume(make_input(session="A", seq=9, text="old"))
        self.assertFalse(stale.accepted)
        self.assertEqual(stale.reason, "session_mismatch")
        self.assertEqual(coordinator.status()["translation_event_seq"], 1)

    def test_consume_before_session_rejected(self) -> None:
        provider = FakeProvider()
        coordinator = TranslationCoordinator(provider)
        outcome = coordinator.consume(make_input(seq=1))
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "no_session")
        self.assertEqual(len(provider.calls), 0)


class FailureTest(unittest.TestCase):
    def test_t7_provider_failure_preserves_last_good_state(self) -> None:
        provider = FakeProvider()
        coordinator = TranslationCoordinator(provider)
        coordinator.begin_session("s1")

        coordinator.consume(make_input(seq=1, text="good"))
        first_event = coordinator.latest_event()

        provider.fail = True
        outcome = coordinator.consume(make_input(seq=2, text="bad"))

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "provider_error")
        self.assertIs(coordinator.latest_event(), first_event)
        self.assertEqual(coordinator.status()["translation_event_seq"], 1)
        self.assertEqual(coordinator.status()["last_committed_source_event_seq"], 1)
        self.assertEqual(coordinator.status()["provider_errors"], 1)
        self.assertIn("provider_error", coordinator.status()["last_error"])

    def test_failed_seq_not_committed_allows_explicit_retry(self) -> None:
        provider = FakeProvider()
        coordinator = TranslationCoordinator(provider)
        coordinator.begin_session("s1")

        coordinator.consume(make_input(seq=1, text="good"))
        provider.fail = True
        coordinator.consume(make_input(seq=2, text="retry-me"))
        self.assertEqual(coordinator.status()["last_committed_source_event_seq"], 1)
        self.assertEqual(coordinator.status()["last_seen_source_event_seq"], 2)

        provider.fail = False
        retry = coordinator.consume(make_input(seq=2, text="retry-me"))
        self.assertTrue(retry.accepted)
        self.assertEqual(coordinator.status()["last_committed_source_event_seq"], 2)
        self.assertEqual(coordinator.latest_event().source_text, "retry-me")


class ValidationTest(unittest.TestCase):
    def test_text_requires_non_empty(self) -> None:
        with self.assertRaises(TranslationError):
            validate_translation_input(make_input(kind="text", text=""))

    def test_clear_requires_empty_text(self) -> None:
        with self.assertRaises(TranslationError):
            validate_translation_input(make_input(kind="clear", text="nope"))

    def test_target_language_required(self) -> None:
        with self.assertRaises(TranslationError):
            validate_translation_input(make_input(target=""))

    def test_event_seq_must_be_positive(self) -> None:
        with self.assertRaises(TranslationError):
            validate_translation_input(make_input(seq=0))

    def test_session_id_required(self) -> None:
        with self.assertRaises(TranslationError):
            validate_translation_input(make_input(session=""))

    def test_invalid_input_rejected_without_provider_call(self) -> None:
        provider = FakeProvider()
        coordinator = TranslationCoordinator(provider)
        coordinator.begin_session("s1")
        outcome = coordinator.consume(make_input(seq=1, text=""))
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "invalid")
        self.assertEqual(len(provider.calls), 0)


class ImportSafetyTest(unittest.TestCase):
    def test_t10_translation_import_no_heavy_or_network_deps(self) -> None:
        code = (
            "import sys; import backend.translation; "
            f"bad=[m for m in {HEAVY_MODULES!r} if m in sys.modules]; "
            "print('BAD' if bad else 'OK', bad)"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertIn("OK", result.stdout, msg=result.stdout + result.stderr)
        self.assertNotIn("BAD", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
