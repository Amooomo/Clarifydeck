"""OCR runtime adapter (Phase 2F).

Isolates every RapidOCR / ONNX Runtime / NumPy detail behind one adapter so the
rest of ClarifyDeck never touches the native OCR API directly.

Design rules:
- native imports happen lazily inside functions (never at module import);
- the engine is initialized once per process (``engine_init_count == 1``);
- model assets are explicit local files, validated against a manifest; a missing
  asset or hash mismatch fails closed (no implicit download);
- RGBA input is converted to a 3-channel array with one explicit, tested color
  order;
- ``close`` is idempotent and drops engine references.
"""

from __future__ import annotations

import hashlib
import json
import platform
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .result import Box, OCRFrameResult, OCRLine

MANIFEST_VERSION = 2
SUPPORTED_MANIFEST_VERSIONS = (1, 2)
MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 256 * 1024
ALLOWED_COLOR_ORDERS = ("rgb", "bgr")
DEFAULT_COLOR_ORDER = "bgr"
ALLOWED_DICTIONARY_MODES = ("embedded", "external")
ALLOWED_DET_LIMIT_TYPES = ("min", "max")
DEFAULT_DET_LIMIT_SIDE_LEN = 736
DEFAULT_DET_LIMIT_TYPE = "min"
FORBIDDEN_MODEL_SUFFIXES = (".traineddata",)


class OCRError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ModelManifest:
    format_version: int
    engine: str
    family: str
    files: dict
    sha256: dict
    size_bytes: dict
    path: Path
    dictionary_mode: str = "embedded"
    model_type: Optional[str] = None
    engine_type: Optional[str] = None
    rapidocr_version: Optional[str] = None


@dataclass
class OCRConfig:
    model_dir: Path
    color_order: str = DEFAULT_COLOR_ORDER
    min_confidence: float = 0.0
    scale: int = 1
    engine_params: Optional[dict] = None
    debug: bool = False
    ort_intra_threads: Optional[int] = None
    ort_inter_threads: Optional[int] = None
    opencv_threads: Optional[int] = None
    det_limit_side_len: int = DEFAULT_DET_LIMIT_SIDE_LEN
    det_limit_type: str = DEFAULT_DET_LIMIT_TYPE


def validate_thread_count(value: Any, name: str) -> Optional[int]:
    """Accept -1 (auto) or >= 1; reject anything else."""
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OCRError("invalid_thread_count", f"{name}={value!r}") from exc
    if parsed == -1 or parsed >= 1:
        return parsed
    raise OCRError("invalid_thread_count", f"{name}={value!r}")


