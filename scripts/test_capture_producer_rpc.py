#!/usr/bin/env python3
"""Phase 2C.1 tests: live-backend RPC harness (mocked transport).

Run:
    python3 scripts/test_capture_producer_rpc.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_cli():
    path = ROOT / "scripts" / "capture_producer_rpc_test.py"
    spec = importlib.util.spec_from_file_location("capture_producer_rpc_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class RpcHarnessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def _run(self, argv, rpc) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return self.cli.main(argv, rpc=rpc)

    def test_status_success(self) -> None:
        calls = []

        def rpc(method, *args):
            calls.append((method, args))
            return {"ok": True, "state": "STOPPED"}

        self.assertEqual(self._run(["status"], rpc), 0)
        self.assertEqual(calls[0][0], "capture_producer_status")

    def test_start_success(self) -> None:
        calls = []

        def rpc(method, *args):
            calls.append((method, args))
            return {"ok": True, "state": "RUNNING", "target_fps": 1.0}

        self.assertEqual(self._run(["start", "--fps", "1"], rpc), 0)
        self.assertEqual(calls[0], ("capture_producer_start", (1.0,)))

    def test_stop_success(self) -> None:
        def rpc(method, *args):
            return {"ok": True, "state": "STOPPED"}

        self.assertEqual(self._run(["stop"], rpc), 0)

    def test_backend_ok_false(self) -> None:
        def rpc(method, *args):
            return {"ok": False, "state": "unavailable", "error": "not_leader"}

        self.assertEqual(self._run(["start"], rpc), 1)

    def test_transport_unavailable(self) -> None:
        def rpc(method, *args):
            raise self.cli.TransportError("cannot fetch auth token")

        self.assertEqual(self._run(["status"], rpc), 2)

    def test_backend_error_is_nonzero(self) -> None:
        def rpc(method, *args):
            raise self.cli.BackendError({"name": "RouteNotFoundError", "error": "no route"})

        self.assertEqual(self._run(["status"], rpc), 1)

    def test_bounded_timeout_on_run(self) -> None:
        def rpc(method, *args):
            if method == "capture_producer_start":
                return {"ok": True, "state": "RUNNING"}
            if method == "capture_producer_stop":
                return {"ok": True, "state": "RUNNING"}  # never reaches STOPPED
            return {"ok": True, "state": "RUNNING"}

        self.assertEqual(self._run(["run", "--duration-sec", "0.1"], rpc), 3)

    def test_run_success(self) -> None:
        def rpc(method, *args):
            if method == "capture_producer_start":
                return {"ok": True, "state": "RUNNING"}
            return {"ok": True, "state": "STOPPED"}

        self.assertEqual(self._run(["run", "--duration-sec", "0.1"], rpc), 0)


class NoBackendConstructionTest(unittest.TestCase):
    def test_harness_does_not_construct_backend(self) -> None:
        src = (ROOT / "scripts" / "capture_producer_rpc_test.py").read_text(encoding="utf-8")
        self.assertNotIn("import main", src)
        self.assertNotIn("from main", src)
        self.assertNotIn("ClarifyDeckEngine", src)
        self.assertNotIn("Plugin(", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
