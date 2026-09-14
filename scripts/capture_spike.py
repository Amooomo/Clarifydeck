#!/usr/bin/env python3
"""ClarifyDeck screenshot feasibility spike.

Tries every plausible SteamOS/gamescope capture backend, writes each artifact to
a known directory, validates the produced bytes, and prints a PASS/FAIL report.

Examples:
    python3 scripts/capture_spike.py
    python3 scripts/capture_spike.py --list
    python3 scripts/capture_spike.py --out /home/deck/Clarifydeck-spike --backend grim
    python3 scripts/capture_spike.py --display gamescope-0

The default output directory is ~/Clarifydeck-spike (on Steam Deck this is
/home/deck/Clarifydeck-spike). The first usable frame is also copied to
latest_capture.<ext> inside that directory for quick inspection.

Run this in game mode (a gamescope-* socket present) for a meaningful result.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_BIN = PLUGIN_ROOT / "bin"
DEFAULT_OUT_DIR = Path(
    os.environ.get("CLARIFYDECK_SPIKE_DIR", str(Path.home() / "Clarifydeck-spike"))
)


def runtime_dir(as_user: Optional[str]) -> str:
    if as_user == "deck" and not os.environ.get("CLARIFYDECK_KEEP_XDG"):
        return os.environ.get("CLARIFYDECK_RUNTIME_DIR", "/run/user/1000")
    existing = os.environ.get("XDG_RUNTIME_DIR")
    if existing:
        return existing
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    return f"/run/user/{uid}"


def default_user() -> Optional[str]:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    try:
        import pwd

        pwd.getpwnam("deck")
        return "deck"
    except Exception:
        return None


def ensure_executable(path: Path) -> bool:
    if not path.exists():
        return False
    if not os.access(path, os.X_OK):
        try:
            path.chmod(path.stat().st_mode | 0o111)
        except OSError:
            return False
    return os.access(path, os.X_OK)


def find_tool(name: str, bundled: Optional[Path] = None) -> Optional[str]:
    if bundled is not None:
        candidate = Path(bundled)
        if candidate.exists():
            if ensure_executable(candidate):
                return str(candidate)
    return shutil.which(name)


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)


def _read_pnm_header(data: bytes):
    tokens: list[bytes] = []
    pos = 0
    size = len(data)
    while len(tokens) < 4 and pos < size:
        while pos < size and data[pos] in b" \t\r\n\v\f":
            pos += 1
        if pos < size and data[pos] == 0x23:
            while pos < size and data[pos] not in (10, 13):
                pos += 1
            continue
        start = pos
        while pos < size and data[pos] not in b" \t\r\n\v\f":
            pos += 1
        if start == pos:
            break
        tokens.append(data[start:pos])
    return tokens, pos


def _analyze_pixels(data: bytes, offset: int, info: dict) -> None:
    width = info.get("width", 0)
    height = info.get("height", 0)
    if width <= 0 or height <= 0:
        return
    step = max(1, (width * height) // 20000)
    total = 0
    nonblack = 0
    count = 0
    for index in range(0, width * height, step):
        pixel = offset + index * 3
        if pixel + 2 >= len(data):
            break
        red, green, blue = data[pixel], data[pixel + 1], data[pixel + 2]
        total += (red + green + blue) // 3
        if red + green + blue > 30:
            nonblack += 1
        count += 1
    if count == 0:
        return
    info["mean"] = round(total / count, 1)
    info["nonblack_ratio"] = round(nonblack / count, 3)
    info["blank"] = info["nonblack_ratio"] < 0.01


def inspect_file(path: Optional[Path]) -> Optional[dict]:
    if path is None or not path.exists():
        return None
    info: dict = {"size": path.stat().st_size, "format": "unknown"}
    if info["size"] == 0:
        return info
    with path.open("rb") as handle:
        head = handle.read(64)
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        info["format"] = "png"
        if len(head) >= 24:
            info["width"] = int.from_bytes(head[16:20], "big")
            info["height"] = int.from_bytes(head[20:24], "big")
        return info
    if head[:2] in (b"P6", b"P7"):
        info["format"] = "ppm" if head[:2] == b"P6" else "pam"
        data = path.read_bytes()
        tokens, pos = _read_pnm_header(data)
        if len(tokens) >= 3:
            try:
                info["width"] = int(tokens[1])
                info["height"] = int(tokens[2])
            except ValueError:
                pass
            if info["format"] == "ppm" and "width" in info:
                while pos < len(data) and data[pos] in b" \t\r\n\v\f":
                    pos += 1
                _analyze_pixels(data, pos, info)
        return info
    if head[:2] == b"\xff\xd8":
        info["format"] = "jpeg"
        return info
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        info["format"] = "webp"
        return info
    return info


@dataclasses.dataclass
class Attempt:
    label: str
    command: list[str] = dataclasses.field(default_factory=list)
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    path: Optional[Path] = None
    info: Optional[dict] = None
    detail: str = ""
    duration: float = 0.0
    ok: bool = False


class Runner:
    def __init__(self, as_user: Optional[str] = None):
        self.as_user = as_user

    def _wrap(self, argv: list[str], env: dict) -> tuple[list[str], Optional[dict]]:
        if self.as_user and hasattr(os, "geteuid") and os.geteuid() == 0:
            prefix = ["sudo", "-u", self.as_user, "env"]
            prefix += [f"{key}={value}" for key, value in env.items()]
            return prefix + argv, None
        merged = os.environ.copy()
        merged.update(env)
        return argv, merged

    def run(self, argv: list[str], env: dict, timeout: int):
        if not argv:
            raise FileNotFoundError("empty command")
        full, merged = self._wrap(argv, env)
        return subprocess.run(
            full,
            env=merged,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )

    def popen(self, argv: list[str], env: dict, **kwargs):
        full, merged = self._wrap(argv, env)
        return subprocess.Popen(full, env=merged, **kwargs)


def pipewire_nodes(runner: Runner, as_user: Optional[str]) -> list[dict]:
    pw_dump = shutil.which("pw-dump")
    if not pw_dump:
        return []
    try:
        proc = runner.run([pw_dump], {"XDG_RUNTIME_DIR": runtime_dir(as_user)}, 15)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    nodes: list[dict] = []
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        media_class = str(props.get("media.class", ""))
        name = str(props.get("node.name", ""))
        nodes.append(
            {
                "id": obj.get("id"),
                "name": name,
                "class": media_class,
                "description": props.get("node.description", ""),
                "video": media_class == "Video/Source" or "video" in media_class.lower(),
            }
        )
    return nodes


def discover_displays(as_user: Optional[str]) -> list[str]:
    names: list[str] = []

    def add(value: Optional[str]) -> None:
        if value and value not in names:
            names.append(value)

    add(os.environ.get("WAYLAND_DISPLAY"))
    runtime = Path(runtime_dir(as_user))
    if runtime.is_dir():
        for entry in sorted(runtime.iterdir()):
            try:
                if not entry.is_socket():
                    continue
            except OSError:
                continue
            if entry.name.startswith(("gamescope-", "wayland-")):
                add(entry.name)
    return names


def candidate_displays(as_user: Optional[str]) -> list[str]:
    names = discover_displays(as_user)
    for name in ("gamescope-0", "wayland-0", "wayland-1"):
        if name not in names:
            names.append(name)
    return names


def session_hint(as_user: Optional[str]) -> str:
    gamescope = False
    wayland = False
    for name in discover_displays(as_user):
        if name.startswith("gamescope-"):
            gamescope = True
        elif name.startswith("wayland-"):
            wayland = True
    if gamescope:
        return "game-mode (gamescope socket present)"
    if wayland:
        return "desktop-mode (only wayland socket found; game mode recommended)"
    return "unknown (no compositor socket found)"


def collect_env(as_user: Optional[str]) -> list[str]:
    lines = [
        f"uid={os.getuid() if hasattr(os, 'getuid') else '?'} "
        f"euid={os.geteuid() if hasattr(os, 'geteuid') else '?'}",
        f"user={os.environ.get('USER', '?')} home={os.environ.get('HOME', '?')}",
        f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR', '')} (using {runtime_dir(as_user)})",
        f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '')}",
        f"DISPLAY={os.environ.get('DISPLAY', '')}",
        f"session: {session_hint(as_user)}",
    ]
    for tool in (
        "grim",
        "gst-launch-1.0",
        "pw-dump",
        "pw-cli",
        "spectacle",
        "gnome-screenshot",
        "scrot",
        "import",
        "gdbus",
        "dbus-monitor",
        "busctl",
    ):
        lines.append(f"which {tool}: {shutil.which(tool)}")
    for binary in ("grim", "tesseract"):
        bundled = BUNDLED_BIN / binary
        if bundled.exists():
            ensure_executable(bundled)
        lines.append(
            f"bundled {binary}: {bundled} exists={bundled.exists()} "
            f"exec={os.access(bundled, os.X_OK) if bundled.exists() else False}"
        )
    return lines


def parse_portal_handle(stdout: str) -> Optional[str]:
    match = re.search(r"(/org/freedesktop/portal/desktop/request/[^,\s)'\"]+)", stdout)
    return match.group(1) if match else None


def parse_portal_uri(lines: list[str]) -> Optional[str]:
    for line in lines:
        match = re.search(r'"(file://[^"]+)"', line)
        if match:
            return match.group(1)
    return None


class CaptureSpike:
    def __init__(self, out_dir: Path, runner: Runner, runtime: str, displays: list[str]):
        self.out_dir = out_dir
        self.runner = runner
        self.runtime = runtime
        self.displays = displays
        self.attempts: list[Attempt] = []

    def _record_missing(self, label: str) -> None:
        self.attempts.append(Attempt(label=label, detail="tool not found / not executable"))

    def _finalize_attempt(self, attempt: Attempt, proc, path: Path) -> Attempt:
        attempt.returncode = proc.returncode
        attempt.stdout = (proc.stdout or "").strip()
        attempt.stderr = (proc.stderr or "").strip()
        attempt.path = path if path.exists() else None
        attempt.info = inspect_file(attempt.path)
        attempt.ok = bool(
            proc.returncode == 0
            and attempt.info
            and attempt.info.get("size", 0) > 0
            and attempt.info.get("format") in ("png", "ppm", "pam")
            and attempt.info.get("width", 0) > 0
            and attempt.info.get("height", 0) > 0
        )
        self.attempts.append(attempt)
        return attempt

    def _run(self, label: str, argv: list[str], env: dict, timeout: int, path: Path) -> Attempt:
        attempt = Attempt(label=label, command=argv)
        started = time.time()
        try:
            proc = self.runner.run(argv, env, timeout)
        except subprocess.TimeoutExpired:
            attempt.detail = f"timeout after {timeout}s"
            self.attempts.append(attempt)
            return attempt
        except Exception as exc:
            attempt.detail = f"exec error: {exc}"
            self.attempts.append(attempt)
            return attempt
        attempt.duration = round(time.time() - started, 2)
        return self._finalize_attempt(attempt, proc, path)

    def try_grim(self) -> Optional[Attempt]:
        grim = find_tool("grim", BUNDLED_BIN / "grim")
        if not grim:
            self._record_missing("grim")
            return None
        for display in self.displays:
            for fmt in ("png", "ppm"):
                out = self.out_dir / f"grim_{_safe(display)}.{fmt}"
                argv = [grim, "-t", fmt, str(out)]
                env = {"XDG_RUNTIME_DIR": self.runtime, "WAYLAND_DISPLAY": display}
                attempt = self._run(f"grim[{display}].{fmt}", argv, env, 15, out)
                if attempt.ok:
                    return attempt
        return None

    def try_gst(self, nodes: list[dict]) -> Optional[Attempt]:
        gst = shutil.which("gst-launch-1.0")
        if not gst:
            self._record_missing("gst-launch-1.0")
            return None
        display = self.displays[0] if self.displays else "gamescope-0"
        targets: list[tuple[Optional[str], str, Optional[str]]] = [(None, "default", None)]
        for node in nodes:
            if node.get("id") is None:
                continue
            node_id = str(node["id"])
            targets.append((node_id, f"target{node_id}", "target-object"))
            targets.append((node_id, f"path{node_id}", "path"))
        for target, tag, prop in targets:
            first_ok: Optional[Attempt] = None
            for fmt, encoder in (("png", "pngenc"), ("ppm", "pnmenc")):
                out = self.out_dir / f"gst_{tag}.{fmt}"
                pipeline = ["pipewiresrc", "num-buffers=1"]
                if target and prop:
                    pipeline.append(f"{prop}={target}")
                pipeline += [
                    "!",
                    "videoconvert",
                    "!",
                    "video/x-raw,format=RGB",
                    "!",
                    encoder,
                    "!",
                    "filesink",
                    f"location={out}",
                ]
                argv = [gst, "-q"] + pipeline
                env = {
                    "XDG_RUNTIME_DIR": self.runtime,
                    "WAYLAND_DISPLAY": display,
                    "HOME": os.environ.get("HOME", "/home/deck"),
                }
                attempt = self._run(f"gst[{tag}].{fmt}", argv, env, 20, out)
                if attempt.ok and first_ok is None:
                    first_ok = attempt
            if first_ok is not None:
                return first_ok
        return None

    def _collect_portal_response(self, monitor, handle: str, timeout: int) -> list[str]:
        lines: list[str] = []
        if monitor is None or monitor.stdout is None:
            return lines
        try:
            import select
        except Exception:
            return lines
        deadline = time.time() + timeout
        in_target = False
        while time.time() < deadline:
            ready, _, _ = select.select([monitor.stdout], [], [], 0.5)
            if not ready:
                continue
            line = monitor.stdout.readline()
            if not line:
                break
            if "member=Response" in line:
                in_target = handle in line
                if in_target:
                    lines.append(line)
            elif in_target:
                lines.append(line)
                if "file://" in line:
                    break
        try:
            monitor.kill()
        except Exception:
            pass
        return lines

    def try_portal_screenshot(self) -> Optional[Attempt]:
        gdbus = shutil.which("gdbus")
        if not gdbus:
            self._record_missing("gdbus")
            return None
        dbus_monitor = shutil.which("dbus-monitor")
        token = f"clarifydeck{int(time.time())}"
        out = self.out_dir / "portal_screenshot.png"
        env = {"XDG_RUNTIME_DIR": self.runtime}

        monitor = None
        if dbus_monitor:
            try:
                monitor = self.runner.popen(
                    [
                        dbus_monitor,
                        "--session",
                        "interface='org.freedesktop.portal.Request',member='Response'",
                    ],
                    env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except Exception:
                monitor = None

        call = [
            gdbus,
            "call",
            "--session",
            "--dest",
            "org.freedesktop.portal.Desktop",
            "--object-path",
            "/org/freedesktop/portal/desktop",
            "--method",
            "org.freedesktop.portal.Screenshot.Screenshot",
            "",
            "{'handle_token': <'%s'>, 'interactive': <false>}" % token,
        ]
        attempt = Attempt(label="portal:screenshot", command=call)
        try:
            proc = self.runner.run(call, env, 15)
        except Exception as exc:
            attempt.detail = f"portal call failed: {exc}"
            self.attempts.append(attempt)
            if monitor:
                monitor.kill()
            return attempt

        attempt.returncode = proc.returncode
        attempt.stdout = (proc.stdout or "").strip()
        attempt.stderr = (proc.stderr or "").strip()
        handle = parse_portal_handle(attempt.stdout)
        if proc.returncode != 0 or not handle:
            attempt.detail = f"portal rejected call (handle={handle})"
            self.attempts.append(attempt)
            if monitor:
                monitor.kill()
            return attempt

        lines = self._collect_portal_response(monitor, handle, timeout=25)
        uri = parse_portal_uri(lines)
        attempt.detail = f"handle={handle} uri={uri}"
        if uri:
            source = Path(uri[len("file://") :])
            if source.exists():
                try:
                    shutil.copyfile(source, out)
                except OSError:
                    pass
        attempt.path = out if out.exists() else None
        attempt.info = inspect_file(attempt.path)
        attempt.ok = bool(
            attempt.info
            and attempt.info.get("format") == "png"
            and attempt.info.get("width", 0) > 0
            and attempt.info.get("height", 0) > 0
        )
        self.attempts.append(attempt)
        return attempt

    def try_fallbacks(self) -> Optional[Attempt]:
        display = self.displays[0] if self.displays else "gamescope-0"
        specs = [
            ("spectacle", lambda exe, out: [exe, "-b", "-n", "-o", str(out)]),
            ("gnome-screenshot", lambda exe, out: [exe, "-f", str(out)]),
            ("scrot", lambda exe, out: [exe, str(out)]),
            ("import", lambda exe, out: [exe, "-window", "root", str(out)]),
        ]
        for name, build in specs:
            exe = shutil.which(name)
            if not exe:
                continue
            out = self.out_dir / f"{name.replace('-', '_')}.png"
            env = {
                "XDG_RUNTIME_DIR": self.runtime,
                "WAYLAND_DISPLAY": display,
                "DISPLAY": os.environ.get("DISPLAY", ":0"),
            }
            attempt = self._run(f"fallback:{name}", build(exe, out), env, 25, out)
            if attempt.ok:
                return attempt
        return None

    def finalize(self) -> Optional[Path]:
        passed = [a for a in self.attempts if a.ok and a.path and a.path.exists()]
        if not passed:
            return None
        best = next((a for a in passed if a.path.suffix == ".png"), passed[0])
        target = self.out_dir / f"latest_capture{best.path.suffix}"
        try:
            shutil.copyfile(best.path, target)
        except OSError:
            return best.path
        return target

    def write_report(self, env_lines: list[str], node_lines: list[str], latest: Optional[Path]) -> str:
        lines = [
            "ClarifyDeck screenshot spike report",
            "time: " + time.strftime("%Y-%m-%d %H:%M:%S"),
            "out_dir: " + str(self.out_dir),
            "",
            "== environment ==",
            *env_lines,
            "",
            "== wayland displays tried ==",
            *(self.displays or ["(none)"]),
            "",
            "== pipewire nodes ==",
            *(node_lines or ["(none found)"]),
            "",
            "== attempts ==",
        ]
        for attempt in self.attempts:
            status = "PASS" if attempt.ok else "FAIL"
            lines.append(f"[{status}] {attempt.label}")
            lines.append(f"    cmd: {' '.join(attempt.command) if attempt.command else '(none)'}")
            lines.append(f"    rc: {attempt.returncode}  time: {attempt.duration}s  detail: {attempt.detail}")
            if attempt.path:
                lines.append(f"    file: {attempt.path}  {attempt.info}")
            if attempt.stderr:
                lines.append("    stderr: " + attempt.stderr.replace("\n", " | ")[:500])
        lines.append("")
        if latest:
            lines.append("RESULT: PASS")
            lines.append("latest_capture: " + str(latest))
        else:
            lines.append("RESULT: FAIL - no backend produced a usable image")
        report = "\n".join(lines)
        (self.out_dir / "report.txt").write_text(report, encoding="utf-8")
        return report


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ClarifyDeck screenshot spike")
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="output directory")
    parser.add_argument(
        "--backend",
        action="append",
        choices=["all", "grim", "gst", "portal", "fallback"],
        default=None,
        help="backend(s) to run (repeatable, default: all)",
    )
    parser.add_argument("--display", default=None, help="force a single WAYLAND_DISPLAY value")
    parser.add_argument("--as-user", default=None, help="run capture commands as this user (default: deck when root)")
    parser.add_argument("--list", action="store_true", help="only print detection info and exit")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    backends = args.backend or ["all"]
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    as_user = args.as_user if args.as_user is not None else default_user()
    runtime = runtime_dir(as_user)
    runner = Runner(as_user)

    env_lines = collect_env(as_user)
    nodes = pipewire_nodes(runner, as_user)
    video_nodes = [node for node in nodes if node.get("video")]
    node_lines = [
        f"id={node['id']} name={node['name']} class={node['class']} video={node['video']}"
        for node in nodes
    ]
    displays = [args.display] if args.display else candidate_displays(as_user)

    (out_dir / "env.txt").write_text("\n".join(env_lines + [""] + node_lines), encoding="utf-8")

    print(f"ClarifyDeck screenshot spike -> {out_dir}")
    print(f"running as user: {as_user or os.environ.get('USER', '?')}")
    print(f"runtime dir: {runtime}")
    print(f"session: {session_hint(as_user)}")
    print("displays: " + ", ".join(displays))
    print(f"pipewire video nodes: {len(video_nodes)}")
    if not video_nodes:
        print("  (no Video/Source node; raw pipewiresrc cannot work without a portal session)")

    if args.list:
        print("")
        print("\n".join(env_lines))
        print("\n".join(node_lines) if node_lines else "(no pipewire nodes)")
        return 0

    spike = CaptureSpike(out_dir, runner, runtime, displays)
    if "all" in backends or "grim" in backends:
        spike.try_grim()
    if "all" in backends or "gst" in backends:
        spike.try_gst(nodes)
    if "all" in backends or "portal" in backends:
        spike.try_portal_screenshot()
    if "all" in backends or "fallback" in backends:
        spike.try_fallbacks()

    latest = spike.finalize()
    report = spike.write_report(env_lines, node_lines, latest)
    print("")
    print(report)
    return 0 if latest else 1


if __name__ == "__main__":
    raise SystemExit(main())
