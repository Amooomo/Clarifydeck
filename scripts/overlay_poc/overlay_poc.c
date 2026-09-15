/*
 * ClarifyDeck Phase 1B - Gamescope External Overlay PoC (C).
 *
 * Fullscreen 32-bit ARGB transparent X11 window tagged with
 * GAMESCOPE_EXTERNAL_OVERLAY=1 and GAMESCOPE_NO_FOCUS=1 before it is mapped,
 * drawing an "OCR TEST" box at the bottom center.
 *
 * Build: ./build.sh   Run: ./run.sh :0
 *
 * Only X11 (and optionally Xfixes) are used. No GTK/Qt/SDL/cairo.
 */

#define _GNU_SOURCE
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/Xatom.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>

#ifdef HAVE_XFIXES
#include <X11/extensions/Xfixes.h>
#endif

static Display *dpy = NULL;
static Window win = 0;
static GC gc = NULL;
static int g_width = 1280;
static int g_height = 800;
static const char *g_text = "OCR TEST";
static int g_shape = 1;
static volatile sig_atomic_t g_running = 1;

static void on_signal(int sig) {
    (void)sig;
    g_running = 0;
}

static void set_cardinal(Window w, const char *name, unsigned long value) {
    Atom atom = XInternAtom(dpy, name, False);
    unsigned long v = value;
    XChangeProperty(dpy, w, atom, XA_CARDINAL, 32, PropModeReplace,
                    (unsigned char *)&v, 1);
}

static void draw(void) {
    int box_w = 300, box_h = 80;
    int box_x = (g_width - box_w) / 2;
    int box_y = g_height - 120;

    XSetForeground(dpy, gc, 0x00000000UL);
    XFillRectangle(dpy, win, gc, 0, 0, g_width, g_height);

    XSetForeground(dpy, gc, 0x8C000000UL); /* black, alpha ~0.55 */
    XFillRectangle(dpy, win, gc, box_x, box_y, box_w, box_h);

    XSetForeground(dpy, gc, 0xFFFFFFFFUL);
    XFontStruct *font = XLoadQueryFont(dpy, "fixed");
    if (font) {
        int tw = XTextWidth(font, g_text, (int)strlen(g_text));
        int tx = box_x + (box_w - tw) / 2;
        int ty = box_y + (box_h + font->ascent - font->descent) / 2;
        XSetFont(dpy, gc, font->fid);
        XDrawString(dpy, win, gc, tx, ty, g_text, (int)strlen(g_text));
        XFreeFont(dpy, font);
    }
    XFlush(dpy);
}

int main(int argc, char **argv) {
    const char *display_name = NULL;
    double duration = 0.0;
    int override_redirect = 0;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--display") && i + 1 < argc) {
            display_name = argv[++i];
        } else if (!strcmp(argv[i], "--width") && i + 1 < argc) {
            g_width = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--height") && i + 1 < argc) {
            g_height = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--text") && i + 1 < argc) {
            g_text = argv[++i];
        } else if (!strcmp(argv[i], "--duration") && i + 1 < argc) {
            duration = atof(argv[++i]);
        } else if (!strcmp(argv[i], "--override-redirect")) {
            override_redirect = 1;
        } else if (!strcmp(argv[i], "--no-shape")) {
            g_shape = 0;
        }
    }

    dpy = XOpenDisplay(display_name);
    if (!dpy) {
        fprintf(stderr, "FATAL: cannot open display %s\n",
                display_name ? display_name : getenv("DISPLAY"));
        return 3;
    }

    int screen = DefaultScreen(dpy);
    Window root = RootWindow(dpy, screen);

    XVisualInfo vinfo;
    if (!XMatchVisualInfo(dpy, screen, 32, TrueColor, &vinfo)) {
        fprintf(stderr, "FATAL: no 32-bit TrueColor (ARGB) visual\n");
        XCloseDisplay(dpy);
        return 4;
    }

    Colormap cmap = XCreateColormap(dpy, root, vinfo.visual, AllocNone);

    XSetWindowAttributes attrs;
    memset(&attrs, 0, sizeof(attrs));
    attrs.background_pixel = 0;
    attrs.border_pixel = 0;
    attrs.colormap = cmap;
    attrs.event_mask = ExposureMask | StructureNotifyMask;
    attrs.override_redirect = override_redirect ? True : False;

    unsigned long mask = CWBackPixel | CWBorderPixel | CWColormap | CWEventMask;
    if (override_redirect)
        mask |= CWOverrideRedirect;

    win = XCreateWindow(dpy, root, 0, 0, g_width, g_height, 0, 32, InputOutput,
                        vinfo.visual, mask, &attrs);
    if (!win) {
        fprintf(stderr, "FATAL: XCreateWindow failed\n");
        XCloseDisplay(dpy);
        return 5;
    }

    XStoreName(dpy, win, "clarifydeck-overlay");

    XWMHints hints;
    memset(&hints, 0, sizeof(hints));
    hints.flags = InputHint;
    hints.input = False;
    hints.initial_state = NormalState;
    XSetWMHints(dpy, win, &hints);

    /* Classify before mapping. */
    set_cardinal(win, "GAMESCOPE_EXTERNAL_OVERLAY", 1);
    set_cardinal(win, "GAMESCOPE_NO_FOCUS", 1);

#ifdef HAVE_XFIXES
    if (g_shape) {
        XserverRegion empty = XFixesCreateRegion(dpy, NULL, 0);
        XFixesSetWindowShapeRegion(dpy, win, ShapeInput, 0, 0, empty);
        XFixesDestroyRegion(dpy, empty);
    }
#else
    (void)g_shape;
#endif

    XFlush(dpy);
    XMapWindow(dpy, win);
    XFlush(dpy);
    XSync(dpy, False);

    gc = XCreateGC(dpy, win, 0, NULL);
    draw();

    printf("ClarifyDeck external overlay PoC running\n");
    printf("  DISPLAY     : %s\n", display_name ? display_name : getenv("DISPLAY"));
    printf("  window id   : 0x%lx\n", (unsigned long)win);
    printf("  size        : %dx%d\n", g_width, g_height);
    printf("  visual depth: %d (visualid 0x%lx)\n", vinfo.depth,
           (unsigned long)vinfo.visualid);
    printf("  override    : %s\n", override_redirect ? "yes" : "no");
    printf("\nVerify: DISPLAY=%s xprop -id 0x%lx\n",
           display_name ? display_name : getenv("DISPLAY"), (unsigned long)win);

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    XEvent ev;
    time_t start = time(NULL);
    while (g_running) {
        while (XPending(dpy)) {
            XNextEvent(dpy, &ev);
            if (ev.type == Expose)
                draw();
        }
        if (duration > 0 && difftime(time(NULL), start) >= duration)
            break;
        usleep(500000);
    }

    XDestroyWindow(dpy, win);
    XFlush(dpy);
    XCloseDisplay(dpy);
    printf("overlay exited cleanly\n");
    return 0;
}
