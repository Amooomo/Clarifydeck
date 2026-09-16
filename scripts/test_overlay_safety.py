#!/usr/bin/env python3
"""Phase 1C.2 recovery-safe overlay tests.

Static/unit tests only. They never touch X11 or Gamescope: the renderer spawn,
socket wait, connect and handshake steps are mocked.

Run:
    python3 scripts/test_overlay_safety.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend_leader  # noqa: E402


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


class LeaderLockTest(unittest.TestCase):
    @unittest.skipIf(backend_leader.fcntl is None, "flock not available on this platform")
    def test_second_lease_is_busy_then_acquires_after_release(self) -> None:
        from backend_leader import BackgroundLeaderLease, LeaderAcquireResult

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clarifydeck" / "backend-leader.lock"
            first = BackgroundLeaderLease(path)
            second = BackgroundLeaderLease(path)
            self.assertEqual(first.try_acquire(), LeaderAcquireResult.ACQUIRED)
            self.assertEqual(second.try_acquire(), LeaderAcquireResult.BUSY)
            first.release()
            self.assertEqual(second.try_acquire(), LeaderAcquireResult.ACQUIRED)
            second.release()

    @unittest.skipIf(backend_leader.fcntl is None, "flock not available on this platform")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root bypasses permissions")
    def test_permission_error_is_error_not_busy(self) -> None:
        from backend_leader import BackgroundLeaderLease, LeaderAcquireResult

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "readonly"
            parent.mkdir()
            os.chmod(parent, 0o500)  # read+execute, no write
            try:
                lease = BackgroundLeaderLease(parent / "clarifydeck" / "backend-leader.lock")
                result = lease.try_acquire()
                self.assertEqual(result, LeaderAcquireResult.ERROR)
                self.assertEqual(lease.last_stage, "mkdir")
            finally:
                os.chmod(parent, 0o700)


class RendererCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["CLARIFYDECK_OVERLAY_DISPLAY"] = ":0"
        os.environ["CLARIFYDECK_OVERLAY_SOCKET"] = str(Path(tempfile.gettempdir()) / "cd-test.sock")
        import overlay_manager

        self.om = overlay_manager

    def test_forbidden_interpreter_rejected(self) -> None:
        manager = self.om.OverlayManager()
        with self.assertRaises(self.om.OverlayError):
            manager._build_command("/home/deck/homebrew/services/PluginLoader")

    def test_frozen_runtime_must_not_use_sys_executable(self) -> None:
        manager = self.om.OverlayManager()
        old_frozen = getattr(sys, "frozen", None)
        old_executable = sys.executable
        try:
            sys.frozen = True  # type: ignore[attr-defined]
            sys.executable = "/usr/bin/python3"
            with self.assertRaises(self.om.OverlayError):
                manager._build_command("/usr/bin/python3")
        finally:
            sys.executable = old_executable
            if old_frozen is None:
                try:
                    del sys.frozen  # type: ignore[attr-defined]
                except AttributeError:
                    pass
            else:
                sys.frozen = old_frozen  # type: ignore[attr-defined]

    def test_command_uses_given_python_and_renderer_path(self) -> None:
        manager = self.om.OverlayManager()
        command = manager._build_command("/usr/bin/python3")
        self.assertEqual(command[0], "/usr/bin/python3")
        self.assertIn("renderer.py", command[1])
        self.assertNotEqual(command[0], sys.executable)


class OverlayManagerTest(unittest.TestCase):
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

    def test_concurrent_enable_spawns_once(self) -> None:
        async def run() -> None:
            manager = self.om.OverlayManager()
            await asyncio.gather(manager.enable(), manager.enable(), manager.enable())
            self.assertEqual(self.spawn_calls["n"], 1)
            self.assertEqual(manager.status()["state"], "RUNNING")
            self.assertTrue(manager.status()["enabled"])

        asyncio.run(run())

    def test_concurrent_disable_idempotent(self) -> None:
        async def run() -> None:
            manager = self.om.OverlayManager()
            await manager.enable()
            await asyncio.gather(manager.disable(), manager.disable())
            self.assertEqual(manager.status()["state"], "DISABLED")
            self.assertFalse(manager.status()["enabled"])
            self.assertIsNone(manager.status()["renderer_pid"])

        asyncio.run(run())

    def test_update_is_noop_when_disabled(self) -> None:
        async def run() -> None:
            manager = self.om.OverlayManager()
            await manager.update("hello")
            await manager.hide()
            self.assertEqual(self.spawn_calls["n"], 0)
            self.assertEqual(manager.status()["state"], "DISABLED")

        asyncio.run(run())

    def test_enable_failure_is_fail_closed(self) -> None:
        async def run() -> None:
            self.om.gamescope_ready = lambda _display: False
            manager = self.om.OverlayManager()
            result = await manager.enable()
            self.assertFalse(result["enabled"])
            self.assertEqual(result["state"], "FAILED")
            self.assertEqual(result["last_error"], "gamescope_not_ready")
            self.assertEqual(self.spawn_calls["n"], 0)

        asyncio.run(run())

    def test_wrong_handshake_is_fail_closed(self) -> None:
        async def run() -> None:
            def bad_handshake(_self):
                raise self.om.OverlayError("renderer_identity_mismatch")

            self.om.OverlayManager._handshake = bad_handshake
            manager = self.om.OverlayManager()
            result = await manager.enable()
            self.assertFalse(result["enabled"])
            self.assertEqual(result["state"], "FAILED")
            self.assertEqual(result["last_error"], "renderer_identity_mismatch")

        asyncio.run(run())


class FrontendBootSafetyTest(unittest.TestCase):
    """Static checks: no legacy/debug frontend overlay paths, no auto-enable."""

    def test_frontend_has_no_debug_probe_or_auto_enable(self) -> None:
        src = (ROOT / "src" / "index.tsx").read_text(encoding="utf-8")
        # Post-2I.3 Cleanup C2: the hard-false legacy React subtitle, notification
        # keepalive, and debug-probe paths were removed outright (not re-flagged).
        for removed in (
            "ENABLE_DEBUG_PROBES",
            "ENABLE_LEGACY_SUBTITLE_OVERLAY",
            "ENABLE_NOTIFICATION_KEEPALIVE",
            "mountOverlayKeepAlive",
            "CD raw",
            "CD probe",
            "CD overlay",
        ):
            self.assertNotIn(removed, src)
        self.assertNotIn("localStorage", src)
        # The only place the overlay is enabled is the explicit QAM toggle.
        self.assertEqual(src.count("setOverlayEnabled("), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
