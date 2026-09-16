#!/usr/bin/env python3
"""Phase 2I.2 tests: backend OCR worker manager lifecycle.

Uses a fake Popen / fake python resolver; no real OCR or child process needed.

Run:
    python3 scripts/test_backend_ocr_worker.py
"""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SETTINGS_DIR = tempfile.mkdtemp(prefix="clarifydeck-worker-settings-")
sys.modules.setdefault(
    "decky",
    types.SimpleNamespace(
        logger=logging.getLogger("clarifydeck-worker-test"),
        DECKY_PLUGIN_RUNTIME_DIR=tempfile.gettempdir(),
        DECKY_PLUGIN_SETTINGS_DIR=_SETTINGS_DIR,
        DECKY_PLUGIN_DIR=".",
        emit=lambda *args, **kwargs: None,
    ),
)

import main  # noqa: E402
from backend.ocr_transport import OCRTransportReceiver  # noqa: E402
from backend.ocr_worker import (  # noqa: E402
    OCRWorkerManager,
    OCRWorkerState,
    is_forbidden_interpreter,
)
from ocr.stabilizer import StableTextEvent  # noqa: E402
from ocr.transport import MAX_LINE_BYTES, encode_envelope, envelope_from_event  # noqa: E402

NATIVE_MODULES = ("rapidocr", "onnxruntime", "numpy", "cv2", "omegaconf", "antlr4")


def _text_line(seq, text="字幕", confidence=0.9, source_seq=1, ts=1.0):
    event = StableTextEvent(kind="text", text=text, confidence=confidence, source_seq=source_seq, timestamp_monotonic=ts)
    return encode_envelope(envelope_from_event(seq, event))


def _clear_line(seq, ts=2.0):
    event = StableTextEvent(kind="clear", text="", confidence=None, source_seq=None, timestamp_monotonic=ts)
    return encode_envelope(envelope_from_event(seq, event))


class _FakeStream:
    def __init__(self, lines) -> None:
        self._lines = [line if isinstance(line, bytes) else (line + "\n").encode("utf-8") for line in lines]
        self._index = 0
        self.closed = False

    def readline(self, limit: int = -1) -> bytes:
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line

    def close(self) -> None:
        self.closed = True


class FakePopen:
    def __init__(
        self,
        command,
        *,
        stdout_lines=None,
        stderr_lines=None,
        auto_exit=None,
        sigint_exit_code=0,
        ignore_sigint=False,
        ignore_terminate=False,
        **kwargs,
    ) -> None:
        self.command = list(command)
        self.kwargs = kwargs
        self.pid = 4242
        self.stdout = _FakeStream(stdout_lines or [])
        self.stderr = _FakeStream(stderr_lines or [])
        self.signals: list = []
        self.calls: list = []
        self.terminated = False
        self.killed = False
        self._sigint_exit_code = sigint_exit_code
        self._ignore_sigint = ignore_sigint
        self._ignore_terminate = ignore_terminate
        self._exited = auto_exit is not None
        self._exit_code = auto_exit
        self._event = threading.Event()
        if self._exited:
            self._event.set()

    def poll(self):
        return self._exit_code if self._exited else None

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if not self._event.wait(timeout):
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self._exit_code

    def _do_exit(self, code=0) -> None:
        self._exit_code = code
        self._exited = True
        self._event.set()

    def send_signal(self, sig):
        self.signals.append(sig)
        self.calls.append(("send_signal", sig))
        if self._ignore_sigint:
            return
        self._do_exit(self._sigint_exit_code)

    def terminate(self):
        self.terminated = True
        self.calls.append(("terminate",))
        if self._ignore_terminate:
            return
        self._do_exit(0)

    def kill(self):
        self.killed = True
        self.calls.append(("kill",))
        self._do_exit(0)


def StubbornPopen(command, **kwargs):
    kwargs["ignore_sigint"] = True
    kwargs["ignore_terminate"] = True
    return FakePopen(command, **kwargs)


