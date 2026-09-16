"""StableTextEvent transport protocol v1/v2 (Phase 2I.1 / 2L.3).

Pure stdlib only: this module must never import RapidOCR / numpy / cv2 /
onnxruntime / omegaconf / antlr4. It defines the newline-delimited JSON envelope
exchanged between the OCR worker process and the Decky backend.

``event_seq`` is a transport-level, per-worker-session counter starting at 1 and
incrementing by 1 for each emitted stable event. It is worker-global (one strict
sequence across v1/v2 and all regions) and is independent of ``source_seq`` (the
capture/OCR frame sequence, which may repeat across regions from one frame).

v1 = single-region (production default); v2 adds a required ``region_id``. A
canonical clear is always ``text=""``, ``confidence=None``, ``source_seq=None``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional

PROTOCOL_VERSION = 1
REGION_PROTOCOL_VERSION = 2
SUPPORTED_VERSIONS = (PROTOCOL_VERSION, REGION_PROTOCOL_VERSION)
MESSAGE_TYPE = "stable_text"
KINDS = ("text", "clear")
MAX_LINE_BYTES = 64 * 1024
MAX_REGION_ID_LENGTH = 128


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
    region_id: Optional[str] = None


def validate_region_id(region_id: Any) -> str:
    """v2 region identity: non-empty string, bounded length (no normalization)."""
    if not isinstance(region_id, str) or not region_id or len(region_id) > MAX_REGION_ID_LENGTH:
        raise TransportError("invalid_region_id")
    return region_id


def envelope_from_event(
    event_seq: int,
    event: Any,
    *,
    version: int = PROTOCOL_VERSION,
    region_id: Optional[str] = None,
) -> StableTextEnvelope:
    """Build an envelope from a stabilizer ``StableTextEvent`` (duck-typed).

    A clear is canonicalized at the transport boundary so no producer can emit a
    second clear shape: ``text=""``, ``confidence=None``, ``source_seq=None``.
    """
    kind = str(getattr(event, "kind", ""))
    if kind not in KINDS:
        raise TransportError("unsupported_kind")
    if version == REGION_PROTOCOL_VERSION:
        region_id = validate_region_id(region_id)
    elif region_id is not None:
        raise TransportError("unexpected_region_id")
    if kind == "clear":
        text = ""
        confidence = None
        source_seq = None
    else:
        confidence = getattr(event, "confidence", None)
        source_seq = getattr(event, "source_seq", None)
        text = "" if getattr(event, "text", None) is None else str(event.text)
        confidence = None if confidence is None else float(confidence)
        source_seq = None if source_seq is None else int(source_seq)
    return StableTextEnvelope(
        version=version,
        event_seq=int(event_seq),
        kind=kind,
        text=text,
        confidence=confidence,
        source_seq=source_seq,
        timestamp_monotonic=float(getattr(event, "timestamp_monotonic", 0.0)),
        region_id=region_id,
    )


def encode_region_stable_text_event(event_seq: int, region_event: Any) -> str:
    """Encode one internal ``RegionStableTextEvent`` as a v2 JSONL line."""
    region_id = validate_region_id(getattr(region_event, "region_id", None))
    event = getattr(region_event, "event", region_event)
    envelope = envelope_from_event(
        event_seq, event, version=REGION_PROTOCOL_VERSION, region_id=region_id
    )
    return encode_envelope(envelope)


def encode_region_stable_text_stream(region_events: Iterable[Any], start: int = 1) -> list[str]:
    """Encode region events with one global, strictly increasing event_seq."""
    lines = []
    seq = int(start)
    for region_event in region_events:
        lines.append(encode_region_stable_text_event(seq, region_event))
        seq += 1
    return lines


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
    if envelope.region_id is not None:
        payload["region_id"] = envelope.region_id
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

    version = data.get("v")
    if version not in SUPPORTED_VERSIONS:
        raise TransportError("unsupported_version")
    region_id: Optional[str] = None
    if version == REGION_PROTOCOL_VERSION:
        region_id = validate_region_id(data.get("region_id"))
    elif data.get("region_id") is not None:
        raise TransportError("unexpected_region_id")
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
            version, event_seq, "text", text, float(confidence), int(source_seq), float(timestamp), region_id
        )

    # kind == "clear"
    if data.get("text") != "":
        raise TransportError("invalid_text")
    if data.get("confidence") is not None:
        raise TransportError("invalid_confidence")
    if data.get("source_seq") is not None:
        raise TransportError("invalid_source_seq")
    return StableTextEnvelope(version, event_seq, "clear", "", None, None, float(timestamp), region_id)
