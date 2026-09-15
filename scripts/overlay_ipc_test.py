#!/usr/bin/env python3
"""Phase 1C IPC test tool for the ClarifyDeck overlay renderer.

Connects to an ALREADY RUNNING renderer and drives show/update/hide. It never
spawns a renderer and, by default, never sends `shutdown` (that would kill a
renderer owned by the backend). Use `--shutdown` explicitly if you really want
to stop it.

Usage:
    python3 scripts/overlay_ipc_test.py
    python3 scripts/overlay_ipc_test.py --socket /run/user/1000/clarifydeck/overlay.sock
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from overlay import protocol  # noqa: E402


def send(sock: socket.socket, payload: dict) -> None:
    sock.sendall(protocol.encode_message(payload))
    print(f"> {payload}")


def main() -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck overlay IPC test")
    parser.add_argument("--socket", default=None)
    parser.add_argument("--step", type=float, default=2.0)
    parser.add_argument(
        "--shutdown",
        action="store_true",
        help="send shutdown at the end (default: leave the renderer running)",
    )
    args = parser.parse_args()

    path = Path(args.socket) if args.socket else protocol.socket_path()
    if not path.exists():
        print(f"renderer is not running (socket not found: {path})")
        print("enable the persistent overlay first, or start the renderer manually.")
        return 1

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except OSError as exc:
        print(f"renderer is not reachable on {path}: {exc}")
        return 1
    print(f"connected to {path}")

    try:
        send(sock, {"type": "show", "text": "OCR TEST"})
        time.sleep(args.step)
        send(sock, {"type": "update", "text": "OCR TEST 2"})
        time.sleep(args.step)
        send(sock, {"type": "update", "text": "中文测试：你好，Steam Deck"})
        time.sleep(args.step)
        send(sock, {"type": "update", "text": "第二行测试\nHello World"})
        time.sleep(args.step)
        send(sock, {"type": "hide"})
        time.sleep(args.step)
        send(sock, {"type": "show", "text": "FINAL TEST"})
        time.sleep(args.step)
        if args.shutdown:
            send(sock, {"type": "shutdown"})
            time.sleep(0.5)
    finally:
        sock.close()

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