def _manager(proc: FakePopen, **kwargs) -> OCRWorkerManager:
    def factory(command, **popen_kwargs):
        proc.command = list(command)
        proc.kwargs = popen_kwargs
        return proc

    kwargs.setdefault("stop_timeout", 0.1)
    return OCRWorkerManager(
        python_resolver=lambda: "/usr/bin/python3",
        popen_factory=factory,
        clock=lambda: 0.0,
        **kwargs,
    )


class ManagerLifecycleTest(unittest.TestCase):
    def test_initial_state_stopped(self) -> None:
        manager = _manager(FakePopen([], auto_exit=0))
        self.assertEqual(manager.state, OCRWorkerState.STOPPED)
        self.assertEqual(manager.status()["state"], "STOPPED")

    def test_status_query_does_not_spawn(self) -> None:
        spawned = []

        def factory(command, **kwargs):
            spawned.append(command)
            return FakePopen(command, **kwargs)

        manager = OCRWorkerManager(python_resolver=lambda: "/usr/bin/python3", popen_factory=factory, stop_timeout=0.1)
        manager.status()
        manager.status()
        self.assertEqual(spawned, [])
        self.assertIsNone(manager.status()["pid"])

    def test_explicit_start_spawns_once(self) -> None:
        proc = FakePopen([], auto_exit=None)
        manager = _manager(proc)
        status = manager.start(fps=1.0)
        self.assertEqual(status["state"], "RUNNING")
        self.assertEqual(status["pid"], 4242)
        self.assertEqual(proc.command[0], "/usr/bin/python3")
        self.assertIn("scripts", " ".join(proc.command))
        manager.stop()

    def test_second_start_does_not_spawn_again(self) -> None:
        proc = FakePopen([], auto_exit=None)
        calls = []
        manager = OCRWorkerManager(
            python_resolver=lambda: "/usr/bin/python3",
            popen_factory=lambda command, **kwargs: (calls.append(command), proc)[1],
        )
        manager.start()
        second = manager.start()
        self.assertEqual(second["detail"], "already_running")
        self.assertEqual(len(calls), 1)
        manager.stop()

    def test_safe_executable_and_forbidden_interpreter(self) -> None:
        self.assertTrue(is_forbidden_interpreter("/usr/bin/PluginLoader"))
        self.assertTrue(is_forbidden_interpreter("/home/deck/decky"))
        self.assertFalse(is_forbidden_interpreter("/usr/bin/python3"))

    def test_forbidden_interpreter_rejected(self) -> None:
        proc = FakePopen([], auto_exit=None)
        manager = OCRWorkerManager(
            python_resolver=lambda: "/usr/bin/PluginLoader",
            popen_factory=lambda command, **kwargs: proc,
        )
        status = manager.start()
        self.assertEqual(status["state"], "FAILED")
        self.assertIn("forbidden_interpreter", status["last_error"])

    def test_spawn_env_and_pipes(self) -> None:
        proc = FakePopen([], auto_exit=None)
        manager = _manager(proc)
        manager.start()
        self.assertEqual(proc.kwargs["env"]["PYTHONNOUSERSITE"], "1")
        self.assertIs(proc.kwargs["shell"], False)
        self.assertNotIn("start_new_session", proc.kwargs)
        self.assertEqual(proc.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(proc.kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(proc.kwargs["stdin"], subprocess.DEVNULL)
        manager.stop()

    def test_stop_idempotent(self) -> None:
        manager = _manager(FakePopen([], auto_exit=None))
        first = manager.stop()
        second = manager.stop()
        self.assertEqual(first["detail"], "already_stopped")
        self.assertEqual(second["detail"], "already_stopped")

    def test_normal_stop_targets_exact_pid(self) -> None:
        proc = FakePopen([], auto_exit=None)
        manager = _manager(proc)
        manager.start()
        status = manager.stop()
        self.assertEqual(status["state"], "STOPPED")
        self.assertTrue(proc.signals)  # SIGINT sent to the exact owned proc
        self.assertIsNone(manager.status()["pid"])

    def test_bounded_escalation(self) -> None:
        proc = StubbornPopen([], auto_exit=None)
        manager = _manager(proc, stop_timeout=0.05)
        manager.start()
        manager.stop()
        self.assertTrue(proc.signals)  # SIGINT attempted
        self.assertTrue(proc.terminated)  # SIGTERM attempted
        self.assertTrue(proc.killed)  # SIGKILL as last resort
        self.assertEqual(manager.state, OCRWorkerState.STOPPED)

    def test_unexpected_exit_fails(self) -> None:
        proc = FakePopen([], auto_exit=1)
        manager = _manager(proc)
        manager.start()
        for _ in range(50):
            if manager.state == OCRWorkerState.FAILED:
                break
            time.sleep(0.02)
        self.assertEqual(manager.state, OCRWorkerState.FAILED)
        self.assertEqual(manager.status()["exit_code"], 1)

    def test_no_auto_restart(self) -> None:
        proc = FakePopen([], auto_exit=1)
        calls = []
        manager = OCRWorkerManager(
            python_resolver=lambda: "/usr/bin/python3",
            popen_factory=lambda command, **kwargs: (calls.append(command), proc)[1],
        )
        manager.start()
        time.sleep(0.2)
        self.assertEqual(len(calls), 1)
        manager.status()
        self.assertEqual(len(calls), 1)


class ManagerTransportTest(unittest.TestCase):
    def _run(self, stdout_lines, stderr_lines=None):
        proc = FakePopen([], stdout_lines=stdout_lines, stderr_lines=stderr_lines or [], auto_exit=None)
        manager = _manager(proc)
        manager.start()
        time.sleep(0.2)
        status = manager.status()
        manager.stop()
        return manager, status, proc

    def test_text_event_updates_state(self) -> None:
        _manager_obj, status, _proc = self._run([_text_line(1, text="示例字幕")])
        self.assertEqual(status["transport"]["last_text"], "示例字幕")
        self.assertEqual(status["transport"]["last_kind"], "text")

    def test_clear_event_updates_state(self) -> None:
        _manager_obj, status, _proc = self._run([_text_line(1), _clear_line(2)])
        self.assertEqual(status["transport"]["last_kind"], "clear")
        self.assertEqual(status["transport"]["last_text"], "")

    def test_malformed_line_rejected_without_crash(self) -> None:
        _manager_obj, status, _proc = self._run([_text_line(1, text="good"), "{not json"])
        self.assertEqual(status["transport"]["last_text"], "good")
        self.assertGreaterEqual(status["transport"]["transport_messages_rejected"], 1)

    def test_out_of_order_rejected_preserves_state(self) -> None:
        _manager_obj, status, _proc = self._run([_text_line(5, text="a"), _text_line(2, text="b")])
        self.assertEqual(status["transport"]["last_text"], "a")
        self.assertGreaterEqual(status["transport"]["transport_out_of_order"], 1)

    def test_oversized_line_rejected(self) -> None:
        oversized = ('{"v":1,"type":"stable_text","event_seq":1,"kind":"text","text":"' + "a" * (MAX_LINE_BYTES + 10) + '","confidence":0.9,"source_seq":1,"timestamp_monotonic":1.0}') + "\n"
        _manager_obj, status, _proc = self._run([oversized.encode("utf-8")])
        self.assertGreaterEqual(status["transport"]["transport_messages_rejected"], 1)

    def test_stderr_drained_and_bounded(self) -> None:
        lines = [f"line-{index}" for index in range(300)]
        _manager_obj, status, _proc = self._run([_text_line(1)], lines)
        self.assertEqual(status["stderr_tail_count"], 200)
        self.assertEqual(status["last_stderr_line"], "line-299")

    def test_fresh_session_per_start(self) -> None:
        proc = FakePopen([], stdout_lines=[_text_line(1)], auto_exit=None)
        manager = _manager(proc)
        manager.start()
        first_session = manager.status()["worker_session_id"]
        manager.stop()
        proc2 = FakePopen([], stdout_lines=[_text_line(1, text="new")], auto_exit=None)
        manager._popen_factory = lambda command, **kwargs: (setattr(proc2, "command", command), proc2)[1]
        manager.start()
        second_session = manager.status()["worker_session_id"]
        self.assertNotEqual(first_session, second_session)
        self.assertEqual(manager.status()["transport"]["last_text"], "new")
        self.assertEqual(manager.status()["transport"]["last_event_seq"], 1)
        manager.stop()


class ImportSafetyTest(unittest.TestCase):
    def test_backend_modules_import_no_native_deps(self) -> None:
        code = (
            "import sys; import backend.ocr_transport, backend.ocr_worker, backend.parent_death; "
            f"bad=[m for m in {NATIVE_MODULES!r} if m in sys.modules]; print('BAD' if bad else 'OK', bad)"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertIn("OK", result.stdout, msg=result.stdout + result.stderr)


class EngineOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = main.ClarifyDeckEngine()

    def test_status_does_not_spawn(self) -> None:
        status = self.engine.ocr_worker_status()
        self.assertEqual(status["state"], "STOPPED")
        self.assertIsNone(status["pid"])

    def test_no_worker_at_engine_init(self) -> None:
        self.assertIsNone(self.engine._ocr_worker)

    def test_non_leader_start_rejected(self) -> None:
        self.engine._role = "standby"
        result = self.engine.start_ocr_worker()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_leader")

    def test_capture_conflict_rejected(self) -> None:
        class _RunningProducer:
            def status(self):
                return {"state": "RUNNING"}

        self.engine._role = "leader"
        self.engine._producer = _RunningProducer()
        result = self.engine.start_ocr_worker()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "capture_conflict")

    def test_no_worker_at_plugin_boot(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("async def _main(self)")
        end = source.index("async def _unload(self)")
        boot_body = source[start:end]
        self.assertNotIn("start_ocr_worker", boot_body)
        self.assertNotIn("OCRWorkerManager", boot_body)


class WorkerEntrypointTest(unittest.TestCase):
    def test_help_and_defaults(self) -> None:
        import scripts.ocr_worker as worker  # noqa: F401

        args = worker._parse_args([])
        self.assertEqual(args.fps, 1.0)
        self.assertIsNone(args.parent_pid)
        self.assertFalse(args.change_gate)
        self.assertEqual(args.ort_intra_threads, 2)
        self.assertEqual(args.ort_inter_threads, 1)
        self.assertEqual(args.opencv_threads, 1)
        self.assertEqual(args.det_limit_side_len, 256)
        self.assertEqual(args.det_limit_type, "min")

    def test_change_gate_opt_in(self) -> None:
        import scripts.ocr_worker as worker

        args = worker._parse_args(["--change-gate"])
        self.assertTrue(args.change_gate)


class StopOrderingTest(unittest.TestCase):
    """Phase 2I.2.1: explicit stop sends SIGINT immediately (no pre-signal wait)."""

    def _first_index(self, calls, kind):
        for index, call in enumerate(calls):
            if call[0] == kind:
                return index
        return None

    def test_sigint_sent_before_any_wait(self) -> None:
        proc = FakePopen([], auto_exit=None)
        manager = _manager(proc)
        manager.start()
        manager.stop()
        first_signal = self._first_index(proc.calls, "send_signal")
        # the monitor's blocking wait(timeout=None) is not part of the stop path
        first_stop_wait = next(
            (index for index, call in enumerate(proc.calls) if call[0] == "wait" and call[1] is not None),
            None,
        )
        self.assertIsNotNone(first_signal)
        self.assertIsNotNone(first_stop_wait)
        self.assertLess(first_signal, first_stop_wait)

    def test_exit_after_sigint_no_sigterm_or_sigkill(self) -> None:
        proc = FakePopen([], auto_exit=None)
        manager = _manager(proc)
        manager.start()
        manager.stop()
        self.assertEqual(proc.signals, [signal.SIGINT])
        self.assertFalse(proc.terminated)
        self.assertFalse(proc.killed)

    def test_ignoring_sigint_gets_sigterm(self) -> None:
        proc = FakePopen([], auto_exit=None, ignore_sigint=True)
        manager = _manager(proc, stop_timeout=0.05)
        manager.start()
        manager.stop()
        self.assertTrue(proc.terminated)
        self.assertFalse(proc.killed)

    def test_ignoring_sigint_and_sigterm_gets_sigkill(self) -> None:
        proc = StubbornPopen([], auto_exit=None)
        manager = _manager(proc, stop_timeout=0.05)
        manager.start()
        manager.stop()
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)

    def test_already_exited_child_not_signaled(self) -> None:
        proc = FakePopen([], auto_exit=0)
        manager = _manager(proc)
        manager.start()
        time.sleep(0.1)
        manager.stop()
        self.assertEqual(proc.signals, [])
        self.assertFalse(proc.terminated)
        self.assertFalse(proc.killed)

    def test_sigint_exit_code_not_reported_as_failure(self) -> None:
        proc = FakePopen([], auto_exit=None, sigint_exit_code=130)
        manager = _manager(proc)
        manager.start()
        status = manager.stop()
        self.assertEqual(status["state"], "STOPPED")
        self.assertEqual(status["exit_code"], 130)

    def test_stopped_preserves_transport_state(self) -> None:
        proc = FakePopen([], stdout_lines=[_text_line(1, text="保留字幕")], auto_exit=None)
        manager = _manager(proc)
        manager.start()
        time.sleep(0.2)
        status = manager.stop()
        self.assertEqual(status["state"], "STOPPED")
        self.assertEqual(status["transport"]["last_text"], "保留字幕")

    def test_reader_threads_cleaned(self) -> None:
        proc = FakePopen([], auto_exit=None)
        manager = _manager(proc)
        manager.start()
        manager.stop()
        self.assertFalse(manager._stdout_thread.is_alive())
        self.assertFalse(manager._stderr_thread.is_alive())
        self.assertFalse(manager._monitor_thread.is_alive())


class ProductionLaunchPathTest(unittest.TestCase):
    """Phase 2I.2.3: production RPC resolves canonical model/ROI launch paths."""

    def _temp_root(self, *, with_det=True, with_rec=True, with_manifest=True, with_models=True):
        root = Path(tempfile.mkdtemp(prefix="clarifydeck-launch-"))
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "ocr_worker.py").write_text("# stub\n", encoding="utf-8")
        if with_models:
            model_dir = root / "models" / "ppocrv6"
            model_dir.mkdir(parents=True)
            if with_manifest:
                (model_dir / "manifest.json").write_text(
                    json.dumps(
                        {
                            "format_version": 2,
                            "engine": "rapidocr",
                            "family": "PP-OCRv6",
                            "files": {
                                "det": {"path": "PP-OCRv6_det_small.onnx"},
                                "rec": {"path": "PP-OCRv6_rec_small.onnx"},
                            },
                            "dictionary": {"mode": "embedded"},
                        }
                    ),
                    encoding="utf-8",
                )
            if with_det:
                (model_dir / "PP-OCRv6_det_small.onnx").write_bytes(b"x")
            if with_rec:
                (model_dir / "PP-OCRv6_rec_small.onnx").write_bytes(b"x")
        return root

    def _manager(self, root, settings_root=None):
        proc = FakePopen([], auto_exit=None)
        calls = []

        def factory(command, **kwargs):
            calls.append(list(command))
            proc.command = list(command)
            proc.kwargs = kwargs
            return proc

        manager = OCRWorkerManager(
            plugin_root=root,
            settings_root=settings_root,
            python_resolver=lambda: "/usr/bin/python3",
            popen_factory=factory,
            stop_timeout=0.1,
        )
        return manager, calls, proc

    def test_default_model_dir_resolved(self) -> None:
        root = self._temp_root()
        manager, calls, proc = self._manager(root)
        status = manager.start(fps=1.0, change_gate=False)
        self.assertEqual(status["state"], "RUNNING")
        command = calls[0]
        index = command.index("--model-dir")
        self.assertEqual(command[index + 1], str(root / "models" / "ppocrv6"))
        manager.stop()

    def test_no_none_in_argv(self) -> None:
        root = self._temp_root()
        manager, calls, proc = self._manager(root, settings_root=root / "settings")
        manager.start(fps=1.0, change_gate=False)
        self.assertNotIn("None", calls[0])
        manager.stop()

    def test_missing_model_dir_fails_before_spawn(self) -> None:
        root = self._temp_root(with_models=False)
        manager, calls, proc = self._manager(root)
        status = manager.start(fps=1.0, change_gate=False)
        self.assertEqual(status["state"], "FAILED")
        self.assertIn("model_missing", status["last_error"])
        self.assertEqual(calls, [])

    def test_missing_det_fails_before_spawn(self) -> None:
        root = self._temp_root(with_det=False)
        manager, calls, proc = self._manager(root)
        status = manager.start()
        self.assertEqual(status["state"], "FAILED")
        self.assertIn("model_missing", status["last_error"])
        self.assertEqual(calls, [])

    def test_missing_rec_fails_before_spawn(self) -> None:
        root = self._temp_root(with_rec=False)
        manager, calls, proc = self._manager(root)
        status = manager.start()
        self.assertEqual(status["state"], "FAILED")
        self.assertIn("model_missing", status["last_error"])
        self.assertEqual(calls, [])

    def test_explicit_model_dir_still_works(self) -> None:
        root = self._temp_root()
        explicit = self._temp_root()
        manager, calls, proc = self._manager(root)
        manager.start(fps=1.0, model_dir=str(explicit / "models" / "ppocrv6"))
        command = calls[0]
        index = command.index("--model-dir")
        self.assertEqual(command[index + 1], str(explicit / "models" / "ppocrv6"))
        manager.stop()

    def test_default_roi_config_resolved(self) -> None:
        root = self._temp_root()
        settings = root / "settings"
        manager, calls, proc = self._manager(root, settings_root=settings)
        manager.start(fps=1.0)
        command = calls[0]
        index = command.index("--roi-config")
        self.assertEqual(command[index + 1], str(settings / "recognition_roi.json"))
        manager.stop()

    def test_explicit_roi_config_overrides(self) -> None:
        root = self._temp_root()
        manager, calls, proc = self._manager(root, settings_root=root / "settings")
        manager.start(fps=1.0, roi_config="/tmp/custom-roi.json")
        command = calls[0]
        index = command.index("--roi-config")
        self.assertEqual(command[index + 1], str(Path("/tmp/custom-roi.json")))
        manager.stop()

    def test_no_shell_or_setsid(self) -> None:
        root = self._temp_root()
        manager, calls, proc = self._manager(root)
        manager.start(fps=1.0)
        self.assertIs(proc.kwargs["shell"], False)
        self.assertNotIn("start_new_session", proc.kwargs)
        manager.stop()

    def test_production_rpc_path_builds_valid_command(self) -> None:
        root = self._temp_root()
        settings = root / "settings"
        manager, calls, proc = self._manager(root, settings_root=settings)
        engine = main.ClarifyDeckEngine()
        engine._role = "leader"
        engine._producer = None
        engine._ocr_worker = manager
        result = engine.start_ocr_worker()
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "RUNNING")
        command = calls[0]
        self.assertEqual(command[0], "/usr/bin/python3")
        self.assertIn(str(root / "scripts" / "ocr_worker.py"), command)
        model_index = command.index("--model-dir")
        self.assertEqual(command[model_index + 1], str(root / "models" / "ppocrv6"))
        roi_index = command.index("--roi-config")
        self.assertEqual(command[roi_index + 1], str(settings / "recognition_roi.json"))
        self.assertNotIn("None", command)
        manager.stop()


