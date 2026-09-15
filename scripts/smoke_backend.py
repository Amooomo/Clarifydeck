import asyncio
import logging
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EMITTED: list[tuple[str, tuple[Any, ...]]] = []


async def emit(event: str, *args: Any) -> None:
    EMITTED.append((event, args))


sys.modules.setdefault(
    "decky",
    types.SimpleNamespace(
        logger=logging.getLogger("clarifydeck-smoke"),
        DECKY_PLUGIN_RUNTIME_DIR=tempfile.gettempdir(),
        DECKY_PLUGIN_DIR=".",
        emit=emit,
    ),
)

import main  # noqa: E402


def make_ppm(width: int, height: int, value: int) -> bytes:
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return header + bytes([value]) * (width * height * 3)


def make_pam(width: int, height: int, rgba: list[int]) -> bytes:
    header = (
        f"P7\nWIDTH {width}\nHEIGHT {height}\nDEPTH 4\nMAXVAL 255\n"
        "TUPLTYPE RGB_ALPHA\nENDHDR\n"
    ).encode("ascii")
    return header + bytes(rgba) * (width * height)


async def run() -> None:
    engine = main.get_engine()
    engine.configure_runtime(Path(tempfile.mkdtemp(prefix="clarifydeck-smoke-")))

    box = await engine.add_box()
    assert box["w"] == main.ClarifyDeckEngine.DEFAULT_BOX_WIDTH
    assert box["h"] == main.ClarifyDeckEngine.DEFAULT_BOX_HEIGHT

    updated = await engine.update_box(box["id"], 1, 2, 33, 44)
    assert updated is not None
    assert (updated["x"], updated["y"], updated["w"], updated["h"]) == (1, 2, 33, 44)

    status = await engine.get_status()
    assert status["box_count"] == 1
    assert status["screen_width"] == main.ClarifyDeckEngine.DEFAULT_SCREEN_WIDTH
    assert status["screen_height"] == main.ClarifyDeckEngine.DEFAULT_SCREEN_HEIGHT

    frame = main.RGBFrame.from_ppm(make_ppm(100, 80, 200))
    assert (frame.width, frame.height) == (100, 80)
    round_trip = main.RGBFrame.from_ppm(frame.to_ppm_bytes())
    assert round_trip.data == frame.data
    crop = frame.crop(1, 2, 33, 44)
    assert crop is not None and (crop.width, crop.height) == (33, 44)

    pam = main.RGBFrame.from_ppm(make_pam(10, 10, [10, 20, 30, 255]))
    assert (pam.width, pam.height) == (10, 10)
    assert pam.data[:3] == bytes([10, 20, 30])

    up = main.RGBFrame.from_ppm(make_ppm(2, 2, 7)).upscaled(2)
    assert (up.width, up.height) == (4, 4)
    assert up.data[:3] == bytes([7, 7, 7])

    lang_status = await engine.set_ocr_lang("chi_sim")
    assert lang_status["ocr_lang"] == "chi_sim"
    await engine.set_ocr_lang("eng")

    opts = await engine.set_ocr_options(invert=True, psm=4, scale=3)
    assert opts["ocr_invert"] is True
    assert opts["ocr_psm"] == 4
    assert opts["ocr_scale"] == 3
    await engine.set_ocr_options(invert=False, psm=6, scale=2)

    same_a = main.RGBFrame.from_ppm(make_ppm(100, 80, 10))
    same_b = main.RGBFrame.from_ppm(make_ppm(100, 80, 10))
    bright = main.RGBFrame.from_ppm(make_ppm(100, 80, 250))
    assert engine._mse(same_a.signature(), same_b.signature()) == 0
    assert engine._mse(same_a.signature(), bright.signature()) > engine._mse_threshold

    assert engine._clean_ocr_text(" hello\r\n\x00 world \n\n\n") == "hello\nworld"
    assert engine._text_score("脉冲电池") == 12
    assert engine._text_score("abc 123") == 6

    engine.OCR_MIN_INTERVAL_SECONDS = 0
    engine._snapshot_cache.clear()
    calls = {"n": 0}

    async def fake_ocr(_image: Any) -> str:
        calls["n"] += 1
        return f"text-{calls['n']}"

    engine._run_ocr = fake_ocr  # type: ignore[method-assign]

    white = main.RGBFrame.from_ppm(make_ppm(200, 120, 250))
    dark = main.RGBFrame.from_ppm(make_ppm(200, 120, 5))
    await engine._process_frame(white)
    assert calls["n"] == 1, calls
    await engine._process_frame(white)
    assert calls["n"] == 1, calls
    await engine._process_frame(dark)
    assert calls["n"] == 2, calls

    events = [event for event in EMITTED if event[0] == "ocr_broadcast"]
    assert events and events[-1][1][0]["text"] == "text-2"

    async def failing_ocr(_image: Any) -> None:
        calls["n"] += 1
        return None

    engine._run_ocr = failing_ocr  # type: ignore[method-assign]
    engine._snapshot_cache.clear()
    before = calls["n"]
    await engine._process_frame(main.RGBFrame.from_ppm(make_ppm(200, 120, 100)))
    assert calls["n"] == before + 1
    assert box["id"] not in engine._snapshot_cache

    plugin = main.Plugin()
    listed = await plugin.list_boxes()
    assert len(listed) == 1 and listed[0]["id"] == box["id"]

    removed = await plugin.remove_box(box["id"])
    assert removed
    assert await engine.list_boxes() == []

    boot_status = await engine.get_status()
    assert boot_status["backend"]["role"] == "standby"
    assert boot_status["overlay"] is None
    await engine.overlay_update("no-op before explicit enable")
    await engine.overlay_hide()
    assert engine.overlay_status() is None

    print("ClarifyDeck backend smoke test passed")


if __name__ == "__main__":
    asyncio.run(run())
