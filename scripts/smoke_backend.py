import asyncio
import logging
import sys
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
        DECKY_PLUGIN_RUNTIME_DIR=".",
        DECKY_PLUGIN_DIR=".",
        emit=emit,
    ),
)

import main  # noqa: E402


async def run() -> None:
    plugin = main.Plugin()

    box = await plugin.add_box()
    assert box["w"] == plugin.DEFAULT_BOX_WIDTH
    assert box["h"] == plugin.DEFAULT_BOX_HEIGHT

    updated = await plugin.update_box(box["id"], 1, 2, 33, 44)
    assert updated is not None
    assert updated["x"] == 1
    assert updated["y"] == 2
    assert updated["w"] == 33
    assert updated["h"] == 44

    status = await plugin.get_status()
    assert status["screen_width"] == plugin.DEFAULT_SCREEN_WIDTH
    assert status["screen_height"] == plugin.DEFAULT_SCREEN_HEIGHT

    boxes = await plugin.list_boxes()
    assert len(boxes) == 1
    assert boxes[0]["id"] == box["id"]

    assert plugin._clean_ocr_text(" hello\r\n\x00 world \n\n\n") == "hello\nworld"

    if main.Image is not None:
        frame = main.Image.new("RGB", (100, 80), color="white")
        crop = plugin._crop_box(frame, main.BoxState(id="crop", x=1, y=2, w=33, h=44))
        assert crop is not None
        assert crop.size == (33, 44)

        signature_a = plugin._make_signature(frame)
        signature_b = plugin._make_signature(frame.copy())
        assert plugin._mse(signature_a, signature_b) == 0

        ocr_calls = 0

        async def fake_run_ocr(_image: Any) -> str:
            nonlocal ocr_calls
            ocr_calls += 1
            return "Tiny text"

        plugin._run_ocr = fake_run_ocr  # type: ignore[method-assign]
        await plugin._process_frame(frame)
        await plugin._process_frame(frame)
        assert ocr_calls == 1
        ocr_events = [event for event in EMITTED if event[0] == "ocr_broadcast"]
        assert len(ocr_events) == 1
        assert ocr_events[0][1][0]["text"] == "Tiny text"

    removed = await plugin.remove_box(box["id"])
    assert removed
    assert await plugin.list_boxes() == []

    print("ClarifyDeck backend smoke test passed")


if __name__ == "__main__":
    asyncio.run(run())