class _CountingReceiver(OCRTransportReceiver):
    """Spy receiver: counts begin_session() calls on the shared instance."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.begin_calls = 0

    def begin_session(self, session_id=None):
        self.begin_calls += 1
        return super().begin_session(session_id)


class SharedReceiverWiringTest(unittest.TestCase):
    """Phase 2I.3.1: engine owns one receiver, shared with the manager."""

    def setUp(self) -> None:
        self.engine = main.ClarifyDeckEngine()

    def _start_with_stdout(self, lines, manager=None):
        manager = manager or self.engine._ocr_worker_manager()
        self.assertIsNotNone(manager)
        proc = FakePopen([], stdout_lines=lines, auto_exit=None)
        manager._popen_factory = lambda command, **kwargs: proc
        manager._python_resolver = lambda: "/usr/bin/python3"
        manager.start()
        time.sleep(0.2)
        return manager, proc

    def test_engine_transport_receiver_is_stable(self) -> None:
        first = self.engine._transport_receiver()
        self.assertIsNotNone(first)
        self.assertIs(self.engine._transport_receiver(), first)

    def test_manager_receives_engine_owned_receiver(self) -> None:
        manager = self.engine._ocr_worker_manager()
        self.assertIsNotNone(manager)
        self.assertIs(manager._receiver, self.engine._transport_receiver())

    def test_manager_and_engine_share_session_id(self) -> None:
        manager, _proc = self._start_with_stdout([_text_line(1)])
        try:
            self.assertEqual(
                manager.status()["worker_session_id"],
                self.engine.ocr_transport_status()["worker_session_id"],
            )
        finally:
            manager.stop()

    def test_latest_stable_text_reflects_manager_event(self) -> None:
        manager, _proc = self._start_with_stdout(
            [_text_line(1, text="共享字幕", confidence=0.88, source_seq=7)]
        )
        try:
            latest = self.engine.latest_stable_text()
            self.assertTrue(latest["ok"])
            self.assertEqual(latest["kind"], "text")
            self.assertEqual(latest["text"], "共享字幕")
            self.assertAlmostEqual(latest["confidence"], 0.88)
            self.assertEqual(latest["source_seq"], 7)
            self.assertEqual(latest["last_event_seq"], 1)
            self.assertEqual(latest["worker_session_id"], manager.status()["worker_session_id"])
        finally:
            manager.stop()

    def test_manager_and_engine_share_event_seq(self) -> None:
        manager, _proc = self._start_with_stdout([_text_line(1)])
        try:
            self.assertEqual(
                manager.status()["transport"]["last_event_seq"],
                self.engine.ocr_transport_status()["last_event_seq"],
            )
        finally:
            manager.stop()

    def test_no_second_receiver_in_production_path(self) -> None:
        created = []
        original_main = main.ocr_transport.OCRTransportReceiver
        original_worker = main.ocr_worker_module.OCRTransportReceiver

        def counting(*args, **kwargs):
            instance = original_main(*args, **kwargs)
            created.append(instance)
            return instance

        main.ocr_transport.OCRTransportReceiver = counting
        main.ocr_worker_module.OCRTransportReceiver = counting
        try:
            engine = main.ClarifyDeckEngine()
            receiver = engine._transport_receiver()
            manager = engine._ocr_worker_manager()
            self.assertEqual(len(created), 1)
            self.assertIs(manager._receiver, receiver)
        finally:
            main.ocr_transport.OCRTransportReceiver = original_main
            main.ocr_worker_module.OCRTransportReceiver = original_worker

    def test_manager_unavailable_when_transport_unavailable(self) -> None:
        original = main.ocr_transport
        main.ocr_transport = None
        try:
            engine = main.ClarifyDeckEngine()
            self.assertIsNone(engine._ocr_worker_manager())
            status = engine.ocr_worker_status()
            self.assertFalse(status["ok"])
            self.assertEqual(status["error"], "ocr_worker_unavailable")
        finally:
            main.ocr_transport = original

    def test_first_real_start_calls_begin_session_once(self) -> None:
        receiver = _CountingReceiver()
        self.engine._ocr_transport = receiver
        manager, _proc = self._start_with_stdout([_text_line(1)])
        try:
            self.assertEqual(receiver.begin_calls, 1)
        finally:
            manager.stop()

    def test_repeated_start_while_running_does_not_rebegin(self) -> None:
        receiver = _CountingReceiver()
        self.engine._ocr_transport = receiver
        manager, _proc = self._start_with_stdout([_text_line(1)])
        try:
            first_session = manager.status()["worker_session_id"]
            repeated = manager.start()
            self.assertEqual(repeated["detail"], "already_running")
            self.assertEqual(receiver.begin_calls, 1)
            self.assertEqual(manager.status()["worker_session_id"], first_session)
        finally:
            manager.stop()

    def test_restart_reuses_receiver_object_new_session(self) -> None:
        receiver = _CountingReceiver()
        self.engine._ocr_transport = receiver
        manager, _proc = self._start_with_stdout([_text_line(1)])
        first_session = manager.status()["worker_session_id"]
        manager.stop()
        self._start_with_stdout([_text_line(1, text="new")], manager=manager)
        try:
            self.assertIs(manager._receiver, receiver)
            self.assertNotEqual(manager.status()["worker_session_id"], first_session)
            self.assertEqual(receiver.begin_calls, 2)
        finally:
            manager.stop()

    def test_fresh_start_resets_counters(self) -> None:
        receiver = _CountingReceiver()
        self.engine._ocr_transport = receiver
        manager, _proc = self._start_with_stdout([_text_line(1), _text_line(2, text="b")])
        first = manager.status()["transport"]
        self.assertEqual(first["transport_messages_received"], 2)
        manager.stop()
        self._start_with_stdout([_text_line(1, text="fresh")], manager=manager)
        try:
            fresh = manager.status()["transport"]
            self.assertEqual(fresh["last_event_seq"], 1)
            self.assertEqual(fresh["transport_messages_received"], 1)
            self.assertEqual(fresh["last_text"], "fresh")
        finally:
            manager.stop()


class DirectIntegrationRegressionTest(unittest.TestCase):
    """Phase 2I.3.1: production ownership graph engine -> shared receiver -> manager."""

    def test_engine_latest_stable_text_after_manager_event(self) -> None:
        engine = main.ClarifyDeckEngine()
        manager = engine._ocr_worker_manager()
        self.assertIsNotNone(manager)
        proc = FakePopen([], stdout_lines=[_text_line(1, text="集成字幕", confidence=0.9)], auto_exit=None)
        manager._popen_factory = lambda command, **kwargs: proc
        manager._python_resolver = lambda: "/usr/bin/python3"
        manager.start()
        try:
            for _ in range(50):
                if engine.latest_stable_text().get("last_event_seq") == 1:
                    break
                time.sleep(0.02)
            latest = engine.latest_stable_text()
            self.assertEqual(latest["text"], "集成字幕")
            self.assertEqual(latest["last_event_seq"], 1)
        finally:
            manager.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
