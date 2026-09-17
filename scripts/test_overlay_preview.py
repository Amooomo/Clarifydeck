#!/usr/bin/env python3
"""Phase 2L.8.2 tests: explicit renderer-based region preview.

Protocol sanitization + OverlayManager ownership/preview independence. No X11,
no renderer process: spawn/socket/handshake are mocked.

Run:
    python3 scripts/test_overlay_preview.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from overlay import protocol  # noqa: E402


class FakeProc:
    def __init__(self) -> None:
        self.pid = 4242
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False


class FakeSock:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        pass

    def types(self) -> list[str]:
        return [json.loads(chunk.decode())["type"] for chunk in self.sent]


class ProtocolTest(unittest.TestCase):
    def test_r1_r2_sanitize_multiple(self) -> None:
        regions = protocol.sanitize_preview_regions(
            [
                {"region_id": "a", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1, "label": "Region 1"},
                {"region_id": "b", "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2},
            ]
        )
        self.assertEqual([r["region_id"] for r in regions], ["a", "b"])

    def test_r3_metadata_retained(self) -> None:
        region = protocol.sanitize_preview_regions(
            [{"region_id": "a", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1, "selected": True, "primary": True, "enabled": False, "label": "Primary · Region 1"}]
        )[0]
        self.assertTrue(region["selected"])
        self.assertTrue(region["primary"])
        self.assertFalse(region["enabled"])
        self.assertEqual(region["label"], "Primary · Region 1")

    def test_r8_invalid_payload_dropped(self) -> None:
        self.assertEqual(protocol.sanitize_preview_regions("nope"), [])
        self.assertEqual(protocol.sanitize_preview_regions(None), [])
        bad = [
            {"x": 5, "y": 0, "w": 0.1, "h": 0.1},
            {"x": 0.1, "y": 0.1, "w": 0, "h": 0.1},
            {"x": 0.9, "y": 0.1, "w": 0.2, "h": 0.1},
            {"x": float("nan"), "y": 0.1, "w": 0.1, "h": 0.1},
        ]
        self.assertEqual(protocol.sanitize_preview_regions(bad), [])
        mixed = [bad[0], {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}]
        self.assertEqual(len(protocol.sanitize_preview_regions(mixed)), 1)

    def test_r7_geometry_maps_to_window(self) -> None:
        rect = protocol.preview_pixel_rect({"x": 0.13, "y": 0.74, "w": 0.16, "h": 0.06}, 1280, 800)
        self.assertAlmostEqual(rect[0], 166.4)
        self.assertAlmostEqual(rect[1], 592.0)
        self.assertAlmostEqual(rect[2], 204.8)
        self.assertAlmostEqual(rect[3], 48.0)

    def test_bounded_region_count(self) -> None:
        many = [{"x": 0.0, "y": 0.0, "w": 0.1, "h": 0.1} for _ in range(protocol.MAX_PREVIEW_REGIONS + 5)]
        self.assertEqual(len(protocol.sanitize_preview_regions(many)), protocol.MAX_PREVIEW_REGIONS)


class ManagerPreviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["CLARIFYDECK_OVERLAY_DISPLAY"] = ":0"
        os.environ["CLARIFYDECK_OVERLAY_SOCKET"] = str(Path(self._tmp.name) / "overlay.sock")
        os.environ["CLARIFYDECK_OVERLAY_RUNTIME_DIR"] = self._tmp.name

        import overlay_manager

        self.om = overlay_manager
        self.spawn_calls = {"n": 0}
        self._orig = {
            "gamescope_ready": overlay_manager.gamescope_ready,
            "resolve_python3": overlay_manager.resolve_python3,
            "_spawn": overlay_manager.OverlayManager._spawn,
            "_check_python": overlay_manager.OverlayManager._check_python,
            "_build_command": overlay_manager.OverlayManager._build_command,
            "_verify_child": overlay_manager.OverlayManager._verify_child,
            "_wait_for_socket": overlay_manager.OverlayManager._wait_for_socket,
            "_connect": overlay_manager.OverlayManager._connect,
            "_handshake": overlay_manager.OverlayManager._handshake,
            "_ensure_runtime_dir": overlay_manager.OverlayManager._ensure_runtime_dir,
        }
        overlay_manager.gamescope_ready = lambda _display: True
        overlay_manager.resolve_python3 = lambda: "/usr/bin/python3"
        overlay_manager.OverlayManager._check_python = lambda _self, _python: None
        overlay_manager.OverlayManager._build_command = lambda _self, _python: ["/usr/bin/python3", "renderer.py"]
        overlay_manager.OverlayManager._verify_child = lambda _self: None
        overlay_manager.OverlayManager._wait_for_socket = lambda _self, _timeout: True
        overlay_manager.OverlayManager._connect = lambda manager: setattr(manager, "_sock", FakeSock())
        overlay_manager.OverlayManager._handshake = lambda _self: None
        overlay_manager.OverlayManager._ensure_runtime_dir = lambda _self: Path(self._tmp.name)

        def fake_spawn(manager, command):
            self.spawn_calls["n"] += 1
            manager._proc = FakeProc()

        overlay_manager.OverlayManager._spawn = fake_spawn

    def tearDown(self) -> None:
        self.om.gamescope_ready = self._orig["gamescope_ready"]
        self.om.resolve_python3 = self._orig["resolve_python3"]
        self.om.OverlayManager._spawn = self._orig["_spawn"]
        self.om.OverlayManager._check_python = self._orig["_check_python"]
        self.om.OverlayManager._build_command = self._orig["_build_command"]
        self.om.OverlayManager._verify_child = self._orig["_verify_child"]
        self.om.OverlayManager._wait_for_socket = self._orig["_wait_for_socket"]
        self.om.OverlayManager._connect = self._orig["_connect"]
        self.om.OverlayManager._handshake = self._orig["_handshake"]
        self.om.OverlayManager._ensure_runtime_dir = self._orig["_ensure_runtime_dir"]
        self._tmp.cleanup()

    def test_b1_preview_on_starts_renderer(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            result = await manager.set_region_preview_enabled(True)
            self.assertEqual(self.spawn_calls["n"], 1)
            self.assertEqual(result["state"], "RUNNING")
            self.assertTrue(result["preview_enabled"])
            self.assertFalse(result["enabled"])

        asyncio.run(run())

    def test_b2_preview_reuses_running_renderer(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_preview_enabled(True)
            self.assertEqual(self.spawn_calls["n"], 1)

        asyncio.run(run())

    def test_b3_preview_on_then_disable_keeps_renderer(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_preview_enabled(True)
            result = await manager.disable()
            self.assertEqual(result["state"], "RUNNING")
            self.assertFalse(result["enabled"])
            self.assertTrue(result["preview_enabled"])
            self.assertEqual(self.spawn_calls["n"], 1)

        asyncio.run(run())

    def test_b4_preview_only_then_disable_keeps_renderer(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.set_region_preview_enabled(True)
            result = await manager.disable()
            self.assertEqual(result["state"], "RUNNING")
            self.assertEqual(self.spawn_calls["n"], 1)

        asyncio.run(run())

    def test_b5_both_off_stops_renderer(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_preview_enabled(True)
            await manager.set_region_preview_enabled(False)
            result = await manager.disable()
            self.assertEqual(result["state"], "DISABLED")
            self.assertEqual(self.spawn_calls["n"], 1)

        asyncio.run(run())

    def test_b6_repeated_on_does_not_duplicate_child(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.set_region_preview_enabled(True)
            await manager.set_region_preview_enabled(True)
            await manager.set_region_preview_enabled(True)
            self.assertEqual(self.spawn_calls["n"], 1)  # one child, no duplicates
            await manager.set_region_preview_enabled(False)
            self.assertEqual(manager.status()["state"], "DISABLED")

        asyncio.run(run())

    def test_b7_no_autostart(self) -> None:
        manager = self.om.OverlayManager()
        self.assertEqual(self.spawn_calls["n"], 0)
        self.assertEqual(manager.status()["state"], "DISABLED")
        self.assertFalse(manager.status()["preview_enabled"])

    def test_r4_clear_preview_only(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_preview([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}])
            sock = manager._sock
            await manager.clear_region_preview()
            self.assertIn("clear_region_preview", sock.types())
            self.assertEqual(manager.status()["preview_region_count"], 0)

        asyncio.run(run())

    def test_r5_text_survives_preview_clear(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.update("hello")
            self.assertTrue(manager.status()["visible"])
            await manager.set_region_preview([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}])
            await manager.clear_region_preview()
            self.assertTrue(manager.status()["visible"])
            self.assertEqual(manager._last_text, "hello")

        asyncio.run(run())

    def test_r6_preview_survives_text_hide(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_preview_enabled(True)
            await manager.set_region_preview([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}])
            await manager.update("hello")
            await manager.hide()
            self.assertEqual(manager.status()["preview_region_count"], 1)
            self.assertIn("set_region_preview", manager._sock.types())

        asyncio.run(run())

    def test_preview_update_does_not_erase_text_command(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_preview_enabled(True)
            await manager.update("hello")
            await manager.set_region_preview([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}])
            types = manager._sock.types()
            self.assertIn("show", types)
            self.assertIn("set_region_preview", types)

        asyncio.run(run())

    def test_r1_r2_two_region_text_blocks_independent(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "a")
            await manager.set_region_text("B", (0.5, 0.5, 0.2, 0.2), "b")
            self.assertEqual(manager.status()["region_text_count"], 2)
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "a2")
            self.assertEqual(manager._region_text["A"]["text"], "a2")
            self.assertEqual(manager._region_text["B"]["text"], "b")

        asyncio.run(run())

    def test_r3_hide_a_leaves_b(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "a")
            await manager.set_region_text("B", (0.5, 0.5, 0.2, 0.2), "b")
            await manager.hide_region_text("A")
            self.assertEqual(manager.status()["region_text_count"], 1)
            self.assertEqual(manager._region_text["B"]["text"], "b")

        asyncio.run(run())

    def test_r4_clear_all_text_keeps_preview(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_preview_enabled(True)
            await manager.set_region_preview([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}])
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "a")
            await manager.clear_all_region_text()
            self.assertEqual(manager.status()["region_text_count"], 0)
            self.assertEqual(manager.status()["preview_region_count"], 1)

        asyncio.run(run())

    def test_r5_clear_preview_keeps_text(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_preview_enabled(True)
            await manager.set_region_preview([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}])
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "a")
            await manager.clear_region_preview()
            self.assertEqual(manager.status()["preview_region_count"], 0)
            self.assertEqual(manager.status()["region_text_count"], 1)

        asyncio.run(run())

    def test_l4_text_off_clears_blocks_keeps_preview(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_preview_enabled(True)
            await manager.set_region_preview([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}])
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "a")
            result = await manager.disable()
            self.assertEqual(result["state"], "RUNNING")
            self.assertEqual(result["region_text_count"], 0)
            self.assertEqual(result["preview_region_count"], 1)
            self.assertTrue(result["preview_enabled"])

        asyncio.run(run())

    def test_l5_preview_off_keeps_text(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_preview_enabled(True)
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "a")
            result = await manager.set_region_preview_enabled(False)
            self.assertEqual(result["state"], "RUNNING")
            self.assertEqual(result["preview_region_count"], 0)
            self.assertEqual(result["region_text_count"], 1)

        asyncio.run(run())

    def test_region_text_command_sent(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "a")
            self.assertIn("set_region_text", manager._sock.types())

        asyncio.run(run())

    def test_stop_tears_down_regardless_of_preview(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.set_region_preview_enabled(True)
            await manager.stop()
            self.assertEqual(manager.status()["state"], "DISABLED")
            self.assertFalse(manager.status()["preview_enabled"])

        asyncio.run(run())

    # -- Phase 2M.2A runtime per-region panel style ------------------------

    def test_style_default_is_white_on_black(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            result = await manager.get_region_panel_style("A")
            self.assertTrue(result["ok"])
            self.assertEqual(result["style"], "white_on_black")

        asyncio.run(run())

    def test_style_set_remembered(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            result = await manager.set_region_panel_style("A", "black_on_white")
            self.assertTrue(result["ok"])
            self.assertEqual(result["style"], "black_on_white")
            self.assertEqual((await manager.get_region_panel_style("A"))["style"], "black_on_white")

        asyncio.run(run())

    def test_style_invalid_rejected_preserves_previous(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.set_region_panel_style("A", "black_on_white")
            bad = await manager.set_region_panel_style("A", "neon")
            self.assertFalse(bad["ok"])
            self.assertEqual(bad["error"], "invalid_style")
            self.assertEqual((await manager.get_region_panel_style("A"))["style"], "black_on_white")

        asyncio.run(run())

    def test_style_change_does_not_start_renderer(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.set_region_panel_style("A", "black_on_white")
            self.assertEqual(self.spawn_calls["n"], 0)
            self.assertEqual(manager.status()["state"], "DISABLED")

        asyncio.run(run())

    def test_style_without_text_sends_nothing(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            manager._sock.sent.clear()
            await manager.set_region_panel_style("A", "black_on_white")
            self.assertEqual(manager._sock.types(), [])
            self.assertEqual(manager.status()["region_text_count"], 0)

        asyncio.run(run())

    def test_style_updates_visible_block_live(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "hi")
            manager._sock.sent.clear()
            result = await manager.set_region_panel_style("A", "black_on_white")
            self.assertTrue(result["ok"])
            self.assertEqual(manager._sock.types(), ["set_region_style"])
            payload = json.loads(manager._sock.sent[-1].decode())
            self.assertEqual(payload["region_id"], "A")
            self.assertEqual(payload["style"], "black_on_white")
            self.assertEqual(manager._region_text["A"]["style"], "black_on_white")

        asyncio.run(run())

    def test_set_region_text_includes_current_style(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_panel_style("A", "black_on_white")
            manager._sock.sent.clear()
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "hi")
            payload = json.loads(manager._sock.sent[-1].decode())
            self.assertEqual(payload["type"], "set_region_text")
            self.assertEqual(payload["style"], "black_on_white")

        asyncio.run(run())

    def test_styles_independent_between_regions(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "a")
            await manager.set_region_text("B", (0.5, 0.5, 0.2, 0.2), "b")
            await manager.set_region_panel_style("A", "black_on_white")
            self.assertEqual((await manager.get_region_panel_style("A"))["style"], "black_on_white")
            self.assertEqual((await manager.get_region_panel_style("B"))["style"], "white_on_black")
            self.assertEqual(manager._region_text["B"]["style"], "white_on_black")

        asyncio.run(run())

    def test_style_memory_survives_disable_within_lifetime(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_panel_style("A", "black_on_white")
            await manager.disable()
            self.assertEqual((await manager.get_region_panel_style("A"))["style"], "black_on_white")

        asyncio.run(run())

    def test_clear_all_keeps_style_memory(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "a")
            await manager.set_region_panel_style("A", "black_on_white")
            await manager.clear_all_region_text()
            self.assertEqual(manager.status()["region_text_count"], 0)
            self.assertEqual((await manager.get_region_panel_style("A"))["style"], "black_on_white")

        asyncio.run(run())

    def test_panel_and_preview_coexist(self) -> None:
        async def run():
            manager = self.om.OverlayManager()
            await manager.enable()
            await manager.set_region_preview_enabled(True)
            await manager.set_region_preview([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}])
            await manager.set_region_text("A", (0.1, 0.1, 0.2, 0.2), "a")
            self.assertEqual(manager.status()["region_text_count"], 1)
            self.assertEqual(manager.status()["preview_region_count"], 1)
            await manager.clear_all_region_text()
            self.assertEqual(manager.status()["region_text_count"], 0)
            self.assertEqual(manager.status()["preview_region_count"], 1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
