"""ClarifyDeck overlay manager (recovery-safe, Phase 1C.2).

Owns the standalone external overlay renderer process and the IPC connection to
it. Key safety properties:

- The renderer is only ever started by an explicit user ``enable()``.
- ``update`` / ``hide`` never start or restart the renderer.
- No automatic restart: a dead renderer becomes ``FAILED`` until re-enabled.
- The renderer is a direct child (no setsid), stopped by exact PID only.
- The renderer is launched with the *system* python3, never with
  ``sys.executable`` (Decky runs under PyInstaller, where ``sys.executable`` is
  the Decky ``PluginLoader`` and would relaunch the whole loader).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Optional

try:
    import decky

    def _log(message: str) -> None:
        decky.logger.info(f"[overlay] {message}")

    def _log_error(message: str) -> None:
        decky.logger.error(f"[overlay] {message}")
except Exception:  # pragma: no cover - smoke tests / standalone

    def _log(message: str) -> None:
        print(f"[overlay] {message}")

    def _log_error(message: str) -> None:
        print(f"[overlay] ERROR: {message}", file=sys.stderr)


from overlay import protocol

PLUGIN_ROOT = Path(__file__).resolve().parent
RENDERER_PATH = PLUGIN_ROOT / "overlay" / "renderer.py"
RENDERER_COMPONENT = "clarifydeck-overlay-renderer"
RENDERER_PROTOCOL = 1


class OverlayError(RuntimeError):
    pass


class OverlayState(str, Enum):
    DISABLED = "DISABLED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    STOPPING = "STOPPING"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _is_forbidden_interpreter(path: str) -> bool:
    base = Path(path).name.lower()
    return "pluginloader" in base or "decky" in base


def resolve_python3() -> str:
    """Return a real system python3, never a frozen Decky loader binary."""
    candidates: list[str] = ["/usr/bin/python3"]
    which = shutil.which("python3")
    if which:
        candidates.append(which)
    exe = sys.executable or ""
    if exe and not _is_frozen() and "python" in Path(exe).name.lower():
        candidates.append(exe)

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if _is_forbidden_interpreter(candidate):
            continue
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise OverlayError("python3_not_found")


def _read_environ(pid: int) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return result
    for entry in raw.split(b"\0"):
        if b"=" in entry:
            key, _, value = entry.partition(b"=")
            result[key.decode(errors="ignore")] = value.decode(errors="ignore")
    return result


def _find_pids(name: str) -> list[int]:
    pids: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return pids
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if comm == name:
            pids.append(int(entry.name))
    return pids


def resolve_overlay_display() -> str:
    override = os.environ.get("CLARIFYDECK_OVERLAY_DISPLAY")
    if override:
        return override

    for pid in _find_pids("mangoapp"):
        display = _read_environ(pid).get("DISPLAY")
        if display:
            _log(f"display from mangoapp: {display}")
            return display

    sockets = sorted(Path("/tmp/.X11-unix").glob("X*")) if Path("/tmp/.X11-unix").is_dir() else []
    if sockets:
        for candidate in sockets:
            if candidate.name == "X0":
                return ":0"
        return f":{sockets[0].name[1:]}"

    _log("display autodetect failed; falling back to :0")
    return ":0"


def _xprop_env() -> dict[str, str]:
    env = os.environ.copy()
    xauthority = Path("/home/deck/.Xauthority")
    if xauthority.exists():
        env.setdefault("XAUTHORITY", str(xauthority))
    return env


def gamescope_ready(display: str) -> bool:
    xprop = shutil.which("xprop") or "/usr/bin/xprop"
    if not Path(xprop).exists():
        _log_error("xprop not found; cannot verify gamescope readiness")
        return False
    try:
        proc = subprocess.run(
            [xprop, "-display", display, "-root"],
            capture_output=True,
            text=True,
            env=_xprop_env(),
            timeout=6,
        )
    except Exception as exc:
        _log_error(f"gamescope readiness check failed: {exc}")
        return False
    if proc.returncode != 0:
        _log_error(f"gamescope readiness: xprop failed on {display}")
        return False
    text = proc.stdout or ""
    return "GAMESCOPE_PID" in text or "GAMESCOPE_XWAYLAND_SERVER_ID" in text


class OverlayManager:
    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self.socket_path = protocol.socket_path()
        self.display = resolve_overlay_display()
        self._deck_user = self._detect_deck_user()
        self._deck_ids = self._detect_deck_ids()
        self._state = OverlayState.DISABLED
        self._lock = asyncio.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._last_text = ""
        self._visible = False
        self._last_error: Optional[str] = None
        self._log_handle = None
        # Renderer ownership: the shared renderer process stays alive while EITHER
        # the persistent text overlay OR the region preview needs it.
        self._text_enabled = False
        self._preview_enabled = False
        self._preview_regions: list = []
        self._region_text: dict = {}

    # -- identity ----------------------------------------------------------

    @staticmethod
    def _detect_deck_user() -> Optional[str]:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return None
        try:
            import pwd

            pwd.getpwnam("deck")
            return "deck"
        except Exception:
            return None

    @staticmethod
    def _detect_deck_ids() -> Optional[tuple[int, int]]:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return None
        try:
            import pwd

            entry = pwd.getpwnam("deck")
            return entry.pw_uid, entry.pw_gid
        except Exception:
            return None

    # -- state -------------------------------------------------------------

    def status(self) -> dict:
        self._check_alive()
        return {
            "enabled": self._text_enabled,
            "state": self._state.value,
            "display": self.display,
            "socket": str(self.socket_path) if self._state == OverlayState.RUNNING else None,
            "renderer_pid": self._proc.pid if self._proc is not None and self._proc.poll() is None else None,
            "connected": self._sock is not None,
            "visible": self._visible,
            "preview_enabled": self._preview_enabled,
            "preview_region_count": len(self._preview_regions),
            "region_text_count": len(self._region_text),
            "last_error": self._last_error,
        }

    def _check_alive(self) -> None:
        if self._state == OverlayState.RUNNING and self._proc is not None and self._proc.poll() is not None:
            _log_error("renderer exited unexpectedly; state=FAILED (no auto restart)")
            self._last_error = "renderer_exited"
            self._close_socket()
            self._proc = None
            self._state = OverlayState.FAILED

    # -- runtime dir -------------------------------------------------------

    def _ensure_runtime_dir(self) -> Path:
        runtime = protocol.runtime_dir()
        runtime.mkdir(parents=True, exist_ok=True)
        if self._deck_ids is not None:
            uid, gid = self._deck_ids
            try:
                os.chown(runtime, uid, gid)
            except OSError:
                pass
        try:
            os.chmod(runtime, 0o700)
        except OSError:
            pass
        return runtime

    # -- lifecycle ---------------------------------------------------------

    def _ensure_running(self) -> None:
        """Spawn the renderer if needed. Must be called under ``self._lock``."""
        if self._state == OverlayState.RUNNING and self._proc is not None and self._proc.poll() is None:
            return
        if self._state == OverlayState.STARTING:
            return
        self._state = OverlayState.STARTING
        self._last_error = None
        try:
            if not gamescope_ready(self.display):
                raise OverlayError("gamescope_not_ready")
            python_path = resolve_python3()
            self._check_python(python_path)
            command = self._build_command(python_path)
            self._spawn(command)
            self._verify_child()
            if not self._wait_for_socket(4.0):
                raise OverlayError("renderer_socket_timeout")
            self._connect()
            self._handshake()
            self._state = OverlayState.RUNNING
            self._visible = False
            self._last_text = ""
            _log(
                f"overlay renderer started (display={self.display}, "
                f"pid={self._proc.pid if self._proc else None})"
            )
        except Exception as exc:
            self._last_error = str(exc)
            _log_error(f"overlay enable failed: {exc}")
            self._shutdown_process()
            self._state = OverlayState.FAILED

    def _teardown_locked(self) -> None:
        self._state = OverlayState.STOPPING
        self._shutdown_process()
        self._visible = False
        self._last_text = ""
        self._state = OverlayState.DISABLED

    def _send_preview_locked(self) -> None:
        self._send({"type": "set_region_preview", "regions": self._preview_regions})

    async def enable(self) -> dict:
        async with self._lock:
            self._ensure_running()
            if self._state == OverlayState.RUNNING:
                self._text_enabled = True
                self._send({"type": "hide"})
                # No replay: start with empty text blocks on a fresh enable.
                self._region_text = {}
                self._send({"type": "clear_all_region_text"})
                if self._preview_enabled:
                    self._send_preview_locked()
            return self.status()

    async def disable(self) -> dict:
        async with self._lock:
            self._text_enabled = False
            if self._state == OverlayState.RUNNING:
                self._send({"type": "hide"})
                # Clear OCR text blocks but never the preview layer.
                self._region_text = {}
                self._send({"type": "clear_all_region_text"})
            self._visible = False
            self._last_text = ""
            if not self._preview_enabled and self._state != OverlayState.DISABLED:
                self._teardown_locked()
                _log("overlay disabled")
            return self.status()

    async def stop(self) -> None:
        async with self._lock:
            self._text_enabled = False
            self._preview_enabled = False
            self._preview_regions = []
            self._region_text = {}
            if self._state != OverlayState.DISABLED:
                self._teardown_locked()
                _log("overlay stopped")

    # -- per-region persistent text blocks (Phase 2M.1) --------------------

    async def set_region_text(self, region_id: str, rect: tuple, text: str) -> dict:
        async with self._lock:
            key = str(region_id)
            self._region_text[key] = {"rect": tuple(rect), "text": text}
            if self._text_enabled and self._state == OverlayState.RUNNING:
                x, y, w, h = rect
                self._send(
                    {
                        "type": "set_region_text",
                        "region_id": key,
                        "rect": {"x": x, "y": y, "w": w, "h": h},
                        "text": text,
                    }
                )
            return self.status()

    async def hide_region_text(self, region_id: str) -> dict:
        async with self._lock:
            key = str(region_id)
            self._region_text.pop(key, None)
            if self._state == OverlayState.RUNNING:
                self._send({"type": "hide_region_text", "region_id": key})
            return self.status()

    async def clear_all_region_text(self) -> dict:
        async with self._lock:
            self._region_text = {}
            if self._state == OverlayState.RUNNING:
                self._send({"type": "clear_all_region_text"})
            return self.status()

    # -- region preview (Phase 2L.8.2) -------------------------------------

    async def set_region_preview_enabled(self, enabled: bool) -> dict:
        async with self._lock:
            self._preview_enabled = bool(enabled)
            if self._preview_enabled:
                self._ensure_running()
                if self._state == OverlayState.RUNNING:
                    self._send_preview_locked()
            else:
                if self._state == OverlayState.RUNNING:
                    self._send({"type": "clear_region_preview"})
                self._preview_regions = []
                if not self._text_enabled and self._state != OverlayState.DISABLED:
                    self._teardown_locked()
                    _log("overlay disabled (preview off)")
            return self.status()

    async def set_region_preview(self, regions: list) -> dict:
        async with self._lock:
            self._preview_regions = protocol.sanitize_preview_regions(regions)
            if self._preview_enabled and self._state == OverlayState.RUNNING:
                self._send_preview_locked()
            if self._preview_enabled:
                _log(f"region preview set count={len(self._preview_regions)}")
            return self.status()

    async def clear_region_preview(self) -> dict:
        async with self._lock:
            self._preview_regions = []
            if self._state == OverlayState.RUNNING:
                self._send({"type": "clear_region_preview"})
            return self.status()

    # -- command / spawn ---------------------------------------------------

    def _build_command(self, python_path: str) -> list[str]:
        if _is_forbidden_interpreter(python_path):
            raise OverlayError(f"forbidden_interpreter:{python_path}")
        if _is_frozen() and os.path.realpath(python_path) == os.path.realpath(sys.executable or ""):
            raise OverlayError("frozen_runtime_must_not_use_sys_executable")
        if not RENDERER_PATH.is_file():
            raise OverlayError("renderer_not_found")
        return [
            python_path,
            str(RENDERER_PATH),
            "--socket",
            str(self.socket_path),
            "--display",
            self.display,
            "--parent-pid",
            str(os.getpid()),
        ]

    def _check_python(self, python_path: str) -> None:
        try:
            proc = subprocess.run(
                [python_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception as exc:
            raise OverlayError(f"python3_check_failed:{exc}") from exc
        if proc.returncode != 0:
            raise OverlayError("python3_check_failed")

    def _spawn(self, command: list[str]) -> None:
        self._ensure_runtime_dir()
        try:
            self._log_handle = open(protocol.renderer_log_path(), "ab")
        except OSError:
            self._log_handle = None

        env = os.environ.copy()
        env["DISPLAY"] = self.display
        kwargs: dict = {
            "cwd": str(PLUGIN_ROOT),
            "stdin": subprocess.DEVNULL,
            "stdout": self._log_handle,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
            "env": env,
        }

        _log(
            f"renderer spawn: sys.executable={sys.executable!r} frozen={_is_frozen()} "
            f"python={command[0]!r} renderer={str(RENDERER_PATH)!r}"
        )
        _log(f"renderer command={command!r}")

        if hasattr(os, "geteuid") and os.geteuid() == 0 and self._deck_user:
            env["HOME"] = "/home/deck"
            env["XDG_RUNTIME_DIR"] = "/run/user/1000"
            xauthority = Path("/home/deck/.Xauthority")
            if xauthority.exists():
                env["XAUTHORITY"] = str(xauthority)
            try:
                self._proc = subprocess.Popen(command, user=self._deck_user, group=self._deck_user, **kwargs)
            except Exception as exc:
                _log_error(f"Popen(user=deck) failed ({exc}); falling back to sudo")
                prefix = [
                    "/usr/bin/sudo",
                    "-u",
                    self._deck_user,
                    "env",
                    f"DISPLAY={self.display}",
                    "HOME=/home/deck",
                    "XDG_RUNTIME_DIR=/run/user/1000",
                ]
                if xauthority.exists():
                    prefix.append(f"XAUTHORITY={xauthority}")
                self._proc = subprocess.Popen(prefix + command, **kwargs)
        else:
            self._proc = subprocess.Popen(command, **kwargs)

        _log(f"renderer spawned pid={self._proc.pid} (as {self._deck_user or 'self'})")

    def _verify_child(self) -> None:
        if self._proc is None:
            raise OverlayError("no_process")
        try:
            raw = Path(f"/proc/{self._proc.pid}/cmdline").read_bytes()
        except OSError:
            return  # non-Linux / no proc: skip
        cmdline = raw.replace(b"\0", b" ").decode(errors="ignore")
        lowered = cmdline.lower()
        if "renderer.py" not in lowered or "pluginloader" in lowered:
            raise OverlayError("renderer_identity_mismatch")

    def _wait_for_socket(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False
            if self.socket_path.exists():
                try:
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    probe.settimeout(0.3)
                    probe.connect(str(self.socket_path))
                    probe.close()
                    return True
                except OSError:
                    pass
            time.sleep(0.1)
        return False

    def _connect(self) -> None:
        self._close_socket()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(str(self.socket_path))
        self._sock = sock

    def _handshake(self) -> None:
        if self._sock is None:
            raise OverlayError("no_socket")
        self._sock.sendall(protocol.encode_message({"type": "ping"}))
        self._sock.settimeout(3.0)
        buffer = b""
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                break
            except OSError as exc:
                raise OverlayError(f"handshake_io:{exc}") from exc
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                payload = protocol.decode_message(line)
                if payload and payload.get("type") == "pong":
                    if payload.get("component") != RENDERER_COMPONENT:
                        raise OverlayError("renderer_identity_mismatch")
                    return
        raise OverlayError("renderer_handshake_failed")

    def _close_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _send(self, payload: dict) -> bool:
        if self._sock is None:
            return False
        try:
            self._sock.sendall(protocol.encode_message(payload))
            return True
        except OSError as exc:
            _log_error(f"send failed: {exc}")
            self._close_socket()
            return False

    def _shutdown_process(self) -> None:
        if self._sock is not None:
            self._send({"type": "shutdown"})
        self._close_socket()
        if self._proc is not None:
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        self._proc.kill()
                        self._proc.wait(timeout=2)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception:
                pass
            self._proc = None
        try:
            self.socket_path.unlink()
        except OSError:
            pass
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    # -- display -----------------------------------------------------------

    async def update(self, text: str) -> None:
        self._check_alive()
        if self._state != OverlayState.RUNNING:
            return
        text = protocol.truncate_text(text)
        if not text:
            await self.hide()
            return
        if self._visible and text == self._last_text:
            return
        message_type = "update" if self._visible else "show"
        self._last_text = text
        self._visible = True
        self._send({"type": message_type, "text": text})

    async def hide(self) -> None:
        self._check_alive()
        if self._state != OverlayState.RUNNING:
            return
        if not self._visible and not self._last_text:
            return
        self._visible = False
        self._last_text = ""
        self._send({"type": "hide"})
