"""StableTextEvent transport protocol v1 (Phase 2I.1).

Pure stdlib only: this module must never import RapidOCR / numpy / cv2 /
onnxruntime / omegaconf / antlr4. It defines the newline-delimited JSON envelope
exchanged between the OCR worker process and the Decky backend.

`event_seq` is a transport-level, per-worker-session counter starting at 1 and
incrementing by 1 for each emitted stable event. It is independent of
`source_seq` (the capture/OCR frame sequence).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Optional

PROTOCOL_VERSION = 1
MESSAGE_TYPE = "stable_text"
KINDS = ("text", "clear")
MAX_LINE_BYTES = 64 * 1024


class TransportError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class StableTextEnvelope:
    version: int
    event_seq: int
    kind: str
    text: str
    confidence: Optional[float]
    source_seq: Optional[int]
    timestamp_monotonic: float


def envelope_from_event(event_seq: int, event: Any) -> StableTextEnvelope:
    """Build an envelope from a stabilizer ``StableTextEvent`` (duck-typed)."""
    kind = str(getattr(event, "kind", ""))
    if kind not in KINDS:
        raise TransportError("unsupported_kind")
    confidence = getattr(event, "confidence", None)
    source_seq = getattr(event, "source_seq", None)
    return StableTextEnvelope(
        version=PROTOCOL_VERSION,
        event_seq=int(event_seq),
        kind=kind,
        text="" if getattr(event, "text", None) is None else str(event.text),
        confidence=None if confidence is None else float(confidence),
        source_seq=None if source_seq is None else int(source_seq),
        timestamp_monotonic=float(getattr(event, "timestamp_monotonic", 0.0)),
    )


def encode_envelope(envelope: StableTextEnvelope) -> str:
    payload = {
        "v": envelope.version,
        "type": MESSAGE_TYPE,
        "event_seq": envelope.event_seq,
        "kind": envelope.kind,
        "text": envelope.text,
        "confidence": envelope.confidence,
        "source_seq": envelope.source_seq,
        "timestamp_monotonic": envelope.timestamp_monotonic,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def decode_envelope(line: Any) -> StableTextEnvelope:
    """Strictly validate one JSONL line; raise TransportError on any problem."""
    if not isinstance(line, str):
        raise TransportError("invalid_line")
    try:
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise TransportError("line_too_large")
    except UnicodeEncodeError as exc:
        raise TransportError("invalid_line") from exc
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError) as exc:
        raise TransportError("invalid_json") from exc
    if not isinstance(data, dict):
        raise TransportError("invalid_object")

    if data.get("v") != PROTOCOL_VERSION:
        raise TransportError("unsupported_version")
    if data.get("type") != MESSAGE_TYPE:
        raise TransportError("unsupported_type")
    kind = data.get("kind")
    if kind not in KINDS:
        raise TransportError("unsupported_kind")

    event_seq = data.get("event_seq")
    if not _is_int(event_seq) or event_seq <= 0:
        raise TransportError("invalid_event_seq")

    timestamp = data.get("timestamp_monotonic")
    if not _is_number(timestamp) or not math.isfinite(timestamp) or timestamp < 0:
        raise TransportError("invalid_timestamp")

    if kind == "text":
        text = data.get("text")
        if not isinstance(text, str) or text == "":
            raise TransportError("invalid_text")
        confidence = data.get("confidence")
        if not _is_number(confidence) or not math.isfinite(confidence) or not (0.0 <= confidence <= 1.0):
            raise TransportError("invalid_confidence")
        source_seq = data.get("source_seq")
        if not _is_int(source_seq) or source_seq < 0:
            raise TransportError("invalid_source_seq")
        return StableTextEnvelope(
            PROTOCOL_VERSION, event_seq, "text", text, float(confidence), int(source_seq), float(timestamp)
        )

    # kind == "clear"
    if data.get("text") != "":
        raise TransportError("invalid_text")
    if data.get("confidence") is not None:
        raise TransportError("invalid_confidence")
    if data.get("source_seq") is not None:
        raise TransportError("invalid_source_seq")
    return StableTextEnvelope(PROTOCOL_VERSION, event_seq, "clear", "", None, None, float(timestamp))
