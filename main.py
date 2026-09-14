import asyncio
import dataclasses
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Optional

import decky


def _plugin_root() -> Path:
    return Path(os.path.dirname(os.path.abspath(__file__)))


@dataclasses.dataclass
class BoxState:
    id: str
    x: int
    y: int
    w: int
    h: int
    text: str = ""
    last_mse: Optional[float] = None
    last_ocr_at: float = 0.0
    last_attempt_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class RGBFrame:
    """A tiny in-memory RGB image container backed by binary PPM data.

    Decky plugins cannot assume a conda/Pillow runtime.  The capture pipeline
    therefore asks screenshot tools for PPM (P6) output, parses it with stdlib
    code, and keeps the full frame plus all OCR crops in memory until a crop is
    handed to Tesseract as a temporary PPM file.
    """

    width: int
    height: int
    data: bytes

    @classmethod
    def from_ppm(cls, payload: bytes) -> "RGBFrame":
        token, pos = cls._read_ppm_token(payload, 0)
        if token == b"P6":
            return cls._from_p6(payload, pos)
        if token == b"P7":
            return cls._from_pam(payload, pos)
        raise ValueError("screenshot output is not binary PPM (P6/P7)")

    @classmethod
    def _from_p6(cls, payload: bytes, pos: int) -> "RGBFrame":
        width_token, pos = cls._read_ppm_token(payload, pos)
        height_token, pos = cls._read_ppm_token(payload, pos)
        maxval_token, pos = cls._read_ppm_token(payload, pos)
        width = int(width_token)
        height = int(height_token)
        maxval = int(maxval_token)
        if width <= 0 or height <= 0:
            raise ValueError("invalid PPM dimensions")
        if maxval <= 0 or maxval > 255:
            raise ValueError("unsupported PPM max value")
        if pos < len(payload) and payload[pos] in (9, 10, 11, 12, 13, 32):
            pos += 1
        expected = width * height * 3
        pixels = payload[pos : pos + expected]
        if len(pixels) != expected:
            raise ValueError("truncated PPM pixel data")
        return cls(width=width, height=height, data=bytes(pixels))

    @classmethod
    def _from_pam(cls, payload: bytes, pos: int) -> "RGBFrame":
        width: Optional[int] = None
        height: Optional[int] = None
        depth: Optional[int] = None
        maxval: Optional[int] = None
        size = len(payload)
        while pos < size:
            end = payload.find(b"\n", pos)
            if end == -1:
                break
            line = payload[pos:end].strip()
            pos = end + 1
            if not line or line.startswith(b"#"):
                continue
            if line == b"ENDHDR":
                break
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            key = parts[0].upper()
            try:
                value = int(parts[1])
            except ValueError:
                continue
            if key == b"WIDTH":
                width = value
            elif key == b"HEIGHT":
                height = value
            elif key == b"DEPTH":
                depth = value
            elif key == b"MAXVAL":
                maxval = value
        if not width or not height or not depth or not maxval:
            raise ValueError("invalid PAM header")
        if maxval > 255:
            raise ValueError("unsupported PAM max value")
        if depth not in (3, 4):
            raise ValueError("unsupported PAM depth")
        needed = width * height * depth
        pixels = payload[pos : pos + needed]
        if len(pixels) != needed:
            raise ValueError("truncated PAM pixel data")
        if depth == 3:
            return cls(width=width, height=height, data=bytes(pixels))
        rgb = bytearray(width * height * 3)
        out = 0
        for index in range(0, needed, 4):
            rgb[out] = pixels[index]
            rgb[out + 1] = pixels[index + 1]
            rgb[out + 2] = pixels[index + 2]
            out += 3
        return cls(width=width, height=height, data=bytes(rgb))

    @staticmethod
    def _read_ppm_token(payload: bytes, pos: int) -> tuple[bytes, int]:
        size = len(payload)
        while pos < size:
            byte = payload[pos]
            if byte == 35:  # '#'
                while pos < size and payload[pos] not in (10, 13):
                    pos += 1
            elif byte in (9, 10, 11, 12, 13, 32):
                pos += 1
            else:
                break
        start = pos
        while pos < size and payload[pos] not in (9, 10, 11, 12, 13, 32):
            pos += 1
        if start == pos:
            raise ValueError("unexpected end of PPM header")
        return payload[start:pos], pos

    def crop(self, x: int, y: int, w: int, h: int) -> Optional["RGBFrame"]:
        left = max(0, min(self.width, int(x)))
        top = max(0, min(self.height, int(y)))
        right = max(left + 1, min(self.width, int(x) + int(w)))
        bottom = max(top + 1, min(self.height, int(y) + int(h)))
        if right <= left or bottom <= top:
            return None

        crop_width = right - left
        row_bytes = self.width * 3
        crop_row_bytes = crop_width * 3
        rows = bytearray(crop_row_bytes * (bottom - top))
        out = 0
        for row in range(top, bottom):
            src = row * row_bytes + left * 3
            rows[out : out + crop_row_bytes] = self.data[src : src + crop_row_bytes]
            out += crop_row_bytes
        return RGBFrame(width=crop_width, height=bottom - top, data=bytes(rows))

    def signature(self, size: int = 64) -> tuple[int, ...]:
        """Return a bounded area-averaged grayscale signature for MSE.

        Area averaging (instead of nearest-neighbor sampling) keeps thin, small
        text strokes visible so two different captions do not collapse to the
        same signature. Sampling is strided so cost stays bounded for large boxes.
        """
        if self.width <= 0 or self.height <= 0:
            return tuple()
        stride = max(1, (self.width * self.height) // 40000)
        values: list[int] = []
        for sy in range(size):
            y0 = (sy * self.height) // size
            y1 = max(y0 + 1, ((sy + 1) * self.height) // size)
            for sx in range(size):
                x0 = (sx * self.width) // size
                x1 = max(x0 + 1, ((sx + 1) * self.width) // size)
                total = 0
                count = 0
                for y in range(y0, min(y1, self.height), stride):
                    base = y * self.width * 3
                    for x in range(x0, min(x1, self.width), stride):
                        offset = base + x * 3
                        total += (
                            30 * self.data[offset]
                            + 59 * self.data[offset + 1]
                            + 11 * self.data[offset + 2]
                        ) // 100
                        count += 1
                values.append(total // count if count else 0)
        return tuple(values)

    def to_ppm_bytes(self) -> bytes:
        return f"P6\n{self.width} {self.height}\n255\n".encode("ascii") + self.data

    def to_png_bytes(self) -> bytes:
        """Minimal PNG encoder (truecolor, 8-bit) for debug dumps."""
        raw = bytearray()
        row_bytes = self.width * 3
        for y in range(self.height):
            raw.append(0)
            raw += self.data[y * row_bytes : (y + 1) * row_bytes]

        def chunk(tag: bytes, payload: bytes) -> bytes:
            body = tag + payload
            return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b"")
        )

    def save_ppm(self, path: Path) -> None:
        path.write_bytes(self.to_ppm_bytes())

    def upscaled(self, factor: int) -> "RGBFrame":
        """Nearest-neighbor integer upscale to help OCR read small text."""
        if factor <= 1 or self.width <= 0 or self.height <= 0:
            return self
        out = bytearray()
        for y in range(self.height):
            row = self.data[y * self.width * 3 : (y + 1) * self.width * 3]
            expanded = bytearray()
            for x in range(self.width):
                pixel = row[x * 3 : x * 3 + 3]
                expanded += pixel * factor
            for _ in range(factor):
                out += expanded
        return RGBFrame(width=self.width * factor, height=self.height * factor, data=bytes(out))

    def mean_gray(self) -> float:
        if not self.data:
            return 0.0
        total = 0
        count = 0
        for index in range(0, len(self.data), 3):
            total += (30 * self.data[index] + 59 * self.data[index + 1] + 11 * self.data[index + 2]) // 100
            count += 1
        return total / count if count else 0.0

    def to_grayscale(self, invert: bool = False) -> "RGBFrame":
        out = bytearray(len(self.data))
        for index in range(0, len(self.data), 3):
            gray = (30 * self.data[index] + 59 * self.data[index + 1] + 11 * self.data[index + 2]) // 100
            if invert:
                gray = 255 - gray
            out[index] = gray
            out[index + 1] = gray
            out[index + 2] = gray
        return RGBFrame(width=self.width, height=self.height, data=bytes(out))


class ClarifyDeckEngine:
    DEFAULT_BOX_WIDTH = 300
    DEFAULT_BOX_HEIGHT = 60
    DEFAULT_SCREEN_WIDTH = 1280
    DEFAULT_SCREEN_HEIGHT = 800
    CAPTURE_INTERVAL_SECONDS = 0.5
    OCR_MIN_INTERVAL_SECONDS = 0.75
    DEFAULT_MSE_THRESHOLD = 16.0
    CAPTURE_TIMEOUT_SECONDS = 10.0

    def __init__(self) -> None:
        self.boxes: dict[str, BoxState] = {}
        self._snapshot_cache: dict[str, tuple[int, ...]] = {}
        self._last_frame: Optional[RGBFrame] = None
        self._capture_task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._lock = asyncio.Lock()
        self._runtime_dir = Path(tempfile.gettempdir()) / "clarifydeck"
        self._enabled = False
        self._last_error = ""
        self._last_capture_at = 0.0
        self._last_ocr_at = 0.0
        self._screen_width = self.DEFAULT_SCREEN_WIDTH
        self._screen_height = self.DEFAULT_SCREEN_HEIGHT
        self._ocr_lang = os.environ.get("CLARIFYDECK_OCR_LANG", "chi_sim+eng")
        self._ocr_scale = max(1, int(os.environ.get("CLARIFYDECK_OCR_SCALE", "2")))
        self._ocr_invert = os.environ.get("CLARIFYDECK_OCR_INVERT", "0").strip() == "1"
        self._ocr_psm = int(os.environ.get("CLARIFYDECK_OCR_PSM", "6"))
        self._mse_threshold = float(os.environ.get("CLARIFYDECK_MSE_THRESHOLD", self.DEFAULT_MSE_THRESHOLD))
        self._deck_user = self._detect_deck_user()
        self._xdg_runtime_dir = os.environ.get("CLARIFYDECK_XDG_RUNTIME_DIR", "/run/user/1000")
        self._capture_timeout = float(
            os.environ.get("CLARIFYDECK_CAPTURE_TIMEOUT", self.CAPTURE_TIMEOUT_SECONDS)
        )
        self._debug_crops = os.environ.get("CLARIFYDECK_DEBUG_CROP") == "1"
        self._debug_dir = self._resolve_debug_dir()
        self._ocr_in_progress = False
        self._langs_cache: Optional[list[str]] = None

    def configure_runtime(self, runtime_dir: str | Path) -> None:
        self._runtime_dir = Path(runtime_dir)
        self._runtime_dir.mkdir(parents=True, exist_ok=True)

    async def start(self) -> dict[str, Any]:
        self._enabled = True
        self._last_error = ""
        if self._capture_task is None or self._capture_task.done():
            self._stop_event = asyncio.Event()
            self._capture_task = asyncio.create_task(self._capture_loop(), name="clarifydeck-singleton-capture")
        return await self.get_status()

    async def stop(self) -> dict[str, Any]:
        self._enabled = False
        if self._stop_event is not None:
            self._stop_event.set()
        if self._capture_task is not None:
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
            self._capture_task = None
        self._stop_event = None
        return await self.get_status()

    async def list_boxes(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [box.to_dict() for box in self.boxes.values()]

    async def add_box(self) -> dict[str, Any]:
        box = BoxState(
            id=uuid.uuid4().hex[:8],
            x=max(0, int((self._screen_width - self.DEFAULT_BOX_WIDTH) / 2)),
            y=max(0, int((self._screen_height - self.DEFAULT_BOX_HEIGHT) / 2)),
            w=self.DEFAULT_BOX_WIDTH,
            h=self.DEFAULT_BOX_HEIGHT,
        )
        async with self._lock:
            self.boxes[box.id] = box
        await self._emit_boxes_changed()
        return box.to_dict()

    async def update_box(self, box_id: str, x: int, y: int, w: int, h: int) -> Optional[dict[str, Any]]:
        async with self._lock:
            box = self.boxes.get(box_id)
            if box is None:
                return None
            box.x = self._clamp_int(x, 0, 7680)
            box.y = self._clamp_int(y, 0, 4320)
            box.w = self._clamp_int(w, 10, 7680)
            box.h = self._clamp_int(h, 10, 4320)
            self._snapshot_cache.pop(box_id, None)
            updated = box.to_dict()
        await self._emit_boxes_changed()
        return updated

    async def remove_box(self, box_id: str) -> bool:
        async with self._lock:
            removed = self.boxes.pop(box_id, None) is not None
            self._snapshot_cache.pop(box_id, None)
        if removed:
            await self._emit_boxes_changed()
        return removed

    async def set_ocr_lang(self, lang: str) -> dict[str, Any]:
        self._ocr_lang = (lang or "eng").strip()
        self._snapshot_cache.clear()
        return await self.get_status()

    async def set_ocr_options(
        self,
        invert: Optional[bool] = None,
        psm: Optional[int] = None,
        scale: Optional[int] = None,
    ) -> dict[str, Any]:
        if invert is not None:
            self._ocr_invert = bool(invert)
        if psm is not None:
            self._ocr_psm = int(psm)
        if scale is not None:
            self._ocr_scale = max(1, min(4, int(scale)))
        self._snapshot_cache.clear()
        return await self.get_status()

    async def run_ocr_now(self, box_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            box = self.boxes.get(box_id)
            snapshot = dataclasses.replace(box) if box is not None else None
        if snapshot is None:
            return None
        frame = await self._capture_frame()
        if frame is None:
            return {"text": None, "error": self._last_error, "crop_path": None}
        crop = frame.crop(snapshot.x, snapshot.y, snapshot.w, snapshot.h)
        if crop is None:
            return {"text": None, "error": "region out of frame", "crop_path": None}
        crop_path = self._save_debug_crop(crop, f"last_crop_{box_id}")
        text = await self._run_ocr(crop)
        return {
            "text": text,
            "error": self._last_error if text is None else "",
            "crop_path": str(crop_path) if crop_path else None,
        }

    @staticmethod
    def _resolve_debug_dir() -> Path:
        override = os.environ.get("CLARIFYDECK_DEBUG_DIR")
        if override:
            return Path(override)
        home_deck = Path("/home/deck")
        if home_deck.is_dir():
            return home_deck / "Clarifydeck-spike"
        return Path(tempfile.gettempdir()) / "Clarifydeck-spike"

    def _save_debug_crop(self, crop: RGBFrame, name: str) -> Optional[Path]:
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            path = self._debug_dir / f"{name}.png"
            path.write_bytes(crop.to_png_bytes())
            return path
        except OSError:
            return None

    async def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "box_count": len(self.boxes),
            "capture_running": self._capture_task is not None and not self._capture_task.done(),
            "last_error": self._last_error,
            "last_capture_at": self._last_capture_at,
            "last_ocr_at": self._last_ocr_at,
            "ocr_lang": self._ocr_lang,
            "ocr_scale": self._ocr_scale,
            "ocr_invert": self._ocr_invert,
            "ocr_psm": self._ocr_psm,
            "mse_threshold": self._mse_threshold,
            "tesseract": str(self._find_tesseract() or ""),
            "tessdata": str(self._find_tessdata_dir() or ""),
            "ocr_langs": self._list_langs(),
            "screen_width": self._screen_width,
            "screen_height": self._screen_height,
            "frame_cached": self._last_frame is not None,
        }

    def _list_langs(self) -> list[str]:
        if self._langs_cache is not None:
            return self._langs_cache
        tesseract = self._find_tesseract()
        tessdata = self._find_tessdata_dir()
        if tesseract is None or tessdata is None:
            return []
        env = {"PATH": "/usr/bin:/bin", "TESSDATA_PREFIX": str(tessdata) + os.sep}
        if _plugin_root() in tesseract.parents:
            env["LD_LIBRARY_PATH"] = str(_plugin_root() / "lib")
        try:
            proc = subprocess.run(
                [str(tesseract), "--list-langs"],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
        except Exception:
            return []
        langs: list[str] = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line and not line.lower().startswith("list of"):
                langs.append(line)
        self._langs_cache = langs
        return langs

    async def clear_error(self) -> None:
        self._last_error = ""

    async def _capture_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                if self._enabled and self.boxes:
                    frame = await self._capture_frame()
                    if frame is not None:
                        self._last_frame = frame
                        self._screen_width = frame.width
                        self._screen_height = frame.height
                        self._last_capture_at = time.time()
                        if not self._ocr_in_progress:
                            self._ocr_in_progress = True
                            asyncio.create_task(self._process_frame_guarded(frame))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"capture loop error: {exc}"
                decky.logger.exception(self._last_error)
            await asyncio.sleep(self.CAPTURE_INTERVAL_SECONDS)

    async def _process_frame_guarded(self, frame: RGBFrame) -> None:
        try:
            await self._process_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"ocr task error: {exc}"
            decky.logger.exception(self._last_error)
        finally:
            self._ocr_in_progress = False

    async def _process_frame(self, frame: RGBFrame) -> None:
        async with self._lock:
            boxes = [dataclasses.replace(box) for box in self.boxes.values()]

        for box in boxes:
            crop = frame.crop(box.x, box.y, box.w, box.h)
            if crop is None:
                continue

            if self._debug_crops:
                self._save_debug_crop(crop, f"auto_{box.id}")

            signature = crop.signature()
            cached = self._snapshot_cache.get(box.id)
            mse = self._mse(signature, cached) if cached is not None else math.inf
            now = time.time()
            if cached is not None and mse < self._mse_threshold:
                continue
            if now - box.last_attempt_at < self.OCR_MIN_INTERVAL_SECONDS:
                continue

            text = await self._run_ocr(crop)

            current = self._last_frame
            if current is not None:
                fresh = current.crop(box.x, box.y, box.w, box.h)
                if fresh is not None and fresh.signature() != signature:
                    self._snapshot_cache.pop(box.id, None)
                    continue

            async with self._lock:
                live_box = self.boxes.get(box.id)
                if live_box is None:
                    continue
                live_box.last_attempt_at = now
                if text is None:
                    self._snapshot_cache.pop(box.id, None)
                    continue
                live_box.text = text
                live_box.last_mse = None if math.isinf(mse) else mse
                live_box.last_ocr_at = now
                payload = live_box.to_dict()

            self._snapshot_cache[box.id] = signature
            self._last_ocr_at = now
            await decky.emit("ocr_broadcast", {"id": payload["id"], "text": payload["text"], "box": payload})

    async def _capture_frame(self) -> Optional[RGBFrame]:
        """Capture one SteamOS/gamescope frame through PipeWire + GStreamer.

        The validated pipeline is ``pipewiresrc num-buffers=1 ! videoconvert !
        video/x-raw,format=RGB ! pnmenc ! filesink``. Forcing RGB guarantees a
        binary PPM (P6) instead of a PAM (P7) with alpha. PipeWire only exposes
        the gamescope screen-cast node in game mode; desktop mode returns
        "target not found".
        """
        return await self._capture_with_command("")

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

    def _capture_env(self) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "XDG_RUNTIME_DIR": self._xdg_runtime_dir,
            "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", "gamescope-0"),
            "HOME": os.environ.get("HOME", "/home/deck"),
            "USER": os.environ.get("USER", "deck"),
        }

    def _gst_capture_argv(self, output: Path) -> list[str]:
        gst = shutil.which("gst-launch-1.0") or "/usr/bin/gst-launch-1.0"
        return [
            gst,
            "-q",
            "pipewiresrc",
            "num-buffers=1",
            "!",
            "videoconvert",
            "!",
            "video/x-raw,format=RGB",
            "!",
            "pnmenc",
            "!",
            "filesink",
            f"location={output}",
        ]

    async def _capture_with_command(self, command: str) -> Optional[RGBFrame]:
        output = self._runtime_dir / f"capture-{uuid.uuid4().hex}.ppm"
        env = self._capture_env()
        override = os.environ.get("CLARIFYDECK_SCREENSHOT_CMD", "").strip()

        try:
            if override:
                shell_cmd = override.format(output=str(output)) if "{output}" in override else override
                argv = ["/bin/sh", "-c", shell_cmd]
                prefix = self._deck_prefix()
                if prefix:
                    argv = prefix + [f"{key}={value}" for key, value in env.items()] + argv
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env={**os.environ, **env},
                    )
            else:
                base = self._gst_capture_argv(output)
                prefix = self._deck_prefix()
                if prefix:
                    argv = prefix + [f"{key}={value}" for key, value in env.items()] + base
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    proc = await asyncio.create_subprocess_exec(
                        *base,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env={**os.environ, **env},
                    )

            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._capture_timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass
                self._last_error = "截图超时：PipeWire 未在限定时间内返回帧。"
                return None

            if proc.returncode != 0:
                message = stderr.decode(errors="ignore").strip()
                self._last_error = f"GStreamer 截图失败: {message[:200]}"
                return None

            if not output.exists() or output.stat().st_size == 0:
                self._last_error = "截图文件未生成"
                return None

            return RGBFrame.from_ppm(output.read_bytes())

        except Exception as exc:
            self._last_error = f"截图执行异常: {exc}"
            return None

        finally:
            try:
                if output.exists():
                    output.unlink()
            except OSError:
                pass

    def _deck_prefix(self) -> list[str]:
        if self._deck_user:
            sudo = shutil.which("sudo") or "/usr/bin/sudo"
            return [sudo, "-u", self._deck_user, "env"]
        return []

    def _mse(self, signature_a: tuple[int, ...], signature_b: Optional[tuple[int, ...]]) -> float:
        if not signature_a or not signature_b or len(signature_a) != len(signature_b):
            return math.inf
        return sum((a - b) ** 2 for a, b in zip(signature_a, signature_b)) / len(signature_a)

    async def _run_ocr(self, image: RGBFrame) -> Optional[str]:
        tesseract = self._find_tesseract()
        if tesseract is None:
            self._last_error = "Bundled Tesseract not found. Expected current plugin directory/bin/tesseract."
            return None

        tessdata = self._find_tessdata_dir()
        if tessdata is None:
            self._last_error = "Tesseract tessdata not found. Expected current plugin directory/share/tessdata/."
            return None

        fd, image_path = tempfile.mkstemp(prefix="clarifydeck-ocr-", suffix=".ppm", dir=self._runtime_dir)
        os.close(fd)
        try:
            ppm_path = Path(image_path)
            ocr_env = {
                "PATH": "/usr/bin:/bin",
                "TESSDATA_PREFIX": str(tessdata) + os.sep,
            }
            if _plugin_root() in tesseract.parents:
                ocr_env["LD_LIBRARY_PATH"] = str(_plugin_root() / "lib")

            psm = str(self._ocr_psm)
            processed = image.upscaled(self._ocr_scale)
            if self._ocr_invert:
                processed = processed.to_grayscale(True)
            processed.save_ppm(ppm_path)
            proc = await asyncio.create_subprocess_exec(
                str(tesseract),
                str(ppm_path),
                "stdout",
                "--psm",
                psm,
                "-l",
                self._ocr_lang,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=ocr_env,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                self._last_error = stderr.decode(errors="ignore")[:500]
                return None
            return self._clean_ocr_text(stdout.decode("utf-8", errors="ignore"))
        finally:
            try:
                os.remove(image_path)
            except OSError:
                pass

    @staticmethod
    def _text_score(text: str) -> int:
        score = 0
        for char in text:
            if "\u4e00" <= char <= "\u9fff":
                score += 3
            elif char.isalnum():
                score += 1
            elif char in "，。！？、；：（）《》「」“”":
                score += 1
        return score

    def _find_tesseract(self) -> Optional[Path]:
        candidates = [
            _plugin_root() / "bin" / "tesseract",
            _plugin_root() / ".conda" / "bin" / "tesseract",
            _plugin_root() / "conda" / "bin" / "tesseract",
        ]
        for candidate in candidates:
            if not candidate.exists():
                continue
            if not os.access(candidate, os.X_OK):
                try:
                    candidate.chmod(candidate.stat().st_mode | 0o111)
                except OSError:
                    pass
            if os.access(candidate, os.X_OK):
                return candidate
        found = shutil.which("tesseract")
        return Path(found) if found else None

    def _find_tessdata_dir(self) -> Optional[Path]:
        candidates = [
            _plugin_root() / "share" / "tessdata",
            _plugin_root() / ".conda" / "share" / "tessdata",
            _plugin_root() / "conda" / "share" / "tessdata",
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_dir():
                return candidate
        return None

    def _clean_ocr_text(self, text: str) -> str:
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    async def _emit_boxes_changed(self) -> None:
        await decky.emit("boxes_changed", await self.list_boxes())

    def _clamp_int(self, value: int, minimum: int, maximum: int) -> int:
        return max(minimum, min(maximum, int(value)))


_ENGINE: Optional[ClarifyDeckEngine] = None


def get_engine() -> ClarifyDeckEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = ClarifyDeckEngine()
    return _ENGINE


class Plugin:
    async def list_boxes(self) -> list[dict[str, Any]]:
        return await get_engine().list_boxes()

    async def add_box(self) -> dict[str, Any]:
        return await get_engine().add_box()

    async def update_box(self, box_id: str, x: int, y: int, w: int, h: int) -> Optional[dict[str, Any]]:
        return await get_engine().update_box(box_id, x, y, w, h)

    async def remove_box(self, box_id: str) -> bool:
        return await get_engine().remove_box(box_id)

    async def start_plugin(self) -> dict[str, Any]:
        return await get_engine().start()

    async def stop_plugin(self) -> dict[str, Any]:
        return await get_engine().stop()

    async def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if enabled:
            return await get_engine().start()
        return await get_engine().stop()

    async def get_status(self) -> dict[str, Any]:
        return await get_engine().get_status()

    async def set_ocr_lang(self, lang: str) -> dict[str, Any]:
        return await get_engine().set_ocr_lang(lang)

    async def set_ocr_options(
        self,
        invert: Optional[bool] = None,
        psm: Optional[int] = None,
        scale: Optional[int] = None,
    ) -> dict[str, Any]:
        return await get_engine().set_ocr_options(invert, psm, scale)

    async def run_ocr_now(self, box_id: str) -> Optional[dict[str, Any]]:
        return await get_engine().run_ocr_now(box_id)

    async def clear_error(self) -> None:
        await get_engine().clear_error()

    async def _main(self) -> None:
        runtime_dir = getattr(decky, "DECKY_PLUGIN_RUNTIME_DIR", tempfile.gettempdir())
        get_engine().configure_runtime(runtime_dir)
        decky.logger.info("ClarifyDeck backend initialized; OCR is stopped until start_plugin is called")

    async def _unload(self) -> None:
        await get_engine().stop()
        decky.logger.info("ClarifyDeck backend unloaded")

    async def _uninstall(self) -> None:
        await get_engine().stop()
        decky.logger.info("ClarifyDeck backend uninstalled")

    async def _migration(self) -> None:
        decky.logger.info("ClarifyDeck migration complete")
