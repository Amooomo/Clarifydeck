#!/usr/bin/env python3
"""Phase 2C.2 tests: Wayland runtime-dir / display resolution + socket diagnostics.

Cross-platform: the runtime-dir resolver accepts an injected uid and run_root so
the fallback logic can be tested without /run/user.

Run:
    python3 scripts/test_wayland_env.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture import gamescope_capture as gc  # noqa: E402
from capture.errors import CaptureError  # noqa: E402


class RuntimeDirTest(unittest.TestCase):
    def test_xdg_runtime_dir_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_RUNTIME_DIR": tmp}
            self.assertEqual(gc.resolve_wayland_runtime_dir(uid=1234, env=env), tmp)

    def test_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CLARIFYDECK_WAYLAND_RUNTIME_DIR": tmp, "XDG_RUNTIME_DIR": "/nope"}
            self.assertEqual(gc.resolve_wayland_runtime_dir(uid=1234, env=env), tmp)

    def test_missing_xdg_falls_back_to_uid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "1234").mkdir()
            env: dict = {}
            self.assertEqual(
                gc.resolve_wayland_runtime_dir(uid=1234, env=env, run_root=tmp),
                str(Path(tmp) / "1234"),
            )

    def test_missing_xdg_and_uid_dir_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CaptureError) as ctx:
                gc.resolve_wayland_runtime_dir(uid=1234, env={}, run_root=tmp)
            self.assertEqual(ctx.exception.code, "wayland_runtime_dir_unavailable")

    def test_does_not_hardcode_1000(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "4321").mkdir()
            result = gc.resolve_wayland_runtime_dir(uid=4321, env={}, run_root=tmp)
            self.assertEqual(result, str(Path(tmp) / "4321"))
            self.assertNotIn("1000", result)


class DisplayTest(unittest.TestCase):
    def test_env_display(self) -> None:
        env = {"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-7"}
        self.assertEqual(gc.resolve_gamescope_display(env=env), "gamescope-7")

    def test_override_when_env_missing(self) -> None:
        self.assertEqual(gc.resolve_gamescope_display("gamescope-3", env={}), "gamescope-3")

    def test_wayland_display_gamescope(self) -> None:
        env = {"WAYLAND_DISPLAY": "gamescope-1"}
        self.assertEqual(gc.resolve_gamescope_display(env=env), "gamescope-1")

    def test_fallback(self) -> None:
        self.assertEqual(gc.resolve_gamescope_display(env={}), "gamescope-0")

    def test_does_not_use_plain_wayland(self) -> None:
        env = {"WAYLAND_DISPLAY": "wayland-0"}
        self.assertEqual(gc.resolve_gamescope_display(env=env), "gamescope-0")


class FakeLib:
    def wl_display_connect_to_fd(self, _fd):
        return 0


class ConnectDiagnosticsTest(unittest.TestCase):
    def _client(self, runtime: str):
        client = gc._WaylandClient("gamescope-0", lambda _m: None, runtime_dir=runtime)
        client.lib = FakeLib()
        return client

    def test_missing_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            with self.assertRaises(CaptureError) as ctx:
                client._connect_display()
            self.assertEqual(ctx.exception.code, "gamescope_wayland_socket_missing")

    def test_regular_file_is_not_a_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "gamescope-0").write_text("not a socket")
            client = self._client(tmp)
            with self.assertRaises(CaptureError) as ctx:
                client._connect_display()
            self.assertEqual(ctx.exception.code, "gamescope_wayland_socket_missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
