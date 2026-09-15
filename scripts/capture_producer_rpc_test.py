#!/usr/bin/env python3
"""Phase 2C.1: exercise the LIVE ClarifyDeck backend producer over the real
Decky RPC transport.

Transport (same as the Steam UI frontend):
    ws://127.0.0.1:1337/ws?auth=<token>   token from /auth/token
    CALL  {"type":0,"id":N,"route":"loader/call_plugin_method",
           "args":[<plugin_name>, <method>, ...]}
    REPLY {"type":1,"id":N,"result":...} / ERROR {"type":-1,...}
    then {"type":3,"id":N} (RECEIVED_RESPONSE)

This harness NEVER imports or constructs the plugin backend/engine; it only
talks to the already-running backend over the loader websocket.

Usage:
    python3 scripts/capture_producer_rpc_test.py status
    python3 scripts/capture_producer_rpc_test.py start --fps 1
    python3 scripts/capture_producer_rpc_test.py stop
    python3 scripts/capture_producer_rpc_test.py run --fps 1 --duration-sec 10

Exit codes: 0 success, 1 backend ok=false, 2 transport/unreachable, 3 timeout.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1337
POLL_INTERVAL = 0.5
POLL_TIMEOUT = 15.0


class TransportError(RuntimeError):
    pass


class BackendError(RuntimeError):
    def __init__(self, error: Any) -> None:
        super().__init__(str(error))
        self.error = error


class WSTimeout(RuntimeError):
    pass


def _plugin_name() -> str:
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    return str(manifest.get("name", "ClarifyDeck"))


def _auth_token(host: str, port: int, timeout: float = 5.0) -> str:
    url = f"http://{host}:{port}/auth/token"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8").strip()


class MinimalWebSocket:
    """Tiny RFC6455 text client (stdlib only)."""

    def __init__(self, host: str, port: int, path: str, timeout: float = 10.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise TransportError("websocket handshake failed (closed)")
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise TransportError(f"websocket handshake rejected: {head[:120]!r}")
        self._buf = rest

    def _read_exact(self, count: int) -> bytes:
        while len(self._buf) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise TransportError("websocket closed")
            self._buf += chunk
        data, self._buf = self._buf[:count], self._buf[count:]
        return data

    def _read_frame(self) -> tuple[int, bool, bytes]:
        b0, b1 = self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, fin, payload

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self, deadline: Optional[float] = None) -> str:
        parts = bytearray()
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WSTimeout("websocket receive timeout")
                self.sock.settimeout(remaining)
            opcode, fin, payload = self._read_frame()
            if opcode == 0x1:  # text
                parts += payload
                if fin:
                    return parts.decode("utf-8")
            elif opcode == 0x0:  # continuation
                parts += payload
                if fin:
                    return parts.decode("utf-8")
            elif opcode == 0x8:
                raise TransportError("websocket closed by peer")
            elif opcode == 0x9:  # ping
                self._send_control(0xA, payload)
            else:
                continue

    def _send_control(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode, 0x80 | len(payload)])
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def close(self) -> None:
        try:
            self._send_control(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def make_rpc(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 10.0) -> Callable[..., Any]:
    plugin = _plugin_name()

    def rpc(method: str, *args: Any) -> Any:
        try:
            token = _auth_token(host, port)
        except Exception as exc:
            raise TransportError(f"cannot fetch auth token: {exc}") from exc
        ws = MinimalWebSocket(host, port, f"/ws?auth={token}", timeout=timeout)
        try:
            call_id = 1
            ws.send_text(
                json.dumps(
                    {
                        "type": 0,
                        "id": call_id,
                        "route": "loader/call_plugin_method",
                        "args": [plugin, method, *args],
                    }
                )
            )
            deadline = time.monotonic() + timeout
            while True:
                message = json.loads(ws.recv_text(deadline))
                if message.get("id") != call_id:
                    continue
                kind = message.get("type")
                ws.send_text(json.dumps({"type": 3, "id": call_id}))
                if kind == 1:
                    return message.get("result")
                if kind == -1:
                    raise BackendError(message.get("error"))
                continue
        finally:
            ws.close()

    return rpc


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _wait_for_state(rpc: Callable[..., Any], expected: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {"ok": False, "state": "UNKNOWN"}
    while time.monotonic() < deadline:
        last = rpc("capture_producer_status")
        if last.get("state") == expected:
            return last
        time.sleep(POLL_INTERVAL)
    raise WSTimeout(f"state did not reach {expected} (last={last.get('state')})")


def main(argv=None, rpc: Optional[Callable[..., Any]] = None) -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck live-backend producer RPC harness")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p_start = sub.add_parser("start")
    p_start.add_argument("--fps", type=float, default=1.0)
    sub.add_parser("stop")
    p_run = sub.add_parser("run")
    p_run.add_argument("--fps", type=float, default=1.0)
    p_run.add_argument("--duration-sec", type=float, default=10.0)
    args = parser.parse_args(argv)

    call = rpc or make_rpc()

    try:
        if args.command == "status":
            result = call("capture_producer_status")
            _print(result)
            return 0 if result.get("ok", True) else 1

        if args.command == "start":
            result = call("capture_producer_start", args.fps)
            _print(result)
            if not result.get("ok", True):
                return 1
            if result.get("state") not in ("RUNNING", "STARTING"):
                return 3
            return 0

        if args.command == "stop":
            result = call("capture_producer_stop")
            _print(result)
            return 0 if result.get("ok", True) else 1

        if args.command == "run":
            started = call("capture_producer_start", args.fps)
            _print(started)
            if not started.get("ok", True):
                return 1
            deadline = time.monotonic() + args.duration_sec
            while time.monotonic() < deadline:
                time.sleep(POLL_INTERVAL)
            stopped = call("capture_producer_stop")
            _print(stopped)
            if not stopped.get("ok", True):
                return 1
            if stopped.get("state") != "STOPPED":
                return 3
            return 0
    except BackendError as exc:
        print(json.dumps({"ok": False, "error": exc.error}, ensure_ascii=False, indent=2))
        return 1
    except WSTimeout as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 3
    except Exception as exc:
        print(json.dumps({"ok": False, "transport_error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
