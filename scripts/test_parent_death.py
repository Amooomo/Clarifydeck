#!/usr/bin/env python3
"""Phase 2I.2.2 tests: parent-death / orphan-worker safety.

Includes a real Linux subprocess test: a parent spawns a child with
PR_SET_PDEATHSIG armed, then abruptly ``os._exit(0)``s; the supervising test
verifies the child disappears on its own (no manager stop, no manual kill).

Run:
    python3 scripts/test_parent_death.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import parent_death as pd  # noqa: E402

NATIVE_MODULES = ("rapidocr", "onnxruntime", "numpy", "cv2", "omegaconf", "antlr4")
LINUX = sys.platform.startswith("linux")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.path.exists(f"/proc/{pid}"):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ParentDeathHelperTest(unittest.TestCase):
    def test_expected_parent_le_one_rejected(self) -> None:
        for bad in (0, 1, -5):
            with self.subTest(value=bad):
                with self.assertRaises(pd.ParentDeathError) as ctx:
                    pd.setup_parent_death(bad, arm=lambda sig: (True, None))
                self.assertEqual(ctx.exception.code, "invalid_parent_pid")

    def test_none_parent_skips_arming(self) -> None:
        result = pd.setup_parent_death(None)
        self.assertFalse(result["armed"])
        self.assertEqual(result["reason"], "no_parent_pid")

    def test_mismatched_getppid_rejected(self) -> None:
        with self.assertRaises(pd.ParentDeathError) as ctx:
            pd.setup_parent_death(1234, getppid=lambda: 1, arm=lambda sig: (True, None))
        self.assertEqual(ctx.exception.code, "parent_changed")

    def test_matching_parent_arms(self) -> None:
        result = pd.setup_parent_death(1234, getppid=lambda: 1234, arm=lambda sig: (True, None))
        self.assertTrue(result["armed"])
        self.assertIsNone(result["reason"])

    def test_arm_failure_surfaced(self) -> None:
        result = pd.setup_parent_death(1234, getppid=lambda: 1234, arm=lambda sig: (False, "prctl_unavailable"))
        self.assertFalse(result["armed"])
        self.assertEqual(result["reason"], "prctl_unavailable")

    def test_parent_changed_uses_relationship(self) -> None:
        self.assertTrue(pd.parent_changed(5, getppid=lambda: 1))
        self.assertFalse(pd.parent_changed(5, getppid=lambda: 5))

    def test_arm_signal_is_explicit(self) -> None:
        calls = []
        pd.setup_parent_death(1234, signal.SIGINT, getppid=lambda: 1234, arm=lambda sig: (calls.append(sig), (True, None))[1])
        self.assertEqual(calls, [signal.SIGINT])

    def test_arm_on_current_platform_returns_structured(self) -> None:
        armed, reason = pd.arm_parent_death_signal(signal.SIGINT)
        self.assertIsInstance(armed, bool)
        if not armed:
            self.assertIsInstance(reason, str)


class WorkerStartupOrderingTest(unittest.TestCase):
    def test_parent_death_armed_before_ocr_init(self) -> None:
        source = (ROOT / "scripts" / "ocr_worker.py").read_text(encoding="utf-8")
        main_start = source.index("def main(")
        body = source[main_start:]
        self.assertLess(body.index("setup_parent_death"), body.index("activate_plugin_ocr_runtime"))
        self.assertLess(body.index("setup_parent_death"), body.index("OCRRuntime("))

    def test_watchdog_uses_parent_relationship(self) -> None:
        source = (ROOT / "scripts" / "ocr_worker.py").read_text(encoding="utf-8")
        self.assertIn("parent_death.parent_changed", source)
        self.assertNotIn("_pid_alive", source)


@unittest.skipUnless(LINUX, "linux-only real subprocess test")
class ParentDeathSubprocessTest(unittest.TestCase):
    def test_abrupt_parent_exit_terminates_child(self) -> None:
        workdir = Path(tempfile.mkdtemp(prefix="clarifydeck-pdeath-"))
        ready_file = workdir / "child.pid"

        child_script = workdir / "child.py"
        child_script.write_text(
            "import os, sys, time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from backend import parent_death\n"
            "expected = int(sys.argv[1])\n"
            "ready = sys.argv[2]\n"
            "parent_death.setup_parent_death(expected, __import__('signal').SIGINT)\n"
            "open(ready, 'w').write(str(os.getpid()))\n"
            "while True:\n"
            "    time.sleep(0.2)\n",
            encoding="utf-8",
        )
        parent_script = workdir / "parent.py"
        parent_script.write_text(
            "import os, subprocess, sys, time\n"
            f"child = {str(child_script)!r}\n"
            f"ready = {str(ready_file)!r}\n"
            "proc = subprocess.Popen([sys.executable, child, str(os.getpid()), ready])\n"
            "for _ in range(100):\n"
            "    if os.path.exists(ready):\n"
            "        break\n"
            "    time.sleep(0.05)\n"
            "os._exit(0)\n",
            encoding="utf-8",
        )

        parent = subprocess.Popen([sys.executable, str(parent_script)])
        child_pid = None
        try:
            for _ in range(100):
                if ready_file.exists():
                    child_pid = int(ready_file.read_text(encoding="utf-8").strip())
                    break
                time.sleep(0.05)
            self.assertIsNotNone(child_pid, "child never reported readiness")
            self.assertTrue(_pid_alive(child_pid))

            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline and _pid_alive(child_pid):
                time.sleep(0.1)
            self.assertFalse(_pid_alive(child_pid), f"orphan child still alive pid={child_pid}")
        finally:
            if parent.poll() is None:
                parent.kill()
            try:
                parent.wait(timeout=2)
            except Exception:
                pass
            if child_pid and _pid_alive(child_pid):
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except Exception:
                    pass


class ImportSafetyTest(unittest.TestCase):
    def test_parent_death_imports_no_native_deps(self) -> None:
        code = (
            "import sys; import backend.parent_death; "
            f"bad=[m for m in {NATIVE_MODULES!r} if m in sys.modules]; print('BAD' if bad else 'OK', bad)"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertIn("OK", result.stdout, msg=result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
