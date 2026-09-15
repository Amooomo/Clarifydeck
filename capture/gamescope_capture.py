#!/usr/bin/env python3
"""ClarifyDeck Phase 2A - Gamescope base-plane capture (isolation).

Captures the game/base plane via the private ``gamescope_control`` Wayland
protocol using ``take_screenshot`` with ``base_plane_only`` so Steam UI, QAM,
mangoapp and ClarifyDeck's own external overlay are excluded from OCR input.

This module is intentionally isolated from OCR and overlay code. It performs a
single-shot capture only; there is no capture loop and it is never started at
plugin boot.

The Wayland client is implemented with ctypes against ``libwayland-client``.
Only *exported* libwayland symbols are resolved; protocol "generated helpers"
such as ``wl_display.get_registry`` / ``wl_registry.bind`` / ``*_add_listener``
are static inline in C and are reproduced here via the exported core
``wl_proxy_*`` marshalling APIs.

CLI:
    python3 -m capture.gamescope_capture --mode base_plane_only --output /tmp/a.png
    python3 -m capture.gamescope_capture --mode base_plane_only --json
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import select
import socket
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .errors import CaptureError
from .frame import CaptureFrame, validate_image_bytes

# Screenshot types from protocol/gamescope-control.xml.
SCREENSHOT_TYPES = {
    "base_plane_only": 1,
    "all_real_layers": 2,
    "full_composition": 3,
    "screen_buffer": 4,
}
TAKE_SCREENSHOT_OPCODE = 2  # destroy, set_app_target_refresh_cycle, take_screenshot
MIN_SCREENSHOT_VERSION = 3  # take_screenshot + base_plane_only are since v3

# Core wl_display / wl_registry request opcodes.
WL_DISPLAY_GET_REGISTRY = 1
WL_REGISTRY_BIND = 0


def _runtime_base() -> Optional[str]:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not (base and Path(base).is_dir()):
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        candidate = Path(f"/run/user/{uid}")
        if candidate.is_dir():
            base = str(candidate)
    return base if base and Path(base).is_dir() else None


def resolve_wayland_runtime_dir(uid=None, env=None, run_root: str = "/run/user") -> str:
    """Resolve the Wayland runtime dir without relying on shell-only env.

    Order: CLARIFYDECK_WAYLAND_RUNTIME_DIR, valid XDG_RUNTIME_DIR,
    /run/user/<uid>. Fails closed (never hard-codes 1000).
    """
    environment = env if env is not None else os.environ
    override = environment.get("CLARIFYDECK_WAYLAND_RUNTIME_DIR")
    if override:
        return override
    xdg = environment.get("XDG_RUNTIME_DIR")
    if xdg and Path(xdg).is_dir():
        return xdg
    if uid is None and hasattr(os, "getuid"):
        uid = os.getuid()
    if uid is not None:
        candidate = Path(run_root) / str(uid)
        if candidate.is_dir():
            return str(candidate)
    raise CaptureError(
        "wayland_runtime_dir_unavailable",
        "XDG_RUNTIME_DIR unset and /run/user/<uid> missing",
    )


def resolve_gamescope_display(override: Optional[str] = None, env=None) -> str:
    """Resolve the Gamescope Wayland display name (never Steam's normal one)."""
    environment = env if env is not None else os.environ
    env_display = environment.get("GAMESCOPE_WAYLAND_DISPLAY")
    if env_display:
        return env_display
    if override:
        return override
    wayland_display = environment.get("WAYLAND_DISPLAY")
    if wayland_display and "gamescope" in wayland_display:
        return wayland_display
    return "gamescope-0"


def default_output_dir() -> Path:
    override = os.environ.get("CLARIFYDECK_CAPTURE_DIR")
    if override:
        return Path(override)
    base = _runtime_base()
    if base:
        return Path(base) / "clarifydeck" / "capture-test"
    return Path("/tmp") / "clarifydeck" / "capture-test"


def default_capture_dir() -> Path:
    """Directory for transient, internally-owned source screenshots."""
    override = os.environ.get("CLARIFYDECK_CAPTURE_SOURCE_DIR")
    if override:
        return Path(override)
    base = _runtime_base()
    if base:
        return Path(base) / "clarifydeck" / "capture"
    return Path("/tmp") / "clarifydeck" / "capture"


def default_output_path(mode: str = "base_plane_only") -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return default_output_dir() / f"capture-{mode}-{stamp}.png"


def validate_image(path: Path) -> dict:
    """Validate a written capture file. Returns {format,width,height,bytes}."""
    if not path.is_file():
        raise CaptureError("invalid_frame", f"missing file {path}")
    return validate_image_bytes(path.read_bytes())


def _safe_unlink(path: Path) -> None:
    """Remove an internally-owned source artifact, tolerating it being gone."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


# --------------------------------------------------------------------------
# ctypes Wayland ABI types
# --------------------------------------------------------------------------

class _WlMessage(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("signature", ctypes.c_char_p),
        ("types", ctypes.c_void_p),
    ]


class _WlInterface(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("version", ctypes.c_int),
        ("method_count", ctypes.c_int),
        ("methods", ctypes.c_void_p),
        ("event_count", ctypes.c_int),
        ("events", ctypes.c_void_p),
    ]


class _WlArgument(ctypes.Union):
    _fields_ = [
        ("i", ctypes.c_int32),
        ("u", ctypes.c_uint32),
        ("f", ctypes.c_int32),
        ("s", ctypes.c_char_p),
        ("o", ctypes.c_void_p),
        ("n", ctypes.c_uint32),
        ("a", ctypes.c_void_p),
        ("h", ctypes.c_int32),
    ]


_RegistryGlobalFn = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_char_p, ctypes.c_uint
)
_RegistryRemoveFn = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
_FeatureSupportFn = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint
)
_ActiveDisplayFn = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_uint,
    ctypes.c_void_p,
)
_ScreenshotTakenFn = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)
_AppPerfFn = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint
)


class _RegistryListener(ctypes.Structure):
    _fields_ = [("global_", _RegistryGlobalFn), ("global_remove", _RegistryRemoveFn)]


class _ControlListener(ctypes.Structure):
    _fields_ = [
        ("feature_support", _FeatureSupportFn),
        ("active_display_info", _ActiveDisplayFn),
        ("screenshot_taken", _ScreenshotTakenFn),
        ("app_performance_stats", _AppPerfFn),
    ]


def _build_control_interface() -> _WlInterface:
    def msg(name: str, signature: str) -> _WlMessage:
        return _WlMessage(name=name.encode(), signature=signature.encode(), types=None)

    methods = (_WlMessage * 8)(
        msg("destroy", ""),
        msg("set_app_target_refresh_cycle", "uf"),
        msg("take_screenshot", "suu"),
        msg("display_sleep", "uu"),
        msg("set_look", "hhu"),
        msg("unset_look", ""),
        msg("request_app_performance_stats", "u"),
        msg("set_keyboard_layout", "ss"),
    )
    events = (_WlMessage * 4)(
        msg("feature_support", "uuu"),
        msg("active_display_info", "sssua"),
        msg("screenshot_taken", "s"),
        msg("app_performance_stats", "uuu"),
    )
    interface = _WlInterface(
        name=b"gamescope_control",
        version=7,
        method_count=8,
        methods=ctypes.cast(methods, ctypes.c_void_p),
        event_count=4,
        events=ctypes.cast(events, ctypes.c_void_p),
    )
    interface._keepalive = (methods, events)  # type: ignore[attr-defined]
    return interface


class _WaylandClient:
    """Minimal gamescope_control client using only exported libwayland symbols."""

    def __init__(self, display_name: str, log: Callable[[str], None], runtime_dir: Optional[str] = None) -> None:
        self.display_name = display_name
        self._log = log
        self._runtime_dir = runtime_dir
        self.lib = None
        self.display = None
        self.control = None
        self.control_version = 0
        self.features: list[tuple[int, int, int]] = []
        self.screenshot_path: Optional[str] = None
        self._callback_error: Optional[Exception] = None
        self._registry_interface = None
        self._interface = _build_control_interface()
        self._init_callback_wrappers()
        self._log("[capture] wayland callbacks initialized")

    def _init_callback_wrappers(self) -> None:
        """Wrap Python handlers as CFUNCTYPE instances and keep them alive."""
        self._registry_global_cb = _RegistryGlobalFn(self._on_registry_global)
        self._registry_remove_cb = _RegistryRemoveFn(self._on_registry_remove)
        self._feature_support_cb = _FeatureSupportFn(self._on_feature_support)
        self._active_display_cb = _ActiveDisplayFn(self._on_active_display)
        self._screenshot_taken_cb = _ScreenshotTakenFn(self._on_screenshot_taken)
        self._app_perf_cb = _AppPerfFn(self._on_app_perf)
        self._registry_listener = _RegistryListener(
            self._registry_global_cb,
            self._registry_remove_cb,
        )
        self._control_listener = _ControlListener(
            self._feature_support_cb,
            self._active_display_cb,
            self._screenshot_taken_cb,
            self._app_perf_cb,
        )

    # -- setup -------------------------------------------------------------

    def _bind_symbols(self) -> None:
        lib = self.lib
        lib.wl_display_connect.argtypes = [ctypes.c_char_p]
        lib.wl_display_connect.restype = ctypes.c_void_p
        if hasattr(lib, "wl_display_connect_to_fd"):
            lib.wl_display_connect_to_fd.argtypes = [ctypes.c_int]
            lib.wl_display_connect_to_fd.restype = ctypes.c_void_p
        lib.wl_display_disconnect.argtypes = [ctypes.c_void_p]
        lib.wl_display_disconnect.restype = None
        lib.wl_display_roundtrip.argtypes = [ctypes.c_void_p]
        lib.wl_display_roundtrip.restype = ctypes.c_int
        lib.wl_display_flush.argtypes = [ctypes.c_void_p]
        lib.wl_display_flush.restype = ctypes.c_int
        lib.wl_display_get_fd.argtypes = [ctypes.c_void_p]
        lib.wl_display_get_fd.restype = ctypes.c_int
        lib.wl_display_prepare_read.argtypes = [ctypes.c_void_p]
        lib.wl_display_prepare_read.restype = ctypes.c_int
        lib.wl_display_read_events.argtypes = [ctypes.c_void_p]
        lib.wl_display_read_events.restype = ctypes.c_int
        lib.wl_display_dispatch_pending.argtypes = [ctypes.c_void_p]
        lib.wl_display_dispatch_pending.restype = ctypes.c_int
        lib.wl_display_cancel_read.argtypes = [ctypes.c_void_p]
        lib.wl_display_cancel_read.restype = ctypes.c_int

        lib.wl_proxy_add_listener.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.wl_proxy_add_listener.restype = ctypes.c_int
        lib.wl_proxy_get_version.argtypes = [ctypes.c_void_p]
        lib.wl_proxy_get_version.restype = ctypes.c_uint

        if hasattr(lib, "wl_proxy_marshal_array_constructor"):
            lib.wl_proxy_marshal_array_constructor.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.POINTER(_WlArgument),
                ctypes.POINTER(_WlInterface),
            ]
            lib.wl_proxy_marshal_array_constructor.restype = ctypes.c_void_p
        if hasattr(lib, "wl_proxy_marshal_array_constructor_versioned"):
            lib.wl_proxy_marshal_array_constructor_versioned.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.POINTER(_WlArgument),
                ctypes.POINTER(_WlInterface),
                ctypes.c_uint,
            ]
            lib.wl_proxy_marshal_array_constructor_versioned.restype = ctypes.c_void_p
        if hasattr(lib, "wl_proxy_marshal_array_flags"):
            lib.wl_proxy_marshal_array_flags.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.POINTER(_WlArgument),
            ]
            lib.wl_proxy_marshal_array_flags.restype = ctypes.c_void_p
        if hasattr(lib, "wl_proxy_marshal_array"):
            lib.wl_proxy_marshal_array.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.POINTER(_WlArgument),
            ]
            lib.wl_proxy_marshal_array.restype = None

    def connect(self, lib=None) -> None:
        if lib is None:
            found = ctypes.util.find_library("wayland-client")
            candidates = [found, "libwayland-client.so.0", "libwayland-client.so"]
            for candidate in candidates:
                if not candidate:
                    continue
                try:
                    lib = ctypes.CDLL(candidate)
                    break
                except OSError:
                    continue
        if lib is None:
            raise CaptureError("gamescope_socket_not_found", "libwayland-client not found")
        self.lib = lib
        self._bind_symbols()

        if not hasattr(self.lib, "wl_proxy_marshal_array_constructor"):
            raise CaptureError("gamescope_protocol_incompatible", "no array constructor API")

        self._connect_display()
        self._log("[capture] wl_display_connect ok")

        try:
            self._registry_interface = _WlInterface.in_dll(self.lib, "wl_registry_interface")
        except ValueError as exc:
            raise CaptureError("gamescope_control_not_found", f"wl_registry_interface missing: {exc}")

        registry = self._display_get_registry(self.display)
        if not registry:
            raise CaptureError("gamescope_control_not_found", "wayland_registry_create_failed")
        self._registry_proxy = registry
        self._log("[capture] registry proxy created")

        if self._proxy_add_listener(registry, self._registry_listener) != 0:
            raise CaptureError("gamescope_control_not_found", "wayland_listener_registration_failed")
        self._log("[capture] registry listener registered")

        self.lib.wl_display_roundtrip(self.display)
        self.lib.wl_display_roundtrip(self.display)
        if self.control is None:
            raise CaptureError("gamescope_control_not_found", "gamescope_control global missing")

    def _connect_display(self) -> None:
        """Connect using an explicit runtime dir + socket fd.

        The live Decky backend may not inherit XDG_RUNTIME_DIR, so we resolve the
        runtime dir and socket path ourselves and use wl_display_connect_to_fd
        instead of relying on libwayland's global-environment lookup.
        """
        try:
            runtime = self._runtime_dir or resolve_wayland_runtime_dir()
        except CaptureError:
            self._log("[capture] connect failed: wayland runtime dir unavailable")
            raise
        socket_path = Path(runtime) / self.display_name
        exists = socket_path.exists()
        self._log(
            f"[capture] connect display={self.display_name} runtime_dir={runtime} "
            f"socket={socket_path} socket_exists={exists}"
        )

        if hasattr(self.lib, "wl_display_connect_to_fd"):
            if not exists:
                raise CaptureError("gamescope_wayland_socket_missing", f"{socket_path} missing")
            try:
                mode = socket_path.stat().st_mode
            except OSError as exc:
                raise CaptureError("gamescope_wayland_socket_missing", str(exc)) from exc
            if not stat.S_ISSOCK(mode):
                raise CaptureError("gamescope_wayland_socket_missing", f"{socket_path} is not a socket")
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(str(socket_path))
            except OSError as exc:
                client.close()
                raise CaptureError(
                    "gamescope_wayland_connect_failed",
                    f"{type(exc).__name__}: {exc}",
                ) from exc
            self.display = self.lib.wl_display_connect_to_fd(client.detach())
        else:
            self.display = self.lib.wl_display_connect(self.display_name.encode())

        if not self.display:
            raise CaptureError(
                "gamescope_wayland_connect_failed",
                f"cannot connect to {socket_path}",
            )

    def close(self) -> None:
        try:
            if self.display and self.lib:
                self.lib.wl_display_disconnect(self.display)
        except Exception:
            pass
        self.display = None

    # -- exported core API wrappers ---------------------------------------

    def _display_get_registry(self, display_ptr: int) -> int:
        args = (_WlArgument * 1)()
        return self.lib.wl_proxy_marshal_array_constructor(
            display_ptr,
            WL_DISPLAY_GET_REGISTRY,
            args,
            ctypes.byref(self._registry_interface),
        )

    def _proxy_add_listener(self, proxy_ptr: int, listener_struct) -> int:
        return self.lib.wl_proxy_add_listener(
            proxy_ptr,
            ctypes.cast(ctypes.byref(listener_struct), ctypes.c_void_p),
            None,
        )

    def _registry_bind(self, registry_ptr: int, name: int, interface_ptr, interface_name: bytes, version: int) -> int:
        args = (_WlArgument * 4)()
        args[0].u = int(name)
        args[1].s = interface_name
        args[2].u = int(version)
        args[3].n = 0
        return self.lib.wl_proxy_marshal_array_constructor_versioned(
            registry_ptr,
            WL_REGISTRY_BIND,
            args,
            interface_ptr,
            int(version),
        )

    # -- registry / control callbacks -------------------------------------

    def _on_registry_global(self, _data, registry, name, interface, version) -> None:
        try:
            if interface == b"gamescope_control":
                bind_version = min(int(version), 7)
                self.control = self._registry_bind(
                    registry,
                    int(name),
                    ctypes.byref(self._interface),
                    self._interface.name,
                    bind_version,
                )
                self.control_version = bind_version
                if self.control:
                    self._proxy_add_listener(self.control, self._control_listener)
        except Exception as exc:  # never let exceptions cross the C boundary
            self._callback_error = exc
            self._log(f"[capture][ERROR] callback registry_global: {exc}")

    def _on_registry_remove(self, _data, _registry, _name) -> None:
        return

    def _on_feature_support(self, _data, _control, feature, version, flags) -> None:
        try:
            self.features.append((int(feature), int(version), int(flags)))
        except Exception as exc:
            self._callback_error = exc
            self._log(f"[capture][ERROR] callback feature_support: {exc}")

    def _on_active_display(self, _data, _control, _connector, _make, _model, _flags, _rates) -> None:
        return

    def _on_screenshot_taken(self, _data, _control, path) -> None:
        try:
            self.screenshot_path = path.decode() if path else None
        except Exception as exc:
            self._callback_error = exc
            self._log(f"[capture][ERROR] callback screenshot_taken: {exc}")

    def _on_app_perf(self, _data, _control, _app_id, _lo, _hi) -> None:
        return

    # -- request -----------------------------------------------------------

    def _marshal_take_screenshot(self, path: str, type_id: int, flags: int) -> None:
        path_bytes = path.encode()
        args = (_WlArgument * 3)()
        args[0].s = path_bytes
        args[1].u = int(type_id)
        args[2].u = int(flags)
        if hasattr(self.lib, "wl_proxy_marshal_array_flags"):
            self.lib.wl_proxy_marshal_array_flags(
                self.control,
                TAKE_SCREENSHOT_OPCODE,
                None,
                self.control_version,
                0,
                args,
            )
        elif hasattr(self.lib, "wl_proxy_marshal_array"):
            self.lib.wl_proxy_marshal_array(self.control, TAKE_SCREENSHOT_OPCODE, args)
        else:
            raise CaptureError("gamescope_protocol_incompatible", "no array marshal API")

    def _pump(self, deadline: float) -> bool:
        fd = self.lib.wl_display_get_fd(self.display)
        while time.time() < deadline:
            while self.lib.wl_display_prepare_read(self.display) != 0:
                self.lib.wl_display_dispatch_pending(self.display)
            self.lib.wl_display_flush(self.display)
            remaining = deadline - time.time()
            if remaining <= 0:
                self.lib.wl_display_cancel_read(self.display)
                break
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except OSError:
                self.lib.wl_display_cancel_read(self.display)
                break
            if not ready:
                self.lib.wl_display_cancel_read(self.display)
                continue
            if self.lib.wl_display_read_events(self.display) < 0:
                raise CaptureError("capture_failed", "wl_display_read_events failed")
            self.lib.wl_display_dispatch_pending(self.display)
            if self.screenshot_path:
                return True
        return bool(self.screenshot_path)

    def take_screenshot(self, path: str, type_id: int, timeout: float) -> str:
        self.screenshot_path = None
        self._marshal_take_screenshot(path, type_id, 0)
        self.lib.wl_display_flush(self.display)
        if not self._pump(time.time() + timeout):
            raise CaptureError("capture_timeout", "screenshot_taken not received")
        return self.screenshot_path or path


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

class GamescopeCapture:
    def __init__(
        self,
        display: Optional[str] = None,
        logger: Optional[Callable[[str], None]] = None,
        runtime_dir: Optional[str] = None,
    ) -> None:
        self.display_name = resolve_gamescope_display(display)
        self._log = logger or (lambda message: None)
        self._runtime_dir = runtime_dir
        self._sequence = 0

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _client(self) -> "_WaylandClient":
        return _WaylandClient(self.display_name, self._log, runtime_dir=self._runtime_dir)

    def probe(self, timeout: float = 3.0) -> dict:
        self._log(f"[capture] probe display={self.display_name}")
        client = self._client()
        try:
            client.connect()
        except CaptureError as exc:
            self._log(f"[capture][ERROR] stage=probe reason={exc.code}")
            return {"available": False, "display": self.display_name, "error": exc.code}
        result = {
            "available": True,
            "display": self.display_name,
            "version": client.control_version,
            "features": client.features,
            "screenshot_supported": client.control_version >= MIN_SCREENSHOT_VERSION,
            "base_plane_only_supported": client.control_version >= MIN_SCREENSHOT_VERSION,
        }
        client.close()
        self._log(f"[capture] protocol_version={result['version']}")
        return result

    def capture(
        self,
        output: Optional[Path] = None,
        mode: str = "base_plane_only",
        timeout: float = 5.0,
        debug_copy: Optional[Path] = None,
    ) -> dict:
        if mode not in SCREENSHOT_TYPES:
            raise CaptureError("invalid_mode", mode)
        type_id = SCREENSHOT_TYPES[mode]

        # Ownership model: a caller-supplied output path is never deleted by
        # ClarifyDeck. An internally-created path is ClarifyDeck-owned, but we
        # still keep it by default so a one-shot capture can always be inspected.
        owns_output = output is None
        out_path = Path(output) if output else default_output_path(mode)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        started = time.time()
        client = self._client()
        try:
            client.connect()
            if client.control_version < MIN_SCREENSHOT_VERSION:
                raise CaptureError("gamescope_protocol_incompatible", f"version={client.control_version}")
            self._log(f"[capture] mode={mode} type={type_id}")
            client.take_screenshot(str(out_path), type_id, timeout)
        except CaptureError:
            client.close()
            raise
        finally:
            client.close()

        try:
            data = out_path.read_bytes()
        except OSError as exc:
            raise CaptureError("invalid_frame", f"source read failed: {exc}") from exc
        info = validate_image_bytes(data)
        if debug_copy is not None:
            debug_path = Path(debug_copy)
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_bytes(data)
        elapsed = int((time.time() - started) * 1000)
        self._log(
            f"[capture] frame={info['width']}x{info['height']} format={info['format']} "
            f"bytes={info['bytes']} output={out_path} elapsed_ms={elapsed}"
        )
        self._log(
            f"[capture] output retained path={out_path} "
            f"owner={'internal' if owns_output else 'caller'}"
        )
        return {
            "ok": True,
            "backend": "gamescope_control",
            "mode": mode,
            "display": self.display_name,
            "width": info["width"],
            "height": info["height"],
            "format": info["format"],
            "bytes": info["bytes"],
            "output": str(out_path),
            "output_owned": owns_output,
            "debug_copy": str(debug_copy) if debug_copy else None,
            "elapsed_ms": elapsed,
        }

    def capture_frame(
        self,
        mode: str = "base_plane_only",
        timeout: float = 5.0,
        debug_copy: Optional[Path] = None,
    ) -> CaptureFrame:
        """Capture one frame into memory; independent of the source file lifetime."""
        if mode not in SCREENSHOT_TYPES:
            raise CaptureError("invalid_mode", mode)
        type_id = SCREENSHOT_TYPES[mode]

        sequence = self._next_sequence()
        source = default_capture_dir() / f"capture-{os.getpid()}-{sequence}-{uuid.uuid4().hex[:8]}.png"
        source.parent.mkdir(parents=True, exist_ok=True)

        started = time.time()
        client = self._client()
        try:
            client.connect()
            if client.control_version < MIN_SCREENSHOT_VERSION:
                raise CaptureError("gamescope_protocol_incompatible", f"version={client.control_version}")
            self._log(f"[capture] frame mode={mode} type={type_id} seq={sequence}")
            client.take_screenshot(str(source), type_id, timeout)
        finally:
            client.close()

        try:
            data = source.read_bytes()
        except OSError as exc:
            raise CaptureError("invalid_frame", f"source read failed: {exc}") from exc
        finally:
            _safe_unlink(source)

        frame = CaptureFrame.from_png(
            data,
            sequence=sequence,
            source_backend="gamescope_control",
            source_mode=mode,
            source_path=str(source),
        )
        if debug_copy is not None:
            debug_path = Path(debug_copy)
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_bytes(frame.encoded_bytes)
            self._log(f"[capture] debug copy written path={debug_path}")
        elapsed = int((time.time() - started) * 1000)
        self._log(
            f"[capture] frame seq={sequence} {frame.width}x{frame.height} "
            f"bytes={len(frame.encoded_bytes)} elapsed_ms={elapsed}"
        )
        return frame

    def capture_base_plane(
        self,
        output: Optional[Path] = None,
        timeout: float = 5.0,
        debug_copy: Optional[Path] = None,
    ) -> dict:
        return self.capture(output=output, mode="base_plane_only", timeout=timeout, debug_copy=debug_copy)

    def capture_base_plane_frame(
        self,
        timeout: float = 5.0,
        debug_copy: Optional[Path] = None,
    ) -> CaptureFrame:
        return self.capture_frame(mode="base_plane_only", timeout=timeout, debug_copy=debug_copy)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck gamescope base-plane capture test")
    parser.add_argument("--mode", default="base_plane_only", choices=sorted(SCREENSHOT_TYPES))
    parser.add_argument("--output", default=None)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--frame", action="store_true")
    parser.add_argument("--debug-copy", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    def log(message: str) -> None:
        sys.stderr.write(message + "\n")

    capture = GamescopeCapture(logger=log)
    try:
        if args.probe:
            result = capture.probe()
            payload = {"ok": bool(result.get("available")), **result}
        elif args.frame:
            frame = capture.capture_frame(
                mode=args.mode,
                timeout=args.timeout,
                debug_copy=Path(args.debug_copy) if args.debug_copy else None,
            )
            payload = {
                "ok": True,
                "backend": frame.source_backend,
                "mode": frame.source_mode,
                "sequence": frame.sequence,
                "width": frame.width,
                "height": frame.height,
                "format": frame.format,
                "bytes": len(frame.encoded_bytes),
                "source_path": frame.source_path,
                "debug_copy": args.debug_copy,
            }
        else:
            payload = capture.capture(
                output=Path(args.output) if args.output else None,
                mode=args.mode,
                timeout=args.timeout,
                debug_copy=Path(args.debug_copy) if args.debug_copy else None,
            )
    except CaptureError as exc:
        payload = {"ok": False, "error": exc.code, "mode": args.mode}

    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
