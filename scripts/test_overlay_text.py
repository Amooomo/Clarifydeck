#!/usr/bin/env python3
"""Phase 2K.1 tests: accepted StableTextEvent -> overlay action foundation.

Pure-stdlib deterministic tests. No renderer, no asyncio, no sockets.

Run:
    python3 scripts/test_overlay_text.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.ocr_transport import AcceptedStableTextEvent  # noqa: E402
from backend.overlay_text import (  # noqa: E402
    OverlayTextCoordinator,
    OverlayTextTransportObserver,
)

FORBIDDEN_SOURCE_IMPORTS = (
    "overlay_manager",
    "overlay.renderer",
    "asyncio",
    "socket",
    "subprocess",
    "decky",
)


def make_event(
    session: str = "A",
    seq: int = 1,
    kind: str = "text",
    text: str = "hello",
    confidence: float | None = 0.9,
    source_seq: int | None = 3,
    timestamp: float | None = 1.0,
) -> AcceptedStableTextEvent:
    return AcceptedStableTextEvent(
        worker_session_id=session,
        event_seq=seq,
        kind=kind,
        text=text,
        confidence=confidence,
        source_seq=source_seq,
        timestamp_monotonic=timestamp,
    )


class TextActionTest(unittest.TestCase):
    def test_k1_accepted_text_creates_exact_action(self) -> None:
        coordinator = OverlayTextCoordinator()
        coordinator.begin_session("A")

        action = coordinator.consume(make_event(seq=1, text="hello"))

        self.assertIsNotNone(action)
        self.assertEqual(action.action_seq, 1)
        self.assertEqual(action.kind, "text")
        self.assertEqual(action.text, "hello")
        self.assertEqual(action.worker_session_id, "A")
        self.assertEqual(action.source_event_seq, 1)
        self.assertEqual(coordinator.latest_action(), action)

    def test_k7_identical_text_distinct_identity(self) -> None:
        coordinator = OverlayTextCoordinator()
        coordinator.begin_session("A")

        first = coordinator.consume(make_event(seq=1, text="same"))
        second = coordinator.consume(make_event(seq=2, text="same"))

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.action_seq, 1)
        self.assertEqual(second.action_seq, 2)
        self.assertEqual(coordinator.status()["text_actions"], 2)

    def test_k8_exact_whitespace_multiline_cjk_preserved(self) -> None:
        coordinator = OverlayTextCoordinator()
        coordinator.begin_session("A")

        source = "  前导空格\n第二行！  trailing  "
        action = coordinator.consume(make_event(seq=1, text=source))

        self.assertEqual(action.text, source)


class ClearActionTest(unittest.TestCase):
    def test_k2_clear_maps_to_hide(self) -> None:
        coordinator = OverlayTextCoordinator()
        coordinator.begin_session("A")

        action = coordinator.consume(make_event(seq=1, kind="clear", text=""))

        self.assertEqual(action.kind, "hide")
        self.assertEqual(action.text, "")
        self.assertEqual(action.action_seq, 1)


class OrderingTest(unittest.TestCase):
    def test_k3_duplicate_event_rejected(self) -> None:
        coordinator = OverlayTextCoordinator()
        coordinator.begin_session("A")

        coordinator.consume(make_event(seq=1, text="first"))
        second = coordinator.consume(make_event(seq=1, text="second"))

        self.assertIsNone(second)
        self.assertEqual(coordinator.latest_action().text, "first")
        self.assertEqual(coordinator.status()["action_seq"], 1)
        self.assertEqual(coordinator.status()["inputs_rejected"], 1)

    def test_k4_out_of_order_rejected(self) -> None:
        coordinator = OverlayTextCoordinator()
        coordinator.begin_session("A")

        coordinator.consume(make_event(seq=2, text="second"))
        stale = coordinator.consume(make_event(seq=1, text="first"))

        self.assertIsNone(stale)
        self.assertEqual(coordinator.latest_action().source_event_seq, 2)
        self.assertEqual(coordinator.status()["action_seq"], 1)

    def test_k5_wrong_session_rejected(self) -> None:
        coordinator = OverlayTextCoordinator()
        coordinator.begin_session("A")

        action = coordinator.consume(make_event(session="B", seq=1))

        self.assertIsNone(action)
        self.assertIsNone(coordinator.latest_action())
        self.assertEqual(coordinator.status()["last_error"], "session_mismatch")

    def test_consume_before_session_rejected(self) -> None:
        coordinator = OverlayTextCoordinator()
        self.assertIsNone(coordinator.consume(make_event(seq=1)))
        self.assertEqual(coordinator.status()["last_error"], "no_session")


class SessionTest(unittest.TestCase):
    def test_k6_new_session_resets_authority(self) -> None:
        coordinator = OverlayTextCoordinator()
        coordinator.begin_session("A")
        coordinator.consume(make_event(session="A", seq=9, text="a"))

        coordinator.begin_session("B")
        self.assertIsNone(coordinator.latest_action())
        self.assertEqual(coordinator.status()["action_seq"], 0)
        self.assertEqual(coordinator.status()["last_source_event_seq"], 0)

        action = coordinator.consume(make_event(session="B", seq=1, text="b"))
        self.assertEqual(action.action_seq, 1)
        self.assertEqual(action.worker_session_id, "B")

        stale = coordinator.consume(make_event(session="A", seq=10, text="old"))
        self.assertIsNone(stale)

    def test_begin_session_emits_no_action(self) -> None:
        coordinator = OverlayTextCoordinator()
        coordinator.begin_session("A")
        coordinator.consume(make_event(seq=1))
        coordinator.begin_session("B")
        # begin_session must not emit a hide action in Phase 2K.1
        self.assertIsNone(coordinator.latest_action())


class IdentityTest(unittest.TestCase):
    def test_k15_event_seq_is_overlay_identity_not_source_seq(self) -> None:
        coordinator = OverlayTextCoordinator()
        coordinator.begin_session("A")

        action = coordinator.consume(make_event(seq=7, source_seq=100))

        self.assertEqual(action.source_event_seq, 7)
        self.assertEqual(action.source_seq, 100)


class ObserverAdapterTest(unittest.TestCase):
    def test_observer_routes_session_and_events(self) -> None:
        coordinator = OverlayTextCoordinator()
        observer = OverlayTextTransportObserver(coordinator)

        observer.begin_session("A")
        observer.on_accepted_event(make_event(session="A", seq=1, text="via observer"))

        self.assertEqual(coordinator.session_id, "A")
        self.assertEqual(coordinator.latest_action().text, "via observer")


class IsolationTest(unittest.TestCase):
    def test_k16_no_renderer_or_async_side_effects(self) -> None:
        prohibited = (
            "overlay_manager",
            "overlay.renderer",
            "asyncio",
            "socket",
            "decky",
            "cairo",
            "Xlib",
            "rapidocr",
            "onnxruntime",
            "numpy",
            "cv2",
            "omegaconf",
            "antlr4",
        )
        code = (
            "import sys; import backend.overlay_text; import backend.ocr_transport; "
            f"bad=[m for m in {prohibited!r} if m in sys.modules]; "
            "print('BAD' if bad else 'OK', bad)"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertIn("OK", result.stdout, msg=result.stdout + result.stderr)

    def test_overlay_text_source_has_no_forbidden_imports(self) -> None:
        source = (ROOT / "backend" / "overlay_text.py").read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_SOURCE_IMPORTS:
            pattern = re.compile(rf"^\s*(?:import|from)\s+{re.escape(forbidden)}\b", re.MULTILINE)
            self.assertIsNone(pattern.search(source), msg=f"overlay_text imports {forbidden}")

    def test_ocr_transport_does_not_import_overlay_text(self) -> None:
        source = (ROOT / "backend" / "ocr_transport.py").read_text(encoding="utf-8")
        self.assertNotIn("overlay_text", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