def validate_det_limit_side_len(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OCRError("invalid_limit_side_len", f"{value!r}") from exc
    if parsed <= 0:
        raise OCRError("invalid_limit_side_len", f"{value!r}")
    return parsed


def validate_det_limit_type(value: Any) -> str:
    if value not in ALLOWED_DET_LIMIT_TYPES:
        raise OCRError("invalid_limit_type", f"{value!r}")
    return value


def _effective_det_limit(limit_side_len: int, limit_type: str, max_wh: int) -> int:
    """Mirror RapidOCR 3.9.2 DetPreProcess limit selection."""
    if limit_type == "min":
        return int(limit_side_len)
    if max_wh < 960:
        return 960
    if max_wh < 1500:
        return 1500
    return 2000


def predict_det_geometry(
    width: int,
    height: int,
    limit_side_len: int = DEFAULT_DET_LIMIT_SIDE_LEN,
    limit_type: str = DEFAULT_DET_LIMIT_TYPE,
) -> dict:
    """Predict the detector resize geometry (mirrors DetPreProcess.resize)."""
    if width <= 0 or height <= 0:
        raise OCRError("invalid_input", f"{width}x{height}")
    limit_type = validate_det_limit_type(limit_type)
    limit = _effective_det_limit(validate_det_limit_side_len(limit_side_len), limit_type, max(width, height))
    if limit_type == "max":
        if max(width, height) > limit:
            ratio = float(limit) / (height if height > width else width)
        else:
            ratio = 1.0
    else:
        if min(width, height) < limit:
            ratio = float(limit) / (height if height < width else width)
        else:
            ratio = 1.0
    resize_h = int(round(height * ratio / 32) * 32)
    resize_w = int(round(width * ratio / 32) * 32)
    return {
        "source": f"{width}x{height}",
        "limit_type": limit_type,
        "limit_side_len": limit,
        "ratio": round(ratio, 4),
        "predicted_resized": f"{resize_w}x{resize_h}",
        "predicted_width": resize_w,
        "predicted_height": resize_h,
        "predicted_pixels": resize_w * resize_h,
    }


def load_manifest(model_dir: Path) -> ModelManifest:
    path = Path(model_dir) / MANIFEST_NAME
    if not path.is_file():
        raise OCRError("manifest_missing", str(path))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OCRError("manifest_unreadable", str(exc)) from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise OCRError("manifest_too_large", f"{len(raw)} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OCRError("manifest_invalid", str(exc)) from exc
    if not isinstance(data, dict):
        raise OCRError("manifest_invalid", "not an object")
    version = data.get("format_version")
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        raise OCRError("manifest_unsupported_version", str(version))
    engine = data.get("engine")
    family = data.get("family")
    files = data.get("files")
    if not isinstance(engine, str) or not engine:
        raise OCRError("manifest_invalid", "engine")
    if not isinstance(family, str) or not family:
        raise OCRError("manifest_invalid", "family")
    if not isinstance(files, dict) or not files:
        raise OCRError("manifest_invalid", "files")

    normalized_files: dict[str, str] = {}
    sha256: dict[str, str] = {}
    size_bytes: dict[str, int] = {}
    if version == 1:
        for role, rel in files.items():
            if not isinstance(role, str) or not isinstance(rel, str) or not rel:
                raise OCRError("manifest_invalid", "files entry")
            normalized_files[role] = rel
        sha256 = data.get("sha256") if isinstance(data.get("sha256"), dict) else {}
        size_bytes = data.get("size_bytes") if isinstance(data.get("size_bytes"), dict) else {}
        dictionary_mode = "external" if "dict" in normalized_files else "embedded"
    else:
        for role, entry in files.items():
            if not isinstance(role, str):
                raise OCRError("manifest_invalid", "files entry")
            if isinstance(entry, str):
                normalized_files[role] = entry
            elif isinstance(entry, dict):
                rel = entry.get("path")
                if not isinstance(rel, str) or not rel:
                    raise OCRError("manifest_invalid", f"files.{role}.path")
                normalized_files[role] = rel
                if isinstance(entry.get("sha256"), str):
                    sha256[role] = entry["sha256"]
                if isinstance(entry.get("size_bytes"), int):
                    size_bytes[role] = entry["size_bytes"]
            else:
                raise OCRError("manifest_invalid", f"files.{role}")
        dictionary = data.get("dictionary")
        if dictionary is None:
            dictionary_mode = "external" if "dict" in normalized_files else "embedded"
        elif isinstance(dictionary, dict):
            dictionary_mode = dictionary.get("mode", "embedded")
        else:
            raise OCRError("manifest_invalid", "dictionary")
        if dictionary_mode not in ALLOWED_DICTIONARY_MODES:
            raise OCRError("invalid_dictionary_mode", str(dictionary_mode))
        if dictionary_mode == "external" and "dict" not in normalized_files:
            raise OCRError("manifest_invalid", "external dictionary requires a dict file")

    for rel in normalized_files.values():
        if rel.lower().endswith(FORBIDDEN_MODEL_SUFFIXES):
            raise OCRError("invalid_model_asset", rel)

    return ModelManifest(
        format_version=version,
        engine=engine,
        family=family,
        files=normalized_files,
        sha256=sha256,
        size_bytes=size_bytes,
        path=path,
        dictionary_mode=dictionary_mode,
        model_type=data.get("model_type") if isinstance(data.get("model_type"), str) else None,
        engine_type=data.get("engine_type") if isinstance(data.get("engine_type"), str) else None,
        rapidocr_version=data.get("rapidocr_version") if isinstance(data.get("rapidocr_version"), str) else None,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_assets(manifest: ModelManifest) -> dict[str, Path]:
    resolved: dict[str, Path] = {}
    missing: list[str] = []
    for role, rel in manifest.files.items():
        candidate = (manifest.path.parent / rel).resolve()
        if not candidate.is_file():
            missing.append(role)
            continue
        resolved[role] = candidate
    if missing:
        raise OCRError("model_assets_missing", ",".join(sorted(missing)))
    for role, expected in manifest.sha256.items():
        candidate = resolved.get(role)
        if candidate is None or not isinstance(expected, str):
            continue
        if _sha256_file(candidate) != expected.lower():
            raise OCRError("model_hash_mismatch", role)
    for role, expected_size in manifest.size_bytes.items():
        candidate = resolved.get(role)
        if candidate is None or not isinstance(expected_size, int):
            continue
        if candidate.stat().st_size != expected_size:
            raise OCRError("model_size_mismatch", role)
    return resolved


def rgba_to_array(rgba: bytes, width: int, height: int, color_order: str = DEFAULT_COLOR_ORDER):
    """Convert tightly packed RGBA bytes to an ``H x W x 3`` uint8 array.

    One explicit color-order conversion lives here (never scattered elsewhere).
    """
    if color_order not in ALLOWED_COLOR_ORDERS:
        raise OCRError("invalid_color_order", color_order)
    if width <= 0 or height <= 0:
        raise OCRError("invalid_input", f"{width}x{height}")
    if len(rgba) != width * height * 4:
        raise OCRError("invalid_input", f"buffer {len(rgba)} != {width * height * 4}")
    try:
        import numpy as np  # noqa: PLC0415 - lazy native import
    except Exception as exc:  # pragma: no cover - depends on device runtime
        raise OCRError("numpy_unavailable", str(exc)) from exc
    array = np.frombuffer(rgba, dtype=np.uint8).reshape(height, width, 4)
    rgb = array[:, :, :3]
    if color_order == "rgb":
        return np.ascontiguousarray(rgb)
    return np.ascontiguousarray(rgb[:, :, ::-1])


def _normalize_box(box: Any) -> Optional[Box]:
    if box is None:
        return None
    try:
        points = tuple((float(point[0]), float(point[1])) for point in box)
    except (TypeError, ValueError, IndexError):
        return None
    return points if len(points) > 0 else None


def _safe_len(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return len(value)
    except TypeError:
        return None


def describe_result(result: Any) -> list:
    """Bounded structural summary (never dumps arrays/images)."""
    lines = [f"[ocr-debug] result_type={type(result).__name__}"]
    if hasattr(result, "boxes"):
        boxes = getattr(result, "boxes", None)
        lines.append(
            f"[ocr-debug] boxes_type={type(boxes).__name__} boxes_shape={getattr(boxes, 'shape', None)}"
        )
    if hasattr(result, "txts"):
        texts = getattr(result, "txts", None)
        lines.append(f"[ocr-debug] txts_type={type(texts).__name__} txts_len={_safe_len(texts)}")
    if hasattr(result, "scores"):
        scores = getattr(result, "scores", None)
        lines.append(f"[ocr-debug] scores_type={type(scores).__name__} scores_len={_safe_len(scores)}")
    return lines


def _elapse_ms(elapse_list: Any) -> tuple[Optional[float], Optional[float]]:
    """Convert a RapidOCR elapse sequence to (det_ms, rec_ms) without truthiness."""
    if elapse_list is None:
        return None, None
    try:
        values = list(elapse_list)
    except TypeError:
        return None, None
    if len(values) == 0:
        return None, None
    try:
        det_ms = round(float(values[0]) * 1000.0, 3)
    except (TypeError, ValueError):
        det_ms = None
    rec_ms: Optional[float] = None
    if len(values) > 1:
        try:
            rec_ms = round(sum(float(value) for value in values[1:]) * 1000.0, 3)
        except (TypeError, ValueError):
            rec_ms = None
    return det_ms, rec_ms


def _structured_lines(boxes: Any, texts: Any, scores: Any) -> list:
    """Normalize a structured RapidOCR 3.x output (NumPy-safe presence checks)."""
    if texts is None:
        return []
    try:
        text_list = list(texts)
    except TypeError as exc:
        raise OCRError("invalid_engine_result", f"txts not iterable: {exc}") from exc
    box_list = None if boxes is None else list(boxes)
    score_list = None if scores is None else list(scores)
    if box_list is not None and len(box_list) != len(text_list):
        raise OCRError(
            "invalid_engine_result", f"boxes={len(box_list)} txts={len(text_list)}"
        )
    if score_list is not None and len(score_list) != len(text_list):
        raise OCRError(
            "invalid_engine_result", f"scores={len(score_list)} txts={len(text_list)}"
        )
    lines = []
    for index, text in enumerate(text_list):
        box = box_list[index] if box_list is not None else None
        score = score_list[index] if score_list is not None else None
        lines.append((box, text, score))
    return lines


def _legacy_lines(payload: Any) -> list:
    if payload is None:
        return []
    try:
        items = list(payload)
    except TypeError:
        return []
    lines = []
    for item in items:
        try:
            lines.append((item[0], item[1], item[2]))
        except (TypeError, IndexError):
            continue
    return lines


def _normalize_rapidocr_result(result: Any) -> tuple[list, Optional[float], Optional[float]]:
    """Normalize RapidOCR output without ever calling ``bool`` on a NumPy array.

    Structured (RapidOCR 3.x object with ``boxes``/``txts``/``scores``) and legacy
    ``(payload, elapse)`` paths are kept separate.
    """
    if hasattr(result, "boxes") or hasattr(result, "txts"):
        boxes = getattr(result, "boxes", None)
        texts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        lines = _structured_lines(boxes, texts, scores)
        det_ms, rec_ms = _elapse_ms(getattr(result, "elapse_list", None))
        return lines, det_ms, rec_ms

    if isinstance(result, tuple) and len(result) == 2:
        payload, elapse = result
        return _legacy_lines(payload), *_elapse_ms(elapse)

    if isinstance(result, list):
        return _legacy_lines(result), None, None

    return [], None, None


class _RapidOCREngine:
    """Thin wrapper so the adapter owns the RapidOCR call surface."""

    def __init__(self, engine: Any, params: Optional[dict] = None) -> None:
        self._engine = engine
        self.params = dict(params or {})
        self.last_result: Any = None
        self.last_raw_ms: Optional[float] = None
        self.last_normalize_ms: Optional[float] = None

    def __call__(self, image):
        raw_started = time.perf_counter()
        result = self._engine(image)
        self.last_result = result
        self.last_raw_ms = round((time.perf_counter() - raw_started) * 1000.0, 3)
        normalize_started = time.perf_counter()
        normalized = _normalize_rapidocr_result(result)
        self.last_normalize_ms = round((time.perf_counter() - normalize_started) * 1000.0, 3)
        return normalized


class OCRRuntime:
    def __init__(
        self,
        config: OCRConfig,
        engine_factory: Optional[Callable[[ModelManifest, OCRConfig], Any]] = None,
    ) -> None:
        self._config = config
        self._engine_factory = engine_factory or self._default_engine_factory
        self._engine: Any = None
        self._manifest: Optional[ModelManifest] = None
        self._backend = f"{config.model_dir.name or 'ocr'}"
        self._init_count = 0
        self._init_ms: Optional[float] = None
        self._closed = False
        self._last_timings: dict = {}
        self.ort_intra_threads = validate_thread_count(config.ort_intra_threads, "ort_intra_threads")
        self.ort_inter_threads = validate_thread_count(config.ort_inter_threads, "ort_inter_threads")
        self.opencv_threads = validate_thread_count(config.opencv_threads, "opencv_threads")
        self.det_limit_side_len = validate_det_limit_side_len(config.det_limit_side_len)
        self.det_limit_type = validate_det_limit_type(config.det_limit_type)

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        if self._engine is not None:
            return
        started = time.perf_counter()
        manifest = load_manifest(self._config.model_dir)
        validate_assets(manifest)
        engine = self._engine_factory(manifest, self._config)
        if engine is None:
            raise OCRError("engine_init_failed", "factory returned None")
        self._manifest = manifest
        self._engine = engine
        self._backend = f"{manifest.engine}/{manifest.family}"
        self._init_count += 1
        self._init_ms = round((time.perf_counter() - started) * 1000.0, 3)

    def close(self) -> None:
        self._engine = None
        self._closed = True

    # -- recognition -------------------------------------------------------

    def recognize_rgba(
        self,
        rgba: bytes,
        width: int,
        height: int,
        sequence: Optional[int] = None,
    ) -> OCRFrameResult:
        if not rgba:
            raise OCRError("invalid_input", "empty buffer")
        if width <= 0 or height <= 0:
            raise OCRError("invalid_input", f"{width}x{height}")
        if len(rgba) != width * height * 4:
            raise OCRError("invalid_input", f"buffer {len(rgba)} != {width * height * 4}")

        self.initialize()
        wall_started = time.perf_counter()
        cpu_started = time.process_time()
        array_started = time.perf_counter()
        image = rgba_to_array(rgba, width, height, self._config.color_order)
        array_ms = round((time.perf_counter() - array_started) * 1000.0, 3)
        call_started = time.perf_counter()
        try:
            raw_lines, det_ms, rec_ms = self._engine(image)
        except OCRError:
            raise
        except Exception as exc:
            raise OCRError("engine_run_failed", str(exc)) from exc
        call_ms = round((time.perf_counter() - call_started) * 1000.0, 3)
        ocr_wall_ms = round((time.perf_counter() - wall_started) * 1000.0, 3)
        ocr_cpu_ms = round((time.process_time() - cpu_started) * 1000.0, 3)
        self._last_timings = {
            "rgba_to_array_ms": array_ms,
            "ocr_call_ms": call_ms,
            "result_normalize_ms": getattr(self._engine, "last_normalize_ms", None),
            "ocr_wall_ms": ocr_wall_ms,
            "ocr_process_cpu_ms": ocr_cpu_ms,
            "ocr_effective_cpu_pct": round(ocr_cpu_ms / ocr_wall_ms * 100.0, 1) if ocr_wall_ms else None,
            "det_ms": det_ms,
            "rec_ms": rec_ms,
        }
        if self._config.debug:
            for line in describe_result(getattr(self._engine, "last_result", None)):
                print(line)

        lines: list[OCRLine] = []
        for box, text, score in raw_lines:
            confidence = float(score) if score is not None else None
            if confidence is not None and confidence < self._config.min_confidence:
                continue
            lines.append(OCRLine(text=str(text), confidence=confidence, box=_normalize_box(box)))
        return OCRFrameResult(
            sequence=sequence,
            lines=tuple(lines),
            elapsed_ms=ocr_wall_ms,
            backend=self._backend,
            roi_width=width,
            roi_height=height,
            det_ms=det_ms,
            rec_ms=rec_ms,
        )

    # -- introspection -----------------------------------------------------

    @property
    def engine_init_count(self) -> int:
        return self._init_count

    @property
    def engine_init_ms(self) -> Optional[float]:
        return self._init_ms

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def last_timings(self) -> dict:
        return dict(self._last_timings)

    @property
    def model_paths(self) -> dict:
        params = getattr(self._engine, "params", {}) if self._engine is not None else {}
        return {
            "det": params.get("Det.model_path"),
            "rec": params.get("Rec.model_path"),
            "cls": params.get("Cls.model_path"),
            "classifier": "disabled" if params.get("Global.use_cls") is False else "enabled",
        }

    @property
    def manifest(self) -> Optional[ModelManifest]:
        return self._manifest

    # -- default engine ----------------------------------------------------

    def _default_engine_factory(self, manifest: ModelManifest, config: OCRConfig) -> Any:
        try:
            import rapidocr  # noqa: PLC0415 - lazy native import
            from rapidocr import RapidOCR  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - depends on device runtime
            raise OCRError("rapidocr_unavailable", str(exc)) from exc
        if config.opencv_threads is not None and int(config.opencv_threads) >= 0:
            try:
                import cv2  # noqa: PLC0415

                cv2.setNumThreads(int(config.opencv_threads))
            except Exception:
                pass
        params = (
            config.engine_params
            if config.engine_params is not None
            else _rapidocr_params(manifest, rapidocr, config)
        )
        try:
            engine = RapidOCR(params=params)
        except Exception as exc:
            # No silent fallback to RapidOCR() defaults: a param error must surface.
            raise OCRError("engine_init_failed", str(exc)) from exc
        _verify_model_paths(engine, params)
        return _RapidOCREngine(engine, params)


_ENGINE_TYPE_NAMES = {"onnxruntime": "ONNXRUNTIME"}
_OCR_VERSION_NAMES = {"PP-OCRv4": "PPOCRV4", "PP-OCRv5": "PPOCRV5", "PP-OCRv6": "PPOCRV6"}
_MODEL_TYPE_NAMES = {"mobile": "MOBILE", "server": "SERVER", "tiny": "TINY", "small": "SMALL", "medium": "MEDIUM"}


def _enum_params(rapidocr_module, manifest: ModelManifest) -> dict:
    """RapidOCR 3.9.2 requires Enum instances for engine/version/type params."""
    if rapidocr_module is None:
        return {}
    params: dict = {}
    engine_enum = getattr(rapidocr_module, "EngineType", None)
    version_enum = getattr(rapidocr_module, "OCRVersion", None)
    type_enum = getattr(rapidocr_module, "ModelType", None)
    engine_name = _ENGINE_TYPE_NAMES.get((manifest.engine_type or "onnxruntime"))
    version_name = _OCR_VERSION_NAMES.get(manifest.family)
    type_name = _MODEL_TYPE_NAMES.get(manifest.model_type or "small")
    if engine_enum is not None and engine_name and hasattr(engine_enum, engine_name):
        value = getattr(engine_enum, engine_name)
        params["Det.engine_type"] = value
        params["Rec.engine_type"] = value
    if version_enum is not None and version_name and hasattr(version_enum, version_name):
        value = getattr(version_enum, version_name)
        params["Det.ocr_version"] = value
        params["Rec.ocr_version"] = value
    if type_enum is not None and type_name and hasattr(type_enum, type_name):
        value = getattr(type_enum, type_name)
        params["Det.model_type"] = value
        params["Rec.model_type"] = value
    return params


def _bundled_classifier_path(rapidocr_module) -> Optional[Path]:
    try:
        package_dir = Path(rapidocr_module.__file__).resolve().parent
    except Exception:
        return None
    candidate = package_dir / "models" / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
    return candidate if candidate.is_file() else None


def _rapidocr_params(manifest: ModelManifest, rapidocr_module=None, config: Optional[OCRConfig] = None) -> dict:
    """Map manifest roles to RapidOCR 3.9.2 params (explicit, no download).

    ``model_path`` values are absolute so ``OrtInferSession`` never falls back to
    ``model_root_dir`` or a network download. Enum params are only added when the
    RapidOCR module is available (3.9.2 requires Enum instances, not strings).
    """
    resolved = validate_assets(manifest)
    params: dict = {"Global.use_cls": False}
    if "det" in resolved:
        params["Det.model_path"] = str(resolved["det"])
    if "rec" in resolved:
        params["Rec.model_path"] = str(resolved["rec"])
    if manifest.dictionary_mode == "external" and "dict" in resolved:
        params["Rec.rec_keys_path"] = str(resolved["dict"])
    params.update(_enum_params(rapidocr_module, manifest))
    if rapidocr_module is not None:
        # RapidOCR 3.9.2 always constructs TextClassifier (even with use_cls=False),
        # so pin its model explicitly to avoid implicit resolution/download.
        cls_path = _bundled_classifier_path(rapidocr_module)
        if cls_path is not None:
            params["Cls.model_path"] = str(cls_path)
    if config is not None:
        # RapidOCR's own ORT session options (EngineConfig.onnxruntime.*).
        if config.ort_intra_threads is not None:
            params["EngineConfig.onnxruntime.intra_op_num_threads"] = int(config.ort_intra_threads)
        if config.ort_inter_threads is not None:
            params["EngineConfig.onnxruntime.inter_op_num_threads"] = int(config.ort_inter_threads)
        # Explicit detector resize geometry (Det.limit_side_len / Det.limit_type).
        if config.det_limit_side_len is not None:
            params["Det.limit_side_len"] = validate_det_limit_side_len(config.det_limit_side_len)
        if config.det_limit_type is not None:
            params["Det.limit_type"] = validate_det_limit_type(config.det_limit_type)
    return params


def _verify_model_paths(engine, params: dict) -> None:
    """Fail closed unless RapidOCR's resolved config matches our explicit paths."""
    for key in ("Det.model_path", "Rec.model_path"):
        expected = params.get(key)
        if not expected:
            continue
        section, name = key.split(".")
        try:
            actual = getattr(getattr(engine.cfg, section), name)
        except Exception as exc:
            raise OCRError("model_path_not_applied", f"{key}: {exc}") from exc
        if str(actual) != str(expected):
            raise OCRError("model_path_not_applied", f"{key}={actual} != {expected}")


def _module_path(name: str) -> Optional[str]:
    try:
        import importlib.util  # noqa: PLC0415

        spec = importlib.util.find_spec(name)
        return spec.origin if spec else None
    except Exception:
        return None


PROBE_DEPENDENCIES = (
    "numpy",
    "cv2",
    "rapidocr",
    "onnxruntime",
    "pyclipper",
    "shapely",
    "Pillow",
    "PyYAML",
    "omegaconf",
    "antlr4",
)


def probe_report_lines(info: dict) -> list[str]:
    """Shared probe formatter so the probe and ocr_test never diverge."""
    lines = [
        f"[ocr-probe] python={info.get('python')}",
        f"[ocr-probe] python_version={info.get('python_version')}",
        f"[ocr-probe] machine={info.get('machine')} pointer_bits={info.get('pointer_bits')}",
        f"[ocr-probe] soabi={info.get('soabi')}",
        f"[ocr-probe] glibc={info.get('glibc')} libc={info.get('libc')}",
        f"[ocr-probe] runtime_site_packages={info.get('runtime_site_packages')}",
        f"[ocr-probe] runtime_activated={info.get('runtime_activated')}",
    ]
    for name in PROBE_DEPENDENCIES:
        lines.append(f"[ocr-probe] {name}={info.get(name)} path={info.get(f'{name}_path')}")
    lines.append(f"[ocr-probe] providers={info.get('providers')}")
    if info.get("model_dir") is not None:
        lines.append(f"[ocr-probe] model_dir={info.get('model_dir')} status={info.get('model_status')}")
        if info.get("model_error"):
            lines.append(f"[ocr-probe] model_error={info['model_error']}")
    lines.append(f"[ocr-probe] compatible={info.get('compatible')}")
    return lines


def probe_runtime(model_dir: Optional[Path] = None) -> dict:
    """Bounded runtime compatibility probe (no model loading, no capture)."""
    import sysconfig  # noqa: PLC0415

    libc_name, libc_version = platform.libc_ver()
    info: dict[str, Any] = {
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "machine": platform.machine(),
        "pointer_bits": struct.calcsize("P") * 8,
        "soabi": sysconfig.get_config_var("SOABI"),
        "glibc": libc_version or None,
        "libc": libc_name or None,
        "providers": [],
    }
    for module, key in (
        ("numpy", "numpy"),
        ("cv2", "cv2"),
        ("rapidocr", "rapidocr"),
        ("onnxruntime", "onnxruntime"),
        ("pyclipper", "pyclipper"),
        ("shapely", "shapely"),
        ("PIL", "Pillow"),
        ("yaml", "PyYAML"),
        ("omegaconf", "omegaconf"),
        ("antlr4", "antlr4"),
    ):
        try:
            imported = __import__(module)
            info[key] = getattr(imported, "__version__", "unknown")
        except Exception as exc:
            info[key] = None
            info[f"{key}_error"] = str(exc)
        info[f"{key}_path"] = _module_path(module)
    try:
        import onnxruntime  # noqa: PLC0415

        info["providers"] = list(onnxruntime.get_available_providers())
    except Exception:
        info["providers"] = []

    if model_dir is not None:
        info["model_dir"] = str(model_dir)
        try:
            manifest = load_manifest(Path(model_dir))
            validate_assets(manifest)
            info["model_status"] = "ok"
            info["model_family"] = manifest.family
            info["model_files"] = dict(manifest.files)
            info["dictionary_mode"] = manifest.dictionary_mode
        except OCRError as exc:
            info["model_status"] = exc.code
            info["model_error"] = str(exc)

    info["compatible"] = bool(
        info.get("numpy")
        and info.get("rapidocr")
        and info.get("onnxruntime")
        and "CPUExecutionProvider" in info.get("providers", [])
    )
    return info
