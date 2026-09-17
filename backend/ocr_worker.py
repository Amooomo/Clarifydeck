"""Backend OCR worker manager (Phase 2I.2).

Pure stdlib: owns exactly one OCR child process, drains its JSONL stdout into the
Phase 2I.1 ``OCRTransportReceiver``, drains stderr into a bounded tail, and stops
the exact owned PID with bounded escalation.

Must never import rapidocr / onnxruntime / numpy / cv2 / omegaconf / antlr4. The
worker itself is the only place native OCR dependencies are loaded.

Lifecycle is explicit only: no boot start, no QAM start, no status-triggered
start, no auto-restart, no daemon, no setsid.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from backend.ocr_transport import OCRTransportReceiver
from ocr.transport import MAX_LINE_BYTES

MAX_STDERR_LINES = 200
STOP_TIMEOUT_SECONDS = 3.0
MODEL_SUBDIR = ("models", "ppocrv6")
ROI_CONFIG_NAME = "recognition_roi.json"
REQUIRED_MODEL_FILES = ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx")


class OCRWorkerError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class OCRWorkerState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    STOPPING = "STOPPING"


def is_forbidden_interpreter(path: str) -> bool:
    base = Path(path).name.lower()
    return "pluginloader" in base or "decky" in base


def default_python_resolver() -> str:
    from overlay_manager import resolve_python3  # stdlib-only module

    return resolve_python3()


@dataclass
class OCRWorkerStatus:
    state: str
    pid: Optional[int]
    worker_session_id: Optional[str]
    started_monotonic: Optional[float]
    stop_requested: bool
    exit_code: Optional[int]
    last_error: Optional[str]
    change_gate_enabled: bool
    transport: dict
    stderr_tail_count: int
    last_stderr_line: Optional[str]


class OCRWorkerManager:
    def __init__(
        self,
        *,
        plugin_root: Optional[Path] = None,
        worker_path: Optional[Path] = None,
        settings_root: Optional[Path] = None,
        python_resolver: Optional[Callable[[], str]] = None,
        popen_factory: Optional[Callable[..., Any]] = None,
        transport_receiver: Optional[OCRTransportReceiver] = None,
        transport_factory: Optional[Callable[[], OCRTransportReceiver]] = None,
        clock: Callable[[], float] = time.monotonic,
        logger: Optional[Callable[[str], None]] = None,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
    ) -> None:
        self._plugin_root = Path(plugin_root) if plugin_root else Path(__file__).resolve().parents[1]
        self._worker_path = Path(worker_path) if worker_path else self._plugin_root / "scripts" / "ocr_worker.py"
        self._settings_root = Path(settings_root) if settings_root else None
        self._python_resolver = python_resolver or default_python_resolver
        self._popen_factory = popen_factory or subprocess.Popen
        self._transport_factory = transport_factory or OCRTransportReceiver
        self._clock = clock
        self._log = logger or (lambda message: None)
        self._stop_timeout = stop_timeout

        self._state = OCRWorkerState.STOPPED
        self._proc: Optional[Any] = None
        # One authoritative receiver per manager: inject the engine-owned receiver
        # when supplied, otherwise own exactly one created once and reused across
        # real starts (begin_session() resets session state).
        self._receiver: Optional[OCRTransportReceiver] = (
            transport_receiver if transport_receiver is not None else self._transport_factory()
        )
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stderr_tail: deque = deque(maxlen=MAX_STDERR_LINES)
        self._started_monotonic: Optional[float] = None
        self._stop_requested = False
        self._exit_code: Optional[int] = None
        self._last_error: Optional[str] = None
        self._change_gate = False
        self._lock = threading.RLock()

    # -- introspection -----------------------------------------------------

    @property
    def state(self) -> OCRWorkerState:
        return self._state

    def status(self) -> dict:
        with self._lock:
            proc = self._proc
            alive = proc is not None and proc.poll() is None
            receiver = self._receiver
            transport = receiver.status() if receiver is not None else {
                "state": "idle",
                "worker_session_id": None,
                "last_event_seq": 0,
                "last_kind": None,
                "last_text": "",
                "last_confidence": None,
                "last_source_seq": None,
                "transport_messages_received": 0,
                "transport_messages_rejected": 0,
                "transport_out_of_order": 0,
                "transport_text_events": 0,
                "transport_clear_events": 0,
                "last_transport_error": None,
            }
            return {
                "ok": True,
                "state": self._state.value,
                "pid": proc.pid if (alive and proc is not None) else None,
                "worker_session_id": transport.get("worker_session_id"),
                "started_monotonic": self._started_monotonic,
                "stop_requested": self._stop_requested,
                "exit_code": self._exit_code,
                "last_error": self._last_error,
                "change_gate_enabled": self._change_gate,
                "transport": transport,
                "stderr_tail_count": len(self._stderr_tail),
                "last_stderr_line": self._stderr_tail[-1] if self._stderr_tail else None,
            }

    # -- command -----------------------------------------------------------

    def build_command(
        self,
        python_path: str,
        *,
        fps: float,
        model_dir: Optional[str] = None,
        roi_config: Optional[str] = None,
        change_gate: bool = False,
    ) -> list[str]:
        if is_forbidden_interpreter(python_path):
            raise OCRWorkerError("forbidden_interpreter", python_path)
        if not self._worker_path.is_file():
            raise OCRWorkerError("worker_not_found", str(self._worker_path))
        resolved_model = self._resolve_model_dir(model_dir)
        resolved_roi = self._resolve_roi_config(roi_config)
        command = [
            python_path,
            str(self._worker_path),
            "--parent-pid",
            str(os.getpid()),
            "--fps",
            str(fps),
            "--model-dir",
            str(resolved_model),
            # Phase 2L.6: production worker always launches in multi-region mode;
            # the primary-region v1 projection keeps legacy QAM/overlay compatible.
            "--multi-region",
        ]
        if resolved_roi is not None:
            command += ["--roi-config", str(resolved_roi)]
        if change_gate:
            command += ["--change-gate"]
        return command

    def _resolve_model_dir(self, model_dir: Optional[str]) -> Path:
        """Canonical PP-OCRv6 model dir; validates before spawn (no runtime download)."""
        path = Path(model_dir) if model_dir else self._plugin_root.joinpath(*MODEL_SUBDIR)
        if not path.is_dir():
            raise OCRWorkerError("model_missing", f"dir:{path}")
        missing = [name for name in self._required_model_files(path) if not (path / name).is_file()]
        if missing:
            raise OCRWorkerError("model_missing", ",".join(missing))
        return path

    @staticmethod
    def _required_model_files(model_dir: Path) -> list:
        """Prefer the model manifest's declared files; fall back to frozen names."""
        manifest = model_dir / "manifest.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                files = data.get("files")
                if isinstance(files, dict) and files:
                    required = []
                    for entry in files.values():
                        rel = entry.get("path") if isinstance(entry, dict) else entry
                        if isinstance(rel, str) and rel:
                            required.append(rel)
                    if required:
                        return required
            except Exception:
                pass
        return list(REQUIRED_MODEL_FILES)

    def _resolve_roi_config(self, roi_config: Optional[str]) -> Optional[Path]:
        """Canonical Recognition ROI config path (same file the backend owns)."""
        if roi_config is not None:
            return Path(roi_config)
        if self._settings_root is None:
            return None
        return self._settings_root / ROI_CONFIG_NAME

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        *,
        fps: float = 1.0,
        model_dir: Optional[str] = None,
        roi_config: Optional[str] = None,
        change_gate: bool = False,
    ) -> dict:
        with self._lock:
            if self._state in (OCRWorkerState.STARTING, OCRWorkerState.RUNNING):
                return {**self.status(), "detail": "already_running"}
            if self._state == OCRWorkerState.STOPPING:
                return {**self.status(), "detail": "stopping"}
            if self._proc is not None and self._proc.poll() is None:
                return {**self.status(), "detail": "child_alive"}
            self._state = OCRWorkerState.STARTING
            try:
                python_path = self._python_resolver()
                command = self.build_command(
                    python_path, fps=fps, model_dir=model_dir, roi_config=roi_config, change_gate=change_gate
                )
                env = os.environ.copy()
                env["PYTHONNOUSERSITE"] = "1"
                env["PYTHONPATH"] = str(self._plugin_root)
                self._proc = self._popen_factory(
                    command,
                    cwd=str(self._plugin_root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    shell=False,
                    close_fds=True,
                )
            except Exception as exc:
                self._state = OCRWorkerState.FAILED
                code = getattr(exc, "code", None)
                self._last_error = f"{type(exc).__name__}: {code or exc}"
                self._proc = None
                self._log(f"[ocr-worker] start failed: {self._last_error}")
                return self.status()

            receiver = self._receiver
            if receiver is None:
                receiver = self._transport_factory()
                self._receiver = receiver
            receiver.begin_session()
            self._stderr_tail.clear()
            self._exit_code = None
            self._last_error = None
            self._stop_requested = False
            self._change_gate = bool(change_gate)
            self._started_monotonic = self._clock()
            self._stdout_thread = threading.Thread(target=self._read_stdout, name="ocr-worker-stdout", daemon=True)
            self._stderr_thread = threading.Thread(target=self._read_stderr, name="ocr-worker-stderr", daemon=True)
            self._monitor_thread = threading.Thread(target=self._monitor, name="ocr-worker-monitor", daemon=True)
            self._state = OCRWorkerState.RUNNING
            self._stdout_thread.start()
            self._stderr_thread.start()
            self._monitor_thread.start()
            self._log(f"[ocr-worker] started pid={self._proc.pid}")
            return self.status()

    def stop(self, timeout: Optional[float] = None) -> dict:
        timeout = self._stop_timeout if timeout is None else timeout
        with self._lock:
            if self._state == OCRWorkerState.STOPPED or self._proc is None:
                self._state = OCRWorkerState.STOPPED
                return {**self.status(), "detail": "already_stopped"}
            self._state = OCRWorkerState.STOPPING
            self._stop_requested = True
            proc = self._proc
        self._terminate(proc, timeout)
        with self._lock:
            self._exit_code = proc.poll() if proc is not None else self._exit_code
            self._proc = None
            self._state = OCRWorkerState.STOPPED
        # Join readers/monitor OUTSIDE the lock so the monitor can finish its
        # own locked section without deadlocking against stop().
        self._join_reader(self._stdout_thread, timeout)
        self._join_reader(self._stderr_thread, timeout)
        self._join_reader(self._monitor_thread, timeout)
        self._log("[ocr-worker] stopped")
        return self.status()

    # -- internals ---------------------------------------------------------

    def _terminate(self, proc: Any, timeout: float) -> None:
        """Cooperative, exact-PID, bounded stop: SIGINT -> SIGTERM -> SIGKILL.

        No unconditional pre-signal wait: explicit stop knowingly targets a
        live long-running child, so SIGINT is sent immediately.
        """
        if proc is None:
            return
        try:
            if proc.poll() is not None:
                # Already exited: never signal a dead PID.
                return
        except Exception:
            pass
        if self._signal_and_wait(proc, "sigint", timeout):
            return
        if self._signal_and_wait(proc, "terminate", timeout):
            return
        self._signal_and_wait(proc, "kill", timeout)

    @staticmethod
    def _signal_and_wait(proc: Any, action: str, timeout: float) -> bool:
        try:
            if action == "sigint":
                proc.send_signal(signal.SIGINT)
            elif action == "terminate":
                proc.terminate()
            else:
                proc.kill()
        except Exception:
            return False
        try:
            proc.wait(timeout=timeout)
            return True
        except Exception:
            return False

    @staticmethod
    def _join_reader(thread: Optional[threading.Thread], timeout: float) -> None:
        if thread is None:
            return
        thread.join(timeout=max(0.1, timeout))

    def _read_stdout(self) -> None:
        proc = self._proc
        receiver = self._receiver
        if proc is None or receiver is None or proc.stdout is None:
            return
        limit = MAX_LINE_BYTES + 1
        try:
            while True:
                raw = proc.stdout.readline(limit)
                if not raw:
                    break
                if len(raw) >= limit and not raw.endswith(b"\n"):
                    # oversized line: count as rejected, drain to next newline
                    receiver.handle_line("")
                    while True:
                        chunk = proc.stdout.readline(limit)
                        if not chunk or chunk.endswith(b"\n"):
                            break
                    continue
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                receiver.handle_line(line)
        except Exception as exc:
            self._last_error = f"stdout_reader: {exc}"
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    self._stderr_tail.append(line)
                    # Phase 2N.3/2N.4: mirror changed-text diagnostics to the
                    # plugin journal so device traces are collectible.
                    if "[latency]" in line or "[stabilizer-audit" in line:
                        self._log(line)
        except Exception:
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    def _monitor(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            code = proc.wait()
        except Exception:
            code = None
        with self._lock:
            self._exit_code = code
            if not self._stop_requested and self._state in (OCRWorkerState.RUNNING, OCRWorkerState.STARTING):
                self._state = OCRWorkerState.FAILED
                self._last_error = f"worker_exit code={code}"
                self._log(f"[ocr-worker] unexpected exit code={code}")
