"""Capture backend abstraction + prototype selector (Phase 2N.6).

Two interchangeable frame sources feed the existing capture producer/OCR
pipeline:

- ``ScreenshotCaptureBackend`` wraps the validated Gamescope screenshot path
  (default, unchanged);
- ``PipeWireCaptureBackend`` streams the Gamescope PipeWire node through
  GStreamer and converts NV12 to RGBA in memory.

Selection is development/runtime only via ``CLARIFYDECK_CAPTURE_BACKEND``
(default ``screenshot``); there is no QAM UI and no persistence. An active
PipeWire session never falls back to screenshots.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import CaptureError
from .frame import CaptureFrame, DecodedFrame
from .gamescope_capture import GamescopeCapture
from .pipewire_capture import (
    DEFAULT_FIRST_SAMPLE_TIMEOUT_SEC,
    DEFAULT_KEEPALIVE_MS,
    DEFAULT_RETRY_INTERVAL_SEC,
    DEFAULT_RETRY_WINDOW_SEC,
    PIPEWIRE_TARGET_DEFAULT,
    PipeWireFrame,
    create_gst_adapter,
    ensure_xdg_runtime_dir,
)

CAPTURE_BACKEND_ENV = "CLARIFYDECK_CAPTURE_BACKEND"
DEFAULT_CAPTURE_BACKEND = "screenshot"
PIPEWIRE_BACKEND = "pipewire"

_SCREENSHOT_ALIASES = {"", "screenshot", "gamescope", "gamescope_control"}
_PIPEWIRE_ALIASES = {"pipewire", "pipewire-gst", "pipewire_gst", "gst"}


def normalize_backend_name(value: Optional[str]) -> str:
    """Map an explicit/env selector to ``screenshot`` or ``pipewire``."""
    name = (value or "").strip().lower()
    if name in _SCREENSHOT_ALIASES:
        return DEFAULT_CAPTURE_BACKEND
    if name in _PIPEWIRE_ALIASES:
        return PIPEWIRE_BACKEND
    raise CaptureError("invalid_capture_backend", value or "")


class ScreenshotCaptureBackend:
    """Default backend: the existing, unchanged Gamescope screenshot capture."""

    name = DEFAULT_CAPTURE_BACKEND

    def __init__(
        self,
        capture: Optional[GamescopeCapture] = None,
        *,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._capture = capture or GamescopeCapture(logger=logger)
        self._log = logger or (lambda message: None)
        self._running = False

    def start(self) -> dict:
        self._running = True
        self._log("[capture-backend] backend=screenshot")
        return {"ok": True, "backend": self.name}

    def stop(self) -> dict:
        self._running = False
        return {"ok": True, "backend": self.name}

    def capture_frame(self, mode: str = "base_plane_only", timeout: float = 5.0) -> CaptureFrame:
        return self._capture.capture_frame(mode=mode, timeout=timeout)

    def status(self) -> dict:
        return {"backend": self.name, "running": self._running}


@dataclass
class PipeWireBackendStats:
    attempts: int = 0
    frames_pulled: int = 0
    pull_errors: int = 0
    startup_ms: Optional[float] = None
    last_pull_ms: Optional[float] = None
    last_map_ms: Optional[float] = None
    last_conversion_ms: Optional[float] = None
    source_width: Optional[int] = None
    source_height: Optional[int] = None
    source_format: Optional[str] = None
    last_error: Optional[str] = None


class PipeWireCaptureBackend:
    """Prototype backend: continuous Gamescope PipeWire stream -> RGBA frames."""

    name = PIPEWIRE_BACKEND

    def __init__(
        self,
        *,
        adapter=None,
        adapter_factory: Optional[Callable[..., object]] = None,
        runtime_env: Optional[Callable[..., dict]] = None,
        env=None,
        logger: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        target: str = PIPEWIRE_TARGET_DEFAULT,
        keepalive_ms: int = DEFAULT_KEEPALIVE_MS,
        first_sample_timeout: float = DEFAULT_FIRST_SAMPLE_TIMEOUT_SEC,
        retry_window: float = DEFAULT_RETRY_WINDOW_SEC,
        retry_interval: float = DEFAULT_RETRY_INTERVAL_SEC,
    ) -> None:
        self._adapter = adapter
        self._adapter_factory = adapter_factory or create_gst_adapter
        self._runtime_env = runtime_env or ensure_xdg_runtime_dir
        self._env = env
        self._owns_adapter = adapter is None
        self._log = logger or (lambda message: None)
        self._clock = clock
        self._sleep = sleep
        self._target = target
        self._keepalive_ms = int(keepalive_ms)
        self._first_sample_timeout = float(first_sample_timeout)
        self._retry_window = float(retry_window)
        self._retry_interval = float(retry_interval)
        self._state = "STOPPED"
        self._stop_requested = False
        self._sequence = 0
        self._first_frame_logged = False
        self._stats = PipeWireBackendStats()

    # -- adapter -----------------------------------------------------------

    def _ensure_adapter(self):
        if self._adapter is None:
            self._adapter = self._adapter_factory(
                target=self._target,
                logger=self._log,
                clock=self._clock,
                keepalive_ms=self._keepalive_ms,
                first_sample_timeout=self._first_sample_timeout,
            )
        return self._adapter

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> dict:
        # Phase 2N.6.2: the Decky loader runs as root without XDG_RUNTIME_DIR;
        # this unprivileged worker must resolve its own runtime dir before
        # GStreamer/PipeWire initialize. Only applies when we own the real
        # adapter (injected test adapters are host-independent).
        if self._owns_adapter:
            self._runtime_env(env=self._env, logger=self._log)
        adapter = self._ensure_adapter()
        self._stop_requested = False
        deadline = self._clock() + max(0.0, self._retry_window)
        while True:
            if self._stop_requested:
                adapter.stop()
                self._state = "STOPPED"
                raise CaptureError("pipewire_start_cancelled", "stop requested during startup")
            self._stats.attempts += 1
            try:
                info = adapter.start_attempt()
            except CaptureError as exc:
                self._stats.last_error = f"{exc.code}: {exc}"
                adapter.stop()
                if self._stop_requested:
                    self._state = "STOPPED"
                    raise CaptureError("pipewire_start_cancelled", "stop requested during startup") from exc
                self._log(
                    f"[capture-pipewire] start attempt={self._stats.attempts} failed "
                    f"error={self._stats.last_error}"
                )
                if self._clock() >= deadline:
                    self._state = "FAILED"
                    self._log(
                        f"[capture-pipewire] start failed attempts={self._stats.attempts} "
                        f"error={self._stats.last_error}"
                    )
                    raise CaptureError(
                        "pipewire_unavailable",
                        f"gamescope source not ready after {self._stats.attempts} attempts: "
                        f"{self._stats.last_error}",
                    ) from exc
                self._sleep(min(self._retry_interval, max(0.0, deadline - self._clock())))
                continue
            self._state = "PLAYING"
            self._stats.startup_ms = info.get("startup_ms")
            self._stats.source_width = info.get("source_width")
            self._stats.source_height = info.get("source_height")
            self._stats.source_format = info.get("source_format")
            self._stats.last_error = None
            self._log(
                f"[capture-pipewire] backend=pipewire target={self._target} state=PLAYING "
                f"caps={self._stats.source_width}x{self._stats.source_height} "
                f"format={self._stats.source_format} startup_ms={self._stats.startup_ms} "
                f"attempts={self._stats.attempts}"
            )
            return self.status()

    def stop(self) -> dict:
        self._stop_requested = True
        adapter = self._adapter
        if adapter is not None:
            try:
                adapter.stop()
            except Exception as exc:  # cleanup must never raise during shutdown
                self._log(f"[capture-pipewire] stop error={type(exc).__name__}: {exc}")
        self._state = "STOPPED"
        self._log(
            f"[capture-pipewire] stopped frames_pulled={self._stats.frames_pulled} "
            f"attempts={self._stats.attempts}"
        )
        return self.status()

    # -- frames ------------------------------------------------------------

    def capture_frame(self, mode: str = "base_plane_only", timeout: float = 5.0) -> DecodedFrame:
        if self._state != "PLAYING":
            raise CaptureError("pipewire_not_started", self._state)
        adapter = self._adapter
        if adapter is None:
            raise CaptureError("pipewire_not_started", "no adapter")
        error = adapter.poll_error()
        if error is not None:
            self._state = "FAILED"
            self._stats.pull_errors += 1
            self._stats.last_error = error
            self._log(f"[capture-pipewire] stream error={error}")
            raise CaptureError("pipewire_stream_error", error)

        pull_started = self._clock()
        frame: Optional[PipeWireFrame] = adapter.try_pull_sample(timeout)
        pull_ms = round((self._clock() - pull_started) * 1000.0, 3)
        if frame is None:
            error = adapter.poll_error()
            if error is not None:
                self._state = "FAILED"
                self._stats.pull_errors += 1
                self._stats.last_error = error
                self._log(f"[capture-pipewire] stream error={error}")
                raise CaptureError("pipewire_stream_error", error)
            self._stats.pull_errors += 1
            self._stats.last_pull_ms = pull_ms
            self._log(f"[capture-pipewire] no frame within {timeout}s pull_ms={pull_ms}")
            raise CaptureError("pipewire_no_frame", f"no sample within {timeout}s")

        self._sequence += 1
        self._stats.frames_pulled += 1
        self._stats.last_pull_ms = pull_ms
        self._stats.last_map_ms = frame.map_ms
        self._stats.last_conversion_ms = frame.conversion_ms
        self._stats.source_width = frame.source_width
        self._stats.source_height = frame.source_height
        self._stats.source_format = frame.format
        if not self._first_frame_logged:
            self._first_frame_logged = True
            self._log(
                f"[capture-pipewire] frame seq={self._sequence} "
                f"{frame.width}x{frame.height} format={frame.format} "
                f"pull_ms={pull_ms} map_ms={frame.map_ms} conversion_ms={frame.conversion_ms}"
            )
        return DecodedFrame(
            width=frame.width,
            height=frame.height,
            format="rgba",
            rgba=frame.rgba,
            captured_monotonic=frame.captured_monotonic,
            captured_wall_time=time.time(),
            source_backend=PIPEWIRE_BACKEND,
            source_mode="gamescope_pipewire",
            sequence=self._sequence,
            decode_ms=frame.conversion_ms,
            source_format=frame.format,
            source_width=frame.source_width,
            source_height=frame.source_height,
            capture_pull_ms=pull_ms,
            conversion_ms=frame.conversion_ms,
        )

    def status(self) -> dict:
        adapter_status = self._adapter.status() if self._adapter is not None else {}
        return {
            "backend": self.name,
            "state": self._state,
            "target": self._target,
            "attempts": self._stats.attempts,
            "frames_pulled": self._stats.frames_pulled,
            "pull_errors": self._stats.pull_errors,
            "startup_ms": self._stats.startup_ms,
            "last_pull_ms": self._stats.last_pull_ms,
            "last_map_ms": self._stats.last_map_ms,
            "last_conversion_ms": self._stats.last_conversion_ms,
            "source_width": self._stats.source_width,
            "source_height": self._stats.source_height,
            "source_format": self._stats.source_format,
            "last_error": self._stats.last_error,
            "adapter": adapter_status,
        }


def resolve_capture_backend(
    name: Optional[str] = None,
    *,
    logger: Optional[Callable[[str], None]] = None,
    env=None,
    **kwargs,
):
    """Select the capture backend (explicit name overrides env; default screenshot)."""
    environment = os.environ if env is None else env
    selected = normalize_backend_name(name if name is not None else environment.get(CAPTURE_BACKEND_ENV))
    if selected == PIPEWIRE_BACKEND:
        return PipeWireCaptureBackend(logger=logger, **kwargs)
    return ScreenshotCaptureBackend(logger=logger, **kwargs)
