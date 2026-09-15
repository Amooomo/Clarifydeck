#!/usr/bin/env python3
"""ClarifyDeck Phase 1B - Gamescope External Overlay PoC.

Creates a fullscreen 1280x800 32-bit ARGB transparent X11 window, tags it as a
Gamescope external overlay, and draws a small "OCR TEST" box at the bottom
center. It is independent of Decky / Steam QAM and must stay visible while the
QAM is closed.

Read-only with respect to the system: it only creates one X11 window. It does
not install anything, does not modify MangoHud or any user config.

Usage (run as deck, in Game Mode with a game running):
    python3 scripts/overlay_poc/overlay_poc.py --display :0
    python3 scripts/overlay_poc/overlay_poc.py --display :0 --duration 20

Verification (from another shell):
    DISPLAY=:0 xprop -id <WINDOW_ID>

Notes:
    - A 32-bit TrueColor (ARGB) visual is required for transparency.
    - Properties are set before XMapWindow so Gamescope classifies the window
      as an external overlay on first sight.
    - No XFixes needed for click-through if GAMESCOPE_EXTERNAL_OVERLAY is set,
      but an empty ShapeInput region is applied when libXfixes is available.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import signal
import sys
import time
from typing import Optional


def load_lib(names: list[str]) -> Optional[ctypes.CDLL]:
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


# --- X11 types -------------------------------------------------------------

Window = ctypes.c_ulong
Atom = ctypes.c_ulong
Colormap = ctypes.c_ulong
Font = ctypes.c_ulong
XID = ctypes.c_ulong


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


def bind() -> None:
    libX11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    libX11.XOpenDisplay.restype = ctypes.c_void_p
    libX11.XDefaultScreen.argtypes = [ctypes.c_void_p]
    libX11.XDefaultScreen.restype = ctypes.c_int
    libX11.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libX11.XRootWindow.restype = ctypes.c_ulong
    libX11.XMatchVisualInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(XVisualInfo),
    ]
    libX11.XMatchVisualInfo.restype = ctypes.c_int
    libX11.XCreateColormap.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_int]
    libX11.XCreateColormap.restype = ctypes.c_ulong
    libX11.XCreateWindow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(XSetWindowAttributes),
    ]
    libX11.XCreateWindow.restype = ctypes.c_ulong
    libX11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    libX11.XInternAtom.restype = ctypes.c_ulong
    libX11.XChangeProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    libX11.XChangeProperty.restype = ctypes.c_int
    libX11.XSetWMHints.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XWMHints)]
    libX11.XSetWMHints.restype = ctypes.c_int
    libX11.XStoreName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p]
    libX11.XStoreName.restype = ctypes.c_int
    libX11.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    libX11.XMapWindow.restype = ctypes.c_int
    libX11.XFlush.argtypes = [ctypes.c_void_p]
    libX11.XFlush.restype = ctypes.c_int
    libX11.XCreateGC.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    libX11.XCreateGC.restype = ctypes.c_void_p
    libX11.XSetForeground.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    libX11.XSetForeground.restype = ctypes.c_int
    libX11.XFillRectangle.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
    ]
    libX11.XFillRectangle.restype = ctypes.c_int
    libX11.XLoadFont.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    libX11.XLoadFont.restype = ctypes.c_ulong
    libX11.XSetFont.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    libX11.XSetFont.restype = ctypes.c_int
    libX11.XQueryTextExtents.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(XCharStruct),
    ]
    libX11.XQueryTextExtents.restype = ctypes.c_int
    libX11.XDrawString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    libX11.XDrawString.restype = ctypes.c_int
    libX11.XPending.argtypes = [ctypes.c_void_p]
    libX11.XPending.restype = ctypes.c_int
    libX11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    libX11.XNextEvent.restype = ctypes.c_int
    libX11.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    libX11.XDestroyWindow.restype = ctypes.c_int
    libX11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    libX11.XCloseDisplay.restype = ctypes.c_int
    libX11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libX11.XSync.restype = ctypes.c_int

    if libXfixes is not None:
        libXfixes.XFixesCreateRegion.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        libXfixes.XFixesCreateRegion.restype = ctypes.c_ulong
        libXfixes.XFixesSetWindowShapeRegion.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        libXfixes.XFixesSetWindowShapeRegion.restype = ctypes.c_int
        libXfixes.XFixesDestroyRegion.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        libXfixes.XFixesDestroyRegion.restype = ctypes.c_int


# X11 constants
CWBackPixel = 1 << 1
CWBorderPixel = 1 << 3
CWOverrideRedirect = 1 << 9
CWEventMask = 1 << 11
CWColormap = 1 << 13
ExposureMask = 1 << 15
StructureNotifyMask = 1 << 17
TrueColor = 4
InputHint = 1 << 0
NormalState = 1
ShapeInput = 2
XA_CARDINAL = 6

running = True


def on_signal(_sig, _frame) -> None:
    global running
    running = False


def set_cardinal(dpy, window: int, name: str, value: int) -> None:
    atom = libX11.XInternAtom(dpy, name.encode(), False)
    v = ctypes.c_ulong(value)
    libX11.XChangeProperty(
        dpy, window, atom, XA_CARDINAL, 32, 0, ctypes.cast(ctypes.byref(v), ctypes.c_char_p), 1
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="ClarifyDeck Gamescope external overlay PoC")
    parser.add_argument("--display", default=None, help="X display, e.g. :0 (default: $DISPLAY)")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--text", default="OCR TEST")
    parser.add_argument("--duration", type=float, default=0.0, help="auto-exit after N seconds (0 = run until killed)")
    parser.add_argument("--override-redirect", action="store_true", help="set override_redirect (not default)")
    parser.add_argument("--no-shape", action="store_true", help="do not apply empty XFixes input region")
    args = parser.parse_args()

    bind()

    dpy = libX11.XOpenDisplay(args.display.encode() if args.display else None)
    if not dpy:
        sys.stderr.write(f"FATAL: cannot open display {args.display or os.environ.get('DISPLAY')}\n")
        return 3

    screen = libX11.XDefaultScreen(dpy)
    root = libX11.XRootWindow(dpy, screen)

    vinfo = XVisualInfo()
    if not libX11.XMatchVisualInfo(dpy, screen, 32, TrueColor, ctypes.byref(vinfo)):
        sys.stderr.write("FATAL: no 32-bit TrueColor (ARGB) visual on this screen\n")
        libX11.XCloseDisplay(dpy)
        return 4

    cmap = libX11.XCreateColormap(dpy, root, vinfo.visual, 0)

    attrs = XSetWindowAttributes()
    attrs.background_pixel = 0
    attrs.border_pixel = 0
    attrs.colormap = cmap
    attrs.event_mask = ExposureMask | StructureNotifyMask
    attrs.override_redirect = 1 if args.override_redirect else 0

    mask = CWBackPixel | CWBorderPixel | CWColormap | CWEventMask
    if args.override_redirect:
        mask |= CWOverrideRedirect

    win = libX11.XCreateWindow(
        dpy,
        root,
        0,
        0,
        args.width,
        args.height,
        0,
        32,
        1,  # InputOutput
        vinfo.visual,
        mask,
        ctypes.byref(attrs),
    )
    if not win:
        sys.stderr.write("FATAL: XCreateWindow failed\n")
        libX11.XCloseDisplay(dpy)
        return 5

    libX11.XStoreName(dpy, win, b"clarifydeck-overlay")

    hints = XWMHints()
    hints.flags = InputHint
    hints.input = 0  # window does not accept input focus
    hints.initial_state = NormalState
    libX11.XSetWMHints(dpy, win, ctypes.byref(hints))

    # Classify as a Gamescope external overlay BEFORE the window is mapped.
    set_cardinal(dpy, win, "GAMESCOPE_EXTERNAL_OVERLAY", 1)
    set_cardinal(dpy, win, "GAMESCOPE_NO_FOCUS", 1)

    if libXfixes is not None and not args.no_shape:
        empty = libXfixes.XFixesCreateRegion(dpy, None, 0)
        libXfixes.XFixesSetWindowShapeRegion(dpy, win, ShapeInput, 0, 0, empty)
        libXfixes.XFixesDestroyRegion(dpy, empty)

    libX11.XFlush(dpy)
    libX11.XMapWindow(dpy, win)
    libX11.XFlush(dpy)
    libX11.XSync(dpy, False)

    gc = libX11.XCreateGC(dpy, win, 0, None)
    font = libX11.XLoadFont(dpy, b"fixed")
    if font:
        libX11.XSetFont(dpy, gc, font)

    def draw() -> None:
        libX11.XSetForeground(dpy, gc, 0x00000000)
        libX11.XFillRectangle(dpy, win, gc, 0, 0, args.width, args.height)

        box_w, box_h = 300, 80
        box_x = (args.width - box_w) // 2
        box_y = args.height - 120
        libX11.XSetForeground(dpy, gc, 0x8C000000)  # black, alpha ~0.55
        libX11.XFillRectangle(dpy, win, gc, box_x, box_y, box_w, box_h)

        libX11.XSetForeground(dpy, gc, 0xFFFFFFFF)
        data = args.text.encode()
        direction = ctypes.c_int()
        ascent = ctypes.c_int()
        descent = ctypes.c_int()
        overall = XCharStruct()
        if font:
            libX11.XQueryTextExtents(
                dpy,
                font,
                data,
                len(data),
                ctypes.byref(direction),
                ctypes.byref(ascent),
                ctypes.byref(descent),
                ctypes.byref(overall),
            )
            text_w = overall.width
            text_x = box_x + (box_w - text_w) // 2
            text_y = box_y + (box_h + ascent.value - descent.value) // 2
            libX11.XDrawString(dpy, win, gc, text_x, text_y, data, len(data))
        libX11.XFlush(dpy)

    draw()

    print("ClarifyDeck external overlay PoC running")
    print(f"  DISPLAY     : {args.display or os.environ.get('DISPLAY')}")
    print(f"  window id   : 0x{win:x}")
    print(f"  size        : {args.width}x{args.height}")
    print(f"  visual depth: {vinfo.depth} (visualid 0x{vinfo.visualid:x})")
    print(f"  XFixes shape: {'yes' if (libXfixes is not None and not args.no_shape) else 'no'}")
    print(f"  override    : {'yes' if args.override_redirect else 'no'}")
    print("")
    print("Verify in another shell:")
    print(f"  DISPLAY={args.display or os.environ.get('DISPLAY')} xprop -id 0x{win:x}")

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    event = ctypes.create_string_buffer(192)
    started = time.time()
    while running:
        while libX11.XPending(dpy):
            libX11.XNextEvent(dpy, ctypes.byref(event))
            if event.raw[0] == 12:  # Expose
                draw()
        if args.duration and (time.time() - started) >= args.duration:
            break
        time.sleep(0.5)

    libX11.XDestroyWindow(dpy, win)
    libX11.XFlush(dpy)
    libX11.XCloseDisplay(dpy)
    print("overlay exited cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
