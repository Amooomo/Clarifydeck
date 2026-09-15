"""ClarifyDeck backend background-leader lease.

Only one backend instance may run background services (capture loop, OCR loop,
OverlayManager, renderer). All other instances stay in standby and only serve
basic RPC/status. The lease is a kernel flock so it is released automatically
when the owning process dies; no PID file validity logic is involved.

The lock lives in the deck user's runtime dir (shared with the renderer/overlay),
never in a root-only path. A genuine lock contention is reported as BUSY; any
other failure (mkdir/open/permission) is reported as ERROR so a configuration
bug is never mistaken for a duplicate backend.
"""

from __future__ import annotations

import errno
import os
from enum import Enum
from pathlib import Path
from typing import Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Linux (local tests)
    fcntl = None  # type: ignore[assignment]

try:
    from overlay import protocol
except Exception:  # pragma: no cover - import order safety
    protocol = None  # type: ignore[assignment]


class LeaderAcquireResult(str, Enum):
    ACQUIRED = "ACQUIRED"
    BUSY = "BUSY"
    ERROR = "ERROR"


def _fallback_runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and Path(base).is_dir():
        return Path(base) / "clarifydeck"
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    candidate = Path(f"/run/user/{uid}")
    if candidate.is_dir():
        return candidate / "clarifydeck"
    return Path("/tmp") / "clarifydeck"


def runtime_dir() -> Path:
    if protocol is not None:
        return protocol.runtime_dir()
    return _fallback_runtime_dir()


def leader_lock_path() -> Path:
    override = os.environ.get("CLARIFYDECK_LEADER_LOCK")
    if override:
        return Path(override)
    return runtime_dir() / "backend-leader.lock"


class BackgroundLeaderLease:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or leader_lock_path()
        self._fh = None
        self.last_stage: Optional[str] = None
        self.last_error: Optional[str] = None

    def try_acquire(self) -> LeaderAcquireResult:
        if self._fh is not None:
            return LeaderAcquireResult.ACQUIRED

        if fcntl is None:
            # Non-Linux: pretend to be leader so local tests still run.
            self._fh = open(os.devnull, "a+")
            return LeaderAcquireResult.ACQUIRED

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
        except OSError as exc:
            self.last_stage = "mkdir"
            self.last_error = f"{type(exc).__name__}: {exc}"
            return LeaderAcquireResult.ERROR

        try:
            fh = open(self.path, "a+")
        except OSError as exc:
            self.last_stage = "open"
            self.last_error = f"{type(exc).__name__}: {exc}"
            return LeaderAcquireResult.ERROR

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            try:
                fh.close()
            except OSError:
                pass
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                self.last_stage = "flock"
                self.last_error = "busy"
                return LeaderAcquireResult.BUSY
            self.last_stage = "flock"
            self.last_error = f"{type(exc).__name__}: {exc}"
            return LeaderAcquireResult.ERROR

        try:
            os.set_inheritable(fh.fileno(), False)
        except OSError:
            pass
        self._fh = fh
        self.last_stage = None
        self.last_error = None
        return LeaderAcquireResult.ACQUIRED

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None

    def held(self) -> bool:
        return self._fh is not None
