#!/usr/bin/env python3
"""ClarifyDeck persistent Gamescope external overlay renderer.

A long-running, standalone process that owns exactly one X11 window tagged as a
Gamescope external overlay and draws the OCR caption on demand. It is fully
independent of Decky / the Steam QAM React tree.

It listens on an AF_UNIX socket and consumes newline-delimited JSON:
    {"type":"show","text":"..."}   {"type":"update","text":"..."}
    {"type":"hide"}                {"type":"shutdown"}   {"type":"ping"}

The window is created once and never recreated on updates. Text is rendered with
cairo (UTF-8, CJK) when available, otherwise a plain Xlib ASCII fallback.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import math
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Linux
    fcntl = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol  # noqa: E402


def load_lib(names: list[str]):
    for name in names:
        found = ctypes.util.find_library(name)
        for candidate in (found, f"lib{name}.so.6", f"lib{name}.so.3", f"lib{name}.so"):
            if not candidate:
                continue
            try:
                return ctypes.CDLL(candidate)
            except OSError:
                continue
    return None


libX11 = load_lib(["X11"])
if libX11 is None:
    sys.stderr.write("FATAL: libX11 not found\n")
    sys.exit(2)
libXfixes = load_lib(["Xfixes"])
libcairo = load_lib(["cairo"])


# --------------------------------------------------------------------------
# X11 types / bindings
# --------------------------------------------------------------------------

class XSetWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("background_pixmap", ctypes.c_ulong),
        ("background_pixel", ctypes.c_ulong),
        ("border_pixmap", ctypes.c_ulong),
        ("border_pixel", ctypes.c_ulong),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("cursor", ctypes.c_ulong),
    ]


class XVisualInfo(ctypes.Structure):
    _fields_ = [
        ("visual", ctypes.c_void_p),
        ("visualid", ctypes.c_ulong),
        ("screen", ctypes.c_int),
        ("depth", ctypes.c_uint),
        ("c_class", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("colormap_size", ctypes.c_int),
        ("bits_per_rgb", ctypes.c_int),
    ]


class XWMHints(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_long),
        ("input", ctypes.c_int),
        ("initial_state", ctypes.c_int),
        ("icon_pixmap", ctypes.c_ulong),
        ("icon_window", ctypes.c_ulong),
        ("icon_x", ctypes.c_int),
        ("icon_y", ctypes.c_int),
        ("icon_mask", ctypes.c_ulong),
        ("window_group", ctypes.c_ulong),
    ]


class XCharStruct(ctypes.Structure):
    _fields_ = [
        ("lbearing", ctypes.c_short),
        ("rbearing", ctypes.c_short),
        ("width", ctypes.c_short),
        ("ascent", ctypes.c_short),
        ("descent", ctypes.c_short),
        ("attributes", ctypes.c_ushort),
    ]


def _bind_x11() -> None:
    libX11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    libX11.XOpenDisplay.restype = ctypes.c_void_p
    libX11.XDefaultScreen.argtypes = [ctypes.c_void_p]
    libX11.XDefaultScreen.restype = ctypes.c_int
    libX11.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libX11.XRootWindow.restype = ctypes.c_ulong
    libX11.XMatchVisualInfo.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(XVisualInfo)]
    libX11.XMatchVisualInfo.restype = ctypes.c_int
    libX11.XCreateColormap.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_int]
    libX11.XCreateColormap.restype = ctypes.c_ulong
    libX11.XCreateWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XSetWindowAttributes)]
    libX11.XCreateWindow.restype = ctypes.c_ulong
    libX11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    libX11.XInternAtom.restype = ctypes.c_ulong
    libX11.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    libX11.XChangeProperty.restype = ctypes.c_int
    libX11.XSetWMHints.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XWMHints)]
    libX11.XSetWMHints.restype = ctypes.c_int
    libX11.XStoreName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p]
    libX11.XStoreName.restype = ctypes.c_int
    libX11.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    libX11.XMapWindow.restype = ctypes.c_int
    libX11.XFlush.argtypes = [ctypes.c_void_p]
    libX11.XFlush.restype = ctypes.c_int
    libX11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libX11.XSync.restype = ctypes.c_int
    libX11.XCreateGC.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    libX11.XCreateGC.restype = ctypes.c_void_p
    libX11.XSetForeground.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    libX11.XSetForeground.restype = ctypes.c_int
    libX11.XFillRectangle.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    libX11.XFillRectangle.restype = ctypes.c_int
    libX11.XLoadFont.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    libX11.XLoadFont.restype = ctypes.c_ulong
    libX11.XSetFont.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    libX11.XSetFont.restype = ctypes.c_int
    libX11.XQueryTextExtents.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(XCharStruct)]
    libX11.XQueryTextExtents.restype = ctypes.c_int
    libX11.XDrawString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    libX11.XDrawString.restype = ctypes.c_int
    libX11.XDrawRectangle.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    libX11.XDrawRectangle.restype = ctypes.c_int
    libX11.XPending.argtypes = [ctypes.c_void_p]
    libX11.XPending.restype = ctypes.c_int
    libX11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    libX11.XNextEvent.restype = ctypes.c_int
    libX11.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    libX11.XDestroyWindow.restype = ctypes.c_int
    libX11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    libX11.XCloseDisplay.restype = ctypes.c_int
    libX11.XConnectionNumber.argtypes = [ctypes.c_void_p]
    libX11.XConnectionNumber.restype = ctypes.c_int

    if libXfixes is not None:
        libXfixes.XFixesCreateRegion.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        libXfixes.XFixesCreateRegion.restype = ctypes.c_ulong
        libXfixes.XFixesSetWindowShapeRegion.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        libXfixes.XFixesSetWindowShapeRegion.restype = ctypes.c_int
        libXfixes.XFixesDestroyRegion.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        libXfixes.XFixesDestroyRegion.restype = ctypes.c_int


CWBackPixel = 1 << 1
CWBorderPixel = 1 << 3
CWEventMask = 1 << 11
CWColormap = 1 << 13
ExposureMask = 1 << 15
StructureNotifyMask = 1 << 17
TrueColor = 4
InputHint = 1 << 0
NormalState = 1
ShapeInput = 2
XA_CARDINAL = 6


# --------------------------------------------------------------------------
# cairo bindings (optional)
# --------------------------------------------------------------------------

class cairo_text_extents_t(ctypes.Structure):
    _fields_ = [
        ("x_bearing", ctypes.c_double),
        ("y_bearing", ctypes.c_double),
        ("width", ctypes.c_double),
        ("height", ctypes.c_double),
        ("x_advance", ctypes.c_double),
        ("y_advance", ctypes.c_double),
    ]


def _bind_cairo() -> None:
    if libcairo is None:
        return
    libcairo.cairo_xlib_surface_create.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    libcairo.cairo_xlib_surface_create.restype = ctypes.c_void_p
    libcairo.cairo_create.argtypes = [ctypes.c_void_p]
    libcairo.cairo_create.restype = ctypes.c_void_p
    libcairo.cairo_destroy.argtypes = [ctypes.c_void_p]
    libcairo.cairo_destroy.restype = None
    libcairo.cairo_surface_destroy.argtypes = [ctypes.c_void_p]
    libcairo.cairo_surface_destroy.restype = None
    libcairo.cairo_surface_flush.argtypes = [ctypes.c_void_p]
    libcairo.cairo_surface_flush.restype = None
    libcairo.cairo_set_source_rgba.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
    libcairo.cairo_set_source_rgba.restype = None
    libcairo.cairo_set_operator.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libcairo.cairo_set_operator.restype = None
    libcairo.cairo_paint.argtypes = [ctypes.c_void_p]
    libcairo.cairo_paint.restype = None
    libcairo.cairo_rectangle.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
    libcairo.cairo_rectangle.restype = None
    libcairo.cairo_fill.argtypes = [ctypes.c_void_p]
    libcairo.cairo_fill.restype = None
    libcairo.cairo_select_font_face.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    libcairo.cairo_select_font_face.restype = None
    libcairo.cairo_set_font_size.argtypes = [ctypes.c_void_p, ctypes.c_double]
    libcairo.cairo_set_font_size.restype = None
    libcairo.cairo_move_to.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    libcairo.cairo_move_to.restype = None
    libcairo.cairo_show_text.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    libcairo.cairo_show_text.restype = None
    libcairo.cairo_text_extents.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(cairo_text_extents_t)]
    libcairo.cairo_text_extents.restype = None
    libcairo.cairo_stroke.argtypes = [ctypes.c_void_p]
    libcairo.cairo_stroke.restype = None
    libcairo.cairo_set_line_width.argtypes = [ctypes.c_void_p, ctypes.c_double]
    libcairo.cairo_set_line_width.restype = None
    libcairo.cairo_set_dash.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_double), ctypes.c_int, ctypes.c_double]
    libcairo.cairo_set_dash.restype = None
    libcairo.cairo_save.argtypes = [ctypes.c_void_p]
    libcairo.cairo_save.restype = None
    libcairo.cairo_restore.argtypes = [ctypes.c_void_p]
    libcairo.cairo_restore.restype = None
    libcairo.cairo_clip.argtypes = [ctypes.c_void_p]
    libcairo.cairo_clip.restype = None


def resolve_cjk_font() -> str:
    for args in (["fc-match", "-f", "%{family}", ":lang=zh"], ["fc-match", "-f", "%{family}", "sans"]):
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        family = (out.stdout or "").split(",")[0].strip()
        if family:
            return family
    return "sans"


# --------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------

class OverlayRenderer:
    def __init__(self, display_name, width, height, font_size, background_alpha, debug):
        self.display_name = display_name
        self.width = width
        self.height = height
        self.font_size = font_size
        self.background_alpha = background_alpha
        self.debug = debug
        self.dpy = None
        self.win = 0
        self.gc = None
        self.font = 0
        self.visual = None
        self.cairo_surface = None
        self.cairo = None
        self.font_family = "sans"
        self.visible = False
        self.text = ""
        self.preview_regions: list = []
        # region_id -> {"rect": {"x","y","w","h"}, "text": str}
        self.region_text: dict = {}

    def log(self, message: str) -> None:
        sys.stderr.write(f"[renderer] {message}\n")
        sys.stderr.flush()

    def log_region_latency(
        self,
        region_id: str,
        source_seq,
        stable_text_monotonic,
        captured_monotonic,
    ) -> None:
        """Phase 2N.3: log changed-text receive/draw latency (read-only)."""
        if stable_text_monotonic is None and captured_monotonic is None:
            return
        now = time.monotonic()
        parts = [f"[latency] region={region_id} frame={source_seq}"]
        if stable_text_monotonic is not None:
            parts.append("renderer_ms=%.3f" % ((now - float(stable_text_monotonic)) * 1000.0))
        if captured_monotonic is not None:
            parts.append(
                "frame_age_at_render_ms=%.3f" % ((now - float(captured_monotonic)) * 1000.0)
            )
        self.log(" ".join(parts))

    def open(self) -> None:
        self.dpy = libX11.XOpenDisplay(self.display_name.encode() if self.display_name else None)
        if not self.dpy:
            raise RuntimeError(f"cannot open display {self.display_name}")
        screen = libX11.XDefaultScreen(self.dpy)
        root = libX11.XRootWindow(self.dpy, screen)
        vinfo = XVisualInfo()
        if not libX11.XMatchVisualInfo(self.dpy, screen, 32, TrueColor, ctypes.byref(vinfo)):
            raise RuntimeError("no 32-bit TrueColor (ARGB) visual")
        self.visual = vinfo.visual
        cmap = libX11.XCreateColormap(self.dpy, root, vinfo.visual, 0)
        attrs = XSetWindowAttributes()
        attrs.background_pixel = 0
        attrs.border_pixel = 0
        attrs.colormap = cmap
        attrs.event_mask = ExposureMask | StructureNotifyMask
        self.win = libX11.XCreateWindow(
            self.dpy, root, 0, 0, self.width, self.height, 0, 32, 1, vinfo.visual,
            CWBackPixel | CWBorderPixel | CWColormap | CWEventMask, ctypes.byref(attrs),
        )
        if not self.win:
            raise RuntimeError("XCreateWindow failed")
        libX11.XStoreName(self.dpy, self.win, b"clarifydeck-overlay")
        hints = XWMHints()
        hints.flags = InputHint
        hints.input = 0
        hints.initial_state = NormalState
        libX11.XSetWMHints(self.dpy, self.win, ctypes.byref(hints))
        self._set_cardinal("GAMESCOPE_EXTERNAL_OVERLAY", 1)
        self._set_cardinal("GAMESCOPE_NO_FOCUS", 1)
        if libXfixes is not None:
            empty = libXfixes.XFixesCreateRegion(self.dpy, None, 0)
            libXfixes.XFixesSetWindowShapeRegion(self.dpy, self.win, ShapeInput, 0, 0, empty)
            libXfixes.XFixesDestroyRegion(self.dpy, empty)
        libX11.XFlush(self.dpy)
        libX11.XMapWindow(self.dpy, self.win)
        libX11.XFlush(self.dpy)
        libX11.XSync(self.dpy, False)
        self.gc = libX11.XCreateGC(self.dpy, self.win, 0, None)
        self.font = libX11.XLoadFont(self.dpy, b"fixed")
        if self.font:
            libX11.XSetFont(self.dpy, self.gc, self.font)
        if libcairo is not None:
            self.font_family = resolve_cjk_font()
            self.cairo_surface = libcairo.cairo_xlib_surface_create(self.dpy, self.win, self.visual, self.width, self.height)
            self.cairo = libcairo.cairo_create(self.cairo_surface)
            self.log(f"cairo enabled, font family: {self.font_family}")
        else:
            self.log("WARNING: libcairo not found; CJK text will not render (ASCII fallback only)")

    def _set_cardinal(self, name: str, value: int) -> None:
        atom = libX11.XInternAtom(self.dpy, name.encode(), False)
        v = ctypes.c_ulong(value)
        libX11.XChangeProperty(self.dpy, self.win, atom, XA_CARDINAL, 32, 0, ctypes.cast(ctypes.byref(v), ctypes.c_char_p), 1)

    def draw(self) -> None:
        if self.cairo is not None:
            self._draw_cairo()
        else:
            self._draw_xlib()
        libX11.XFlush(self.dpy)

    def _draw_cairo(self) -> None:
        libcairo.cairo_set_operator(self.cairo, 1)  # SOURCE
        libcairo.cairo_set_source_rgba(self.cairo, 0.0, 0.0, 0.0, 0.0)
        libcairo.cairo_paint(self.cairo)
        libcairo.cairo_set_operator(self.cairo, 2)  # OVER
        if self.visible and self.text:
            self._draw_text_cairo()
        if self.region_text:
            self._draw_region_text_cairo()
        if self.preview_regions:
            self._draw_preview_cairo()
        libcairo.cairo_surface_flush(self.cairo_surface)

    def _draw_text_cairo(self) -> None:
        lines = self.text.split("\n") or [""]
        line_h = self.font_size * 1.3
        padding = 12.0
        libcairo.cairo_select_font_face(self.cairo, self.font_family.encode(), 0, 0)
        libcairo.cairo_set_font_size(self.cairo, float(self.font_size))
        max_w = 0.0
        for line in lines:
            ext = cairo_text_extents_t()
            libcairo.cairo_text_extents(self.cairo, line.encode("utf-8"), ctypes.byref(ext))
            max_w = max(max_w, ext.x_advance)
        box_w = max(200.0, min(self.width - 40.0, max_w + padding * 2))
        box_h = line_h * len(lines) + padding * 2
        box_x = (self.width - box_w) / 2.0
        box_y = self.height - box_h - 40.0
        libcairo.cairo_set_source_rgba(self.cairo, 0.0, 0.0, 0.0, self.background_alpha)
        libcairo.cairo_rectangle(self.cairo, box_x, box_y, box_w, box_h)
        libcairo.cairo_fill(self.cairo)
        libcairo.cairo_set_source_rgba(self.cairo, 1.0, 1.0, 1.0, 1.0)
        for index, line in enumerate(lines):
            ext = cairo_text_extents_t()
            libcairo.cairo_text_extents(self.cairo, line.encode("utf-8"), ctypes.byref(ext))
            tx = box_x + (box_w - ext.x_advance) / 2.0
            ty = box_y + padding + self.font_size + index * line_h
            libcairo.cairo_move_to(self.cairo, tx, ty)
            libcairo.cairo_show_text(self.cairo, line.encode("utf-8"))

    def _measure_cairo(self, candidate: str) -> float:
        ext = cairo_text_extents_t()
        libcairo.cairo_text_extents(self.cairo, candidate.encode("utf-8"), ctypes.byref(ext))
        return ext.x_advance

    def _draw_region_text_cairo(self) -> None:
        padding = 8.0
        for block in self.region_text.values():
            rect = block["rect"]
            left = rect["x"] * self.width
            top = rect["y"] * self.height
            width = rect["w"] * self.width
            height = rect["h"] * self.height
            text = block["text"]
            if width <= 2 * padding or height <= 2 * padding or not text:
                continue
            font_size = float(
                protocol.sanitize_region_font_size(block.get("font_size"))
                or protocol.DEFAULT_REGION_FONT_SIZE
            )
            line_h = protocol.region_line_height(font_size)
            text_rgba, panel_rgba = protocol.style_colors(
                block.get("style"), block.get("panel_opacity")
            )
            libcairo.cairo_save(self.cairo)
            libcairo.cairo_rectangle(self.cairo, left, top, width, height)
            libcairo.cairo_clip(self.cairo)
            libcairo.cairo_select_font_face(self.cairo, self.font_family.encode(), 0, 0)
            libcairo.cairo_set_font_size(self.cairo, font_size)
            # Semi-transparent panel fills the exact configured region rectangle.
            libcairo.cairo_set_source_rgba(self.cairo, *panel_rgba)
            libcairo.cairo_rectangle(self.cairo, left, top, width, height)
            libcairo.cairo_fill(self.cairo)
            libcairo.cairo_set_source_rgba(self.cairo, *text_rgba)
            lines = protocol.wrap_text(text, width - 2 * padding, self._measure_cairo)
            lines = protocol.clip_lines(lines, line_h, height - 2 * padding)
            baseline = top + padding + font_size
            for line in lines:
                libcairo.cairo_move_to(self.cairo, left + padding, baseline)
                libcairo.cairo_show_text(self.cairo, line.encode("utf-8"))
                baseline += line_h
            libcairo.cairo_restore(self.cairo)

    def _draw_region_text_xlib(self) -> None:
        font_size = 20
        line_h = font_size + 6
        padding = 8
        for block in self.region_text.values():
            rect = block["rect"]
            left = int(rect["x"] * self.width)
            top = int(rect["y"] * self.height)
            width = int(rect["w"] * self.width)
            height = int(rect["h"] * self.height)
            text = block["text"]
            if width <= 2 * padding or height <= 2 * padding or not text:
                continue
            lines = protocol.wrap_text(text, max(1, width - 2 * padding), lambda candidate: len(candidate) * 14)
            lines = protocol.clip_lines(lines, line_h, height - 2 * padding)
            libX11.XSetForeground(self.dpy, self.gc, 0xFFFFFFFF)
            baseline = top + padding + font_size
            for line in lines:
                data = line.encode("utf-8", errors="replace")
                libX11.XDrawString(self.dpy, self.win, self.gc, left + padding, baseline, data, len(data))
                baseline += line_h

    def _draw_preview_cairo(self) -> None:
        for region in self.preview_regions:
            left, top, width, height = protocol.preview_pixel_rect(region, self.width, self.height)
            alpha = 1.0 if region["enabled"] else 0.4
            if region["selected"]:
                libcairo.cairo_set_source_rgba(self.cairo, 1.0, 0.85, 0.0, alpha)
                libcairo.cairo_set_line_width(self.cairo, 4.0)
            elif region["primary"]:
                libcairo.cairo_set_source_rgba(self.cairo, 0.4, 0.85, 1.0, alpha)
                libcairo.cairo_set_line_width(self.cairo, 3.0)
            else:
                libcairo.cairo_set_source_rgba(self.cairo, 0.85, 0.85, 0.85, alpha)
                libcairo.cairo_set_line_width(self.cairo, 2.0)
            if region["enabled"]:
                libcairo.cairo_set_dash(self.cairo, None, 0, 0.0)
            else:
                dashes = (ctypes.c_double * 2)(6.0, 6.0)
                libcairo.cairo_set_dash(self.cairo, dashes, 2, 0.0)
            libcairo.cairo_rectangle(self.cairo, left, top, width, height)
            libcairo.cairo_stroke(self.cairo)
            libcairo.cairo_set_dash(self.cairo, None, 0, 0.0)
            label = region.get("label") or ""
            if label:
                libcairo.cairo_select_font_face(self.cairo, self.font_family.encode(), 0, 0)
                libcairo.cairo_set_font_size(self.cairo, 14.0)
                libcairo.cairo_set_source_rgba(self.cairo, 1.0, 1.0, 1.0, alpha)
                libcairo.cairo_move_to(self.cairo, left + 2.0, max(14.0, top - 4.0))
                libcairo.cairo_show_text(self.cairo, label.encode("utf-8"))

    def _draw_xlib(self) -> None:
        libX11.XSetForeground(self.dpy, self.gc, 0x00000000)
        libX11.XFillRectangle(self.dpy, self.win, self.gc, 0, 0, self.width, self.height)
        if self.visible and self.text:
            self._draw_text_xlib()
        if self.region_text:
            self._draw_region_text_xlib()
        if self.preview_regions:
            self._draw_preview_xlib()

    def _draw_preview_xlib(self) -> None:
        for region in self.preview_regions:
            left, top, width, height = protocol.preview_pixel_rect(region, self.width, self.height)
            x = int(left)
            y = int(top)
            w = max(1, int(width))
            h = max(1, int(height))
            if region["selected"]:
                color = 0xFFFFD800
            elif region["primary"]:
                color = 0xFF66D9FF
            else:
                color = 0xFFD9D9D9
            if not region["enabled"]:
                color = 0x80D9D9D9
            libX11.XSetForeground(self.dpy, self.gc, color)
            libX11.XDrawRectangle(self.dpy, self.win, self.gc, x, y, w, h)
            if region["selected"]:
                libX11.XDrawRectangle(self.dpy, self.win, self.gc, max(0, x - 1), max(0, y - 1), w + 2, h + 2)
            label = region.get("label") or ""
            if label:
                data = label.encode("utf-8", errors="replace")
                libX11.XDrawString(self.dpy, self.win, self.gc, x + 2, max(12, y - 4), data, len(data))

    def _draw_text_xlib(self) -> None:
        lines = self.text.split("\n") or [""]
        line_h = self.font_size + 8
        box_w = 300
        box_h = line_h * len(lines) + 16
        box_x = (self.width - box_w) // 2
        box_y = self.height - box_h - 40
        libX11.XSetForeground(self.dpy, self.gc, 0x8C000000)
        libX11.XFillRectangle(self.dpy, self.win, self.gc, box_x, box_y, box_w, box_h)
        libX11.XSetForeground(self.dpy, self.gc, 0xFFFFFFFF)
        for index, line in enumerate(lines):
            data = line.encode("utf-8", errors="replace")
            ascent = ctypes.c_int()
            descent = ctypes.c_int()
            direction = ctypes.c_int()
            overall = XCharStruct()
            if self.font:
                libX11.XQueryTextExtents(self.dpy, self.font, data, len(data), ctypes.byref(direction), ctypes.byref(ascent), ctypes.byref(descent), ctypes.byref(overall))
            tx = box_x + 12
            ty = box_y + 8 + (index + 1) * line_h - 4
            libX11.XDrawString(self.dpy, self.win, self.gc, tx, ty, data, len(data))

    def handle_event(self) -> None:
        event = ctypes.create_string_buffer(192)
        while libX11.XPending(self.dpy):
            libX11.XNextEvent(self.dpy, ctypes.byref(event))
            if event.raw[0] == 12:  # Expose
                self.draw()

    def close(self) -> None:
        try:
            if self.cairo is not None:
                libcairo.cairo_destroy(self.cairo)
                self.cairo = None
            if self.cairo_surface is not None:
                libcairo.cairo_surface_destroy(self.cairo_surface)
                self.cairo_surface = None
            if self.win and self.dpy:
                libX11.XDestroyWindow(self.dpy, self.win)
                libX11.XFlush(self.dpy)
            if self.dpy:
                libX11.XCloseDisplay(self.dpy)
        except Exception:
            pass
        self.win = 0


# --------------------------------------------------------------------------
# Singleton lock / parent lifetime
# --------------------------------------------------------------------------

_renderer_lock_fh = None


def acquire_renderer_lock() -> bool:
    global _renderer_lock_fh
    if fcntl is None:
        return True
    lock_path = protocol.renderer_lock_path()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            fh.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return False
    _renderer_lock_fh = fh
    return True


def setup_parent_death() -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# --------------------------------------------------------------------------
# Socket server
# --------------------------------------------------------------------------

def serve(renderer: OverlayRenderer, sock_path: Path, parent_pid: int = 0) -> int:
    sock_path.parent.mkdir(parents=True, exist_ok=True)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(sock_path))
                probe.close()
                renderer.log("another renderer is already listening; exiting")
                return 0
            except OSError:
                renderer.log("removing stale overlay socket")
                try:
                    sock_path.unlink()
                except OSError:
                    pass
            server.bind(str(sock_path))
        else:
            raise
    server.listen(4)
    server.setblocking(False)
    os.chmod(str(sock_path), 0o600)

    renderer.log(f"listening on {sock_path} (window 0x{renderer.win:x})")

    selector = selectors.DefaultSelector()
    selector.register(server, selectors.EVENT_READ, None)
    x_fd = libX11.XConnectionNumber(renderer.dpy)
    selector.register(x_fd, selectors.EVENT_READ, "x11")
    buffers: dict[int, bytearray] = {}

    renderer.draw()

    try:
        while True:
            if parent_pid and not parent_alive(parent_pid):
                renderer.log(f"parent {parent_pid} is gone; exiting")
                break
            for key, _mask in selector.select(timeout=1.0):
                if key.data == "x11":
                    renderer.handle_event()
                    continue
                if key.fileobj is server:
                    client, _ = server.accept()
                    client.setblocking(False)
                    selector.register(client, selectors.EVENT_READ, "client")
                    buffers[client.fileno()] = bytearray()
                    renderer.log("client connected")
                    continue
                client = key.fileobj
                try:
                    chunk = client.recv(4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(client)
                    buffers.pop(client.fileno(), None)
                    client.close()
                    renderer.log("client disconnected")
                    continue
                buf = buffers.setdefault(client.fileno(), bytearray())
                buf.extend(chunk)
                if len(buf) > protocol.MAX_MESSAGE_BYTES:
                    buf.clear()
                    continue
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    buf[:] = rest
                    payload = protocol.decode_message(bytes(line))
                    if payload is None:
                        continue
                    action = payload.get("type")
                    if action == "shutdown":
                        renderer.log("shutdown requested")
                        return 0
                    if action == "ping":
                        try:
                            client.sendall(
                                protocol.encode_message(
                                    {
                                        "type": "pong",
                                        "component": "clarifydeck-overlay-renderer",
                                        "pid": os.getpid(),
                                        "protocol": 1,
                                    }
                                )
                            )
                        except OSError:
                            pass
                        continue
                    if action == "hide":
                        renderer.visible = False
                        renderer.draw()
                        continue
                    if action in ("show", "update"):
                        text = protocol.truncate_text(str(payload.get("text", "")))
                        if not text:
                            renderer.visible = False
                            renderer.draw()
                            continue
                        renderer.text = text
                        renderer.visible = True
                        renderer.draw()
                        if renderer.debug:
                            renderer.log(f"update: {text!r}")
                        continue
                    if action == "set_region_preview":
                        renderer.preview_regions = protocol.sanitize_preview_regions(payload.get("regions"))
                        renderer.draw()
                        if renderer.debug:
                            rects = [
                                protocol.preview_pixel_rect(region, renderer.width, renderer.height)
                                for region in renderer.preview_regions
                            ]
                            renderer.log(
                                f"preview count={len(renderer.preview_regions)} "
                                f"surface={renderer.width}x{renderer.height} rects={rects}"
                            )
                        continue
                    if action == "clear_region_preview":
                        renderer.preview_regions = []
                        renderer.draw()
                        continue
                    if action == "set_region_text":
                        region_id = payload.get("region_id")
                        block = protocol.sanitize_region_text(region_id, payload.get("rect"), payload.get("text", ""))
                        if block is not None:
                            block["style"] = (
                                protocol.sanitize_region_style(payload.get("style")) or protocol.DEFAULT_STYLE
                            )
                            block["font_size"] = (
                                protocol.sanitize_region_font_size(payload.get("font_size"))
                                or protocol.DEFAULT_REGION_FONT_SIZE
                            )
                            panel_opacity = protocol.sanitize_panel_opacity(
                                payload.get("panel_opacity")
                            )
                            block["panel_opacity"] = (
                                panel_opacity
                                if panel_opacity is not None
                                else protocol.DEFAULT_PANEL_OPACITY
                            )
                            renderer.region_text[str(region_id)] = block
                            renderer.draw()
                            renderer.log_region_latency(
                                str(region_id),
                                payload.get("source_seq"),
                                payload.get("stable_text_monotonic"),
                                payload.get("captured_monotonic"),
                            )
                        continue
                    if action == "set_region_style":
                        region_id = str(payload.get("region_id", ""))
                        style = protocol.sanitize_region_style(payload.get("style"))
                        block = renderer.region_text.get(region_id)
                        if block is not None and style is not None:
                            block["style"] = style
                            renderer.draw()
                        continue
                    if action == "set_region_font_size":
                        region_id = str(payload.get("region_id", ""))
                        font_size = protocol.sanitize_region_font_size(payload.get("font_size"))
                        block = renderer.region_text.get(region_id)
                        if block is not None and font_size is not None:
                            block["font_size"] = font_size
                            renderer.draw()
                        continue
                    if action == "set_region_panel_opacity":
                        region_id = str(payload.get("region_id", ""))
                        panel_opacity = protocol.sanitize_panel_opacity(
                            payload.get("panel_opacity")
                        )
                        block = renderer.region_text.get(region_id)
                        if block is not None and panel_opacity is not None:
                            block["panel_opacity"] = panel_opacity
                            renderer.draw()
                        continue
                    if action == "hide_region_text":
                        renderer.region_text.pop(str(payload.get("region_id", "")), None)
                        renderer.draw()
                        continue
                    if action == "clear_all_region_text":
                        renderer.region_text = {}
                        renderer.draw()
                        continue
    finally:
        selector.close()
        server.close()
        try:
            sock_path.unlink()
        except OSError:
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck external overlay renderer")
    parser.add_argument("--socket", default=None)
    parser.add_argument("--display", default=None)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--font-size", type=int, default=protocol.DEFAULT_FONT_SIZE)
    parser.add_argument("--background-alpha", type=float, default=protocol.DEFAULT_BACKGROUND_ALPHA)
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    _bind_x11()
    _bind_cairo()

    # Singleton must be acquired BEFORE any X11 call / window creation.
    if not acquire_renderer_lock():
        sys.stderr.write(
            "[renderer] renderer_lock=busy; another ClarifyDeck renderer already "
            "exists; exiting before X11 window creation\n"
        )
        return 0

    actual_ppid = os.getppid()
    if args.parent_pid and actual_ppid != args.parent_pid:
        # e.g. the sudo fallback: real parent is sudo, not the backend. Do not
        # bind PDEATHSIG to the wrong process; the --parent-pid watchdog still
        # covers the backend lifetime.
        sys.stderr.write(
            f"[renderer] pdeathsig=disabled(parent mismatch expected={args.parent_pid} "
            f"actual={actual_ppid})\n"
        )
    else:
        setup_parent_death()
    if args.parent_pid and not parent_alive(args.parent_pid):
        sys.stderr.write("[renderer] parent already gone; exiting\n")
        return 0

    sock_path = Path(args.socket) if args.socket else protocol.socket_path()
    renderer = OverlayRenderer(args.display, args.width, args.height, args.font_size, args.background_alpha, args.debug)
    try:
        renderer.open()
    except Exception as exc:
        sys.stderr.write(f"[renderer] FATAL: {exc}\n")
        return 1
    renderer.log(
        f"pid={os.getpid()} ppid={os.getppid()} uid={os.getuid()} "
        f"display={args.display} window=0x{renderer.win:x} socket={sock_path} "
        f"renderer_lock=acquired pdeathsig="
        f"{'on' if (args.parent_pid and actual_ppid == args.parent_pid) else 'off'}"
    )
    try:
        return serve(renderer, sock_path, args.parent_pid)
    except KeyboardInterrupt:
        return 0
    finally:
        renderer.log("shutting down: destroying window and releasing renderer lock")
        renderer.close()
        if _renderer_lock_fh is not None:
            try:
                fcntl.flock(_renderer_lock_fh.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                _renderer_lock_fh.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
