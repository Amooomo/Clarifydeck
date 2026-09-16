"""Linux parent-death safety (Phase 2I.2.2). Pure stdlib.

Guarantees a worker cannot outlive its owning backend:

- primary: ``prctl(PR_SET_PDEATHSIG, SIGINT)`` via libc, so the kernel signals the
  worker when the parent **thread** dies (works even while the parent is a zombie,
  which is why the previous ``/proc/<pid>`` + ``kill(pid, 0)`` check failed);
- race handling: after arming, verify ``os.getppid() == expected`` and fail closed
  if the parent already died;
- secondary: ``parent_changed`` checks the parent **relationship** (``getppid``),
  never bare PID existence, so PID reuse cannot mask a dead owner.

Must not import rapidocr / onnxruntime / numpy / cv2 / omegaconf / antlr4.
"""

from __future__ import annotations

import ctypes
import os
import signal
from typing import Callable, Optional

PR_SET_PDEATHSIG = 1


class ParentDeathError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def arm_parent_death_signal(sig: int = signal.SIGINT) -> tuple[bool, Optional[str]]:
    """Install PR_SET_PDEATHSIG. Returns (armed, reason)."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except Exception as exc:
        return False, f"libc_load_failed:{exc}"
    if not hasattr(libc, "prctl"):
        return False, "prctl_unavailable"
    try:
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.prctl.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = libc.prctl(PR_SET_PDEATHSIG, int(sig), 0, 0, 0)
    except Exception as exc:  # pragma: no cover - platform dependent
        return False, f"prctl_exception:{exc}"
    if result != 0:
        return False, f"prctl_failed:{ctypes.get_errno()}"
    return True, None


def parent_changed(expected_pid: int, getppid: Callable[[], int] = os.getppid) -> bool:
    """True when the current parent is no longer the expected owner."""
    try:
        return int(getppid()) != int(expected_pid)
    except Exception:
        return True


def setup_parent_death(
    expected_pid: Optional[int],
    sig: int = signal.SIGINT,
    getppid: Callable[[], int] = os.getppid,
    arm: Callable[[int], tuple[bool, Optional[str]]] = arm_parent_death_signal,
) -> dict:
    """Arm parent-death protection and verify the parent relationship.

    Raises ParentDeathError for an invalid expected pid or the
    parent-died-before-arm race. Returns ``{"armed": bool, "reason": str|None}``.
    """
    if expected_pid is None:
        return {"armed": False, "reason": "no_parent_pid"}
    expected = int(expected_pid)
    if expected <= 1:
        raise ParentDeathError("invalid_parent_pid", str(expected))
    armed, reason = arm(sig)
    if parent_changed(expected, getppid):
        # parent died before/while we armed: fail closed immediately
        raise ParentDeathError("parent_changed", f"expected={expected} actual={getppid()}")
    return {"armed": armed, "reason": reason}
