"""ClarifyDeck capture error type (shared by frame/transport/queue)."""

from __future__ import annotations


class CaptureError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
