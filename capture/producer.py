"""Bounded low-rate capture producer lifecycle.

Explicit START -> sequential capture loop -> CaptureFrame -> LatestFrameQueue
-> explicit STOP. No automatic start, no automatic restart, no overlapping
captures, deadline-based cadence.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from .frame import CaptureFrame
from .latest_frame_queue import LatestFrameQueue


class ProducerState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


@dataclass
class CaptureProducerStats:
    state: str = "STOPPED"
    target_fps: float = 1.0
    frames_attempted: int = 0
    frames_succeeded: int = 0
    frames_failed: int = 0
    last_sequence: Optional[int] = None
    last_capture_ms: Optional[float] = None
    avg_capture_ms: Optional[float] = None
    consecutive_failures: int = 0
    late_ticks: int = 0
    last_error: Optional[str] = None
    last_error_category: Optional[str] = None
    capture_cancellations: int = 0
    shutdown_discarded_frames: int = 0
    started_monotonic: Optional[float] = None


async def _default_runner(fn: Callable[..., CaptureFrame], *args: Any) -> CaptureFrame:
    return await asyncio.to_thread(fn, *args)


class CaptureProducer:
    MIN_FPS = 0.2
    MAX_FPS = 2.0
    MAX_CONSECUTIVE_FAILURES = 5
    STOP_TIMEOUT_SECONDS = 3.0

    def __init__(
        self,
        capture,
        queue: LatestFrameQueue,
        *,
        target_fps: float = 1.0,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
        capture_timeout: float = 5.0,
        logger: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        runner: Callable[..., Awaitable[CaptureFrame]] = _default_runner,
    ) -> None:
        self._capture = capture
        self._queue = queue
        self._target_fps = self._clamp_fps(target_fps)
        self._max_failures = max(1, int(max_consecutive_failures))
        self._capture_timeout = capture_timeout
        self._log = logger or (lambda message: None)
        self._clock = clock
        self._sleep = sleep
        self._runner = runner
        self._state = ProducerState.STOPPED
        self._task: Optional[asyncio.Task] = None
        self._stop_requested = False
        self._lock = asyncio.Lock()
        self._stats = CaptureProducerStats(state=ProducerState.STOPPED.value, target_fps=self._target_fps)
        self._capture_total_ms = 0.0

    @classmethod
    def _clamp_fps(cls, fps: float) -> float:
        try:
            value = float(fps)
        except (TypeError, ValueError):
            value = 1.0
        return max(cls.MIN_FPS, min(cls.MAX_FPS, value))

    # -- lifecycle ---------------------------------------------------------

    async def start(self, target_fps: Optional[float] = None) -> dict:
        async with self._lock:
            if self._state in (ProducerState.RUNNING, ProducerState.STARTING):
                return {**self.status(), "detail": "already_running"}
            if target_fps is not None:
                self._target_fps = self._clamp_fps(target_fps)
            self._state = ProducerState.STARTING
            self._stats.state = ProducerState.STARTING.value
            self._stats.target_fps = self._target_fps
            self._stats.consecutive_failures = 0
            self._stats.last_error = None
            self._stats.last_error_category = None
            self._stats.started_monotonic = self._clock()
            self._stop_requested = False
            self._task = asyncio.create_task(self._run(), name="clarifydeck-capture-producer")
        await asyncio.sleep(0)  # let the loop actually start before reporting
        self._log(f"[capture-producer] start target_fps={self._target_fps}")
        return self.status()

    async def stop(self, timeout: Optional[float] = None) -> dict:
        async with self._lock:
            if self._state == ProducerState.STOPPED or self._task is None:
                self._state = ProducerState.STOPPED
                self._stats.state = ProducerState.STOPPED.value
                return {**self.status(), "detail": "already_stopped"}
            self._state = ProducerState.STOPPING
            self._stats.state = ProducerState.STOPPING.value
            self._stop_requested = True
            task = self._task
        self._log("[capture-producer] stop requested")
        try:
            await asyncio.wait_for(task, timeout=timeout or self.STOP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            async with self._lock:
                self._task = None
                self._state = ProducerState.FAILED
                self._stats.state = ProducerState.FAILED.value
                self._stats.last_error = "shutdown_timeout"
            self._log("[capture-producer] failed consecutive_failures=shutdown_timeout")
            return self.status()
        except asyncio.CancelledError:
            pass
        async with self._lock:
            self._task = None
            if self._state != ProducerState.FAILED:
                self._state = ProducerState.STOPPED
            self._stats.state = self._state.value
        self._log(f"[capture-producer] stopped frames={self._stats.frames_succeeded}")
        return self.status()

    async def reset(self) -> dict:
        """Clear a FAILED state so the producer can be started again explicitly."""
        async with self._lock:
            if self._task is not None and not self._task.done():
                return {**self.status(), "detail": "still_running"}
            self._task = None
            self._state = ProducerState.STOPPED
            self._stats.state = ProducerState.STOPPED.value
            self._stats.consecutive_failures = 0
            self._stats.last_error = None
            self._stats.last_error_category = None
        return self.status()

    # -- loop --------------------------------------------------------------

    async def _run(self) -> None:
        self._state = ProducerState.RUNNING
        self._stats.state = ProducerState.RUNNING.value
        self._log("[capture-producer] running")
        period = 1.0 / self._target_fps
        next_deadline = self._clock()
        try:
            while not self._stop_requested:
                next_deadline += period
                started = self._clock()
                frame: Optional[CaptureFrame] = None
                try:
                    frame = await self._runner(
                        self._capture.capture_frame, "base_plane_only", self._capture_timeout
                    )
                except asyncio.CancelledError:
                    # Expected shutdown cancellation: never a capture failure.
                    self._stats.capture_cancellations += 1
                    self._stats.last_error_category = "stop_cancellation"
                    self._log("[capture-producer] cancelled reason=stop_requested")
                    raise
                except Exception as exc:  # one failure must not kill the loop
                    self._stats.frames_attempted += 1
                    if self._stop_requested:
                        # Stop was requested while this capture was in flight: this is
                        # shutdown bookkeeping, not a genuine capture failure.
                        self._stats.capture_cancellations += 1
                        self._stats.last_error_category = "stop_during_capture"
                        self._stats.last_error = f"{type(exc).__name__}: {exc}"
                        self._log(
                            f"[capture-producer] shutdown_discard reason=stop_during_capture "
                            f"error={self._stats.last_error}"
                        )
                    else:
                        self._stats.frames_failed += 1
                        self._stats.consecutive_failures += 1
                        self._stats.last_error_category = "capture_operation_error"
                        self._stats.last_error = f"{type(exc).__name__}: {exc}"
                        self._queue.note_capture_error()
                        self._log(f"[capture-producer] capture failed: {self._stats.last_error}")
                        if self._stats.consecutive_failures >= self._max_failures:
                            self._state = ProducerState.FAILED
                            self._stats.state = ProducerState.FAILED.value
                            self._log(
                                f"[capture-producer] failed consecutive_failures="
                                f"{self._stats.consecutive_failures}"
                            )
                            return
                else:
                    elapsed_ms = (self._clock() - started) * 1000.0
                    self._stats.frames_attempted += 1
                    self._stats.frames_succeeded += 1
                    self._stats.consecutive_failures = 0
                    self._stats.last_sequence = frame.sequence
                    self._stats.last_capture_ms = round(elapsed_ms, 1)
                    self._capture_total_ms += elapsed_ms
                    self._stats.avg_capture_ms = round(
                        self._capture_total_ms / self._stats.frames_succeeded, 1
                    )
                    if self._stop_requested:
                        # Successful capture but shutdown already requested: discard
                        # it (do not publish) and account separately.
                        self._stats.shutdown_discarded_frames += 1
                        self._log(f"[capture-producer] shutdown_discard seq={frame.sequence}")
                        break
                    await self._queue.put_latest(frame)

                if self._stop_requested:
                    break
                now = self._clock()
                if now > next_deadline:
                    self._stats.late_ticks += 1
                    next_deadline = now
                delay = max(0.0, next_deadline - self._clock())
                await self._sleep(delay)
        finally:
            if self._state != ProducerState.FAILED:
                self._state = ProducerState.STOPPED
                self._stats.state = ProducerState.STOPPED.value

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        return {
            "ok": True,
            "state": self._state.value,
            "target_fps": self._target_fps,
            "frames_attempted": self._stats.frames_attempted,
            "frames_succeeded": self._stats.frames_succeeded,
            "frames_failed": self._stats.frames_failed,
            "last_sequence": self._stats.last_sequence,
            "last_capture_ms": self._stats.last_capture_ms,
            "avg_capture_ms": self._stats.avg_capture_ms,
            "consecutive_failures": self._stats.consecutive_failures,
            "late_ticks": self._stats.late_ticks,
            "last_error": self._stats.last_error,
            "last_error_category": self._stats.last_error_category,
            "capture_cancellations": self._stats.capture_cancellations,
            "shutdown_discarded_frames": self._stats.shutdown_discarded_frames,
            "started_monotonic": self._stats.started_monotonic,
            "queue": self._queue.stats().__dict__,
        }
