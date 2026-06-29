import asyncio
import dataclasses
import math
import os
import re
import shutil
import tempfile
import time
import uuid
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
        if token != b"P6":
            raise ValueError("screenshot output is not binary PPM (P6)")
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
        expected = width * height * 3
        pixels = payload[pos : pos + expected]
        if len(pixels) != expected:
            raise ValueError("truncated PPM pixel data")
        return cls(width=width, height=height, data=bytes(pixels))

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

    def signature(self, size: int = 32) -> tuple[int, ...]:
        """Return a small grayscale nearest-neighbor signature for MSE."""
        if self.width <= 0 or self.height <= 0:
            return tuple()
        signature: list[int] = []
        for sy in range(size):
            y = min(self.height - 1, int((sy + 0.5) * self.height / size))
            for sx in range(size):
                x = min(self.width - 1, int((sx + 0.5) * self.width / size))
                offset = (y * self.width + x) * 3
                r = self.data[offset]
                g = self.data[offset + 1]
                b = self.data[offset + 2]
                signature.append((30 * r + 59 * g + 11 * b) // 100)
        return tuple(signature)

    def to_ppm_bytes(self) -> bytes:
        return f"P6\n{self.width} {self.height}\n255\n".encode("ascii") + self.data

    def save_ppm(self, path: Path) -> None:
        path.write_bytes(self.to_ppm_bytes())


class ClarifyDeckEngine:
    DEFAULT_BOX_WIDTH = 300
    DEFAULT_BOX_HEIGHT = 60
    DEFAULT_SCREEN_WIDTH = 1280
    DEFAULT_SCREEN_HEIGHT = 800
    CAPTURE_INTERVAL_SECONDS = 0.5
    OCR_MIN_INTERVAL_SECONDS = 0.75
    DEFAULT_MSE_THRESHOLD = 16.0

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
        self._ocr_lang = os.environ.get("CLARIFYDECK_OCR_LANG", "eng")
        self._mse_threshold = float(os.environ.get("CLARIFYDECK_MSE_THRESHOLD", self.DEFAULT_MSE_THRESHOLD))

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

    async def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "box_count": len(self.boxes),
            "capture_running": self._capture_task is not None and not self._capture_task.done(),
            "last_error": self._last_error,
            "last_capture_at": self._last_capture_at,
            "last_ocr_at": self._last_ocr_at,
            "ocr_lang": self._ocr_lang,
            "mse_threshold": self._mse_threshold,
            "tesseract": str(self._find_tesseract() or ""),
            "tessdata": str(self._find_tessdata_dir() or ""),
            "screen_width": self._screen_width,
            "screen_height": self._screen_height,
            "frame_cached": self._last_frame is not None,
        }

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
                        await self._process_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"capture loop error: {exc}"
                decky.logger.exception(self._last_error)
            await asyncio.sleep(self.CAPTURE_INTERVAL_SECONDS)

    async def _process_frame(self, frame: RGBFrame) -> None:
        async with self._lock:
            boxes = [dataclasses.replace(box) for box in self.boxes.values()]

        for box in boxes:
            crop = frame.crop(box.x, box.y, box.w, box.h)
            if crop is None:
                continue

            signature = crop.signature()
            cached = self._snapshot_cache.get(box.id)
            mse = self._mse(signature, cached) if cached is not None else math.inf
            now = time.time()
            if cached is not None and mse < self._mse_threshold:
                continue
            if now - box.last_ocr_at < self.OCR_MIN_INTERVAL_SECONDS:
                continue

            self._snapshot_cache[box.id] = signature
            text = await self._run_ocr(crop)
            if text is None:
                continue

            async with self._lock:
                live_box = self.boxes.get(box.id)
                if live_box is None:
                    continue
                live_box.text = text
                live_box.last_mse = None if math.isinf(mse) else mse
                live_box.last_ocr_at = now
                payload = live_box.to_dict()

            self._last_ocr_at = now
            await decky.emit("ocr_broadcast", {"id": payload["id"], "text": payload["text"], "box": payload})

    async def _capture_frame(self) -> Optional[RGBFrame]:
        """Capture one SteamOS frame through PipeWire + GStreamer.

        SteamOS capture uses the built-in deck-user PipeWire/GStreamer pipeline.
        The command argument is intentionally ignored by _capture_with_command;
        capture always uses the hard-coded pipeline required on device.
        """
        return await self._capture_with_command("")

    async def _capture_with_command(self, command: str) -> Optional[RGBFrame]:
        # 废弃传入的外部指令，强制使用 SteamOS 内置的 GStreamer/PipeWire 管道
        temp_ppm = Path("/tmp/clarifydeck_capture.ppm")
        
        # 纯净白名单环境变量，物理隔离 Decky 注入的动态库污染
        clean_env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "WAYLAND_DISPLAY": "wayland-0",
            "USER": "deck",
            "HOME": "/home/deck",
        }
        
        # SteamOS 原生截图命令：捕获单帧 -> 转换格式 -> 编码为 PPM -> 写入临时文件
        cmd = (
            "sudo -u deck "
            "XDG_RUNTIME_DIR=/run/user/1000 "
            "WAYLAND_DISPLAY=wayland-0 "
            "/usr/bin/gst-launch-1.0 -q "
            "pipewiresrc num-buffers=1 ! videoconvert ! pnmenc ! "
            f"filesink location={temp_ppm}"
        )

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_env,
            )
            
            # 强制 2.0 秒超时熔断：防止系统 PipeWire 权限弹窗导致 Python 线程永久阻塞
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                self._last_error = "截图死锁超时：系统 PipeWire 拦截了抓取请求。"
                return None

            if proc.returncode != 0:
                message = stderr.decode(errors="ignore").strip()
                self._last_error = f"GStreamer 截图失败: {message[:100]}"
                return None

            if not temp_ppm.exists():
                self._last_error = "截图文件未生成"
                return None

            # 读取二进制文件并转换为 RGBFrame 内存对象
            frame_data = temp_ppm.read_bytes()
            return RGBFrame.from_ppm(frame_data)
            
        except Exception as exc:
            self._last_error = f"截图执行异常: {exc}"
            return None
            
        finally:
            # 清理临时文件，释放空间
            try:
                if temp_ppm.exists():
                    temp_ppm.unlink()
            except OSError:
                pass

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
            image.save_ppm(ppm_path)
            lib_dir = _plugin_root() / "lib"
            ocr_env = {
                "PATH": "/usr/bin:/bin",
                "LD_LIBRARY_PATH": str(lib_dir),
                "TESSDATA_PREFIX": str(tessdata) + os.sep,
            }
            proc = await asyncio.create_subprocess_exec(
                str(tesseract),
                str(ppm_path),
                "stdout",
                "--psm",
                "6",
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

    def _find_tesseract(self) -> Optional[Path]:
        tesseract = _plugin_root() / "bin" / "tesseract"
        if tesseract.exists() and os.access(tesseract, os.X_OK):
            return tesseract
        return None

    def _find_tessdata_dir(self) -> Optional[Path]:
        tessdata = _plugin_root() / "share" / "tessdata"
        if tessdata.exists() and tessdata.is_dir():
            return tessdata
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
