# PHASE 1C.2 AUDIT — Phase 1C lifecycle / singleton / restart safety

Audit of the Phase 1C code (`main.py`, `overlay_manager.py`, `overlay/renderer.py`,
`overlay/protocol.py`, `scripts/overlay_ipc_test.py`, `src/index.tsx`) against the
recovery-safe requirements. Answers are based on the actual code, not assumptions.

## Answers to the required questions

1. **Does `_main()` auto-call `OverlayManager.start()`?**
   Yes. `Plugin._main()` calls `engine.start_overlay(debug=...)`, which creates the
   `OverlayManager` and immediately calls `start()` → spawns the renderer at plugin
   load, with no user action. This is the primary dangerous behavior.

2. **Do `show/update` implicitly `ensure_started()`?**
   Yes. `OverlayManager.update()` and `hide()` call `self._ensure_alive()`, which
   spawns or restarts the renderer. So an OCR update can implicitly start a renderer
   during Steam/Decky startup.

3. **What does the renderer singleton currently depend on?**
   Only the socket path. `serve()` tries `bind(sock_path)`; on `EADDRINUSE` it probes
   the socket and exits if a live listener answers, otherwise unlinks and rebinds.

4. **Can different sockets start different renderers?**
   Yes. `--socket a.sock` and `--socket b.sock` bind different paths and both succeed,
   so two renderers can each set `GAMESCOPE_EXTERNAL_OVERLAY=1`. This matches the
   observed accident (multiple overlay windows).

5. **Does a second renderer unlink the first renderer's socket?**
   Possibly. On `EADDRINUSE`, if the probe connect fails for any reason (race,
   permission, transient), the second instance unlinks the socket path and rebinds,
   stealing the name from a live renderer. The singleton is not robust.

6. **Where is renderer auto-restart triggered?**
   `OverlayManager._ensure_alive()`, called from `update()` / `hide()`. It restarts
   once per manager instance (`self._restarts >= 1`).

7. **Can restart counts stack across multiple backend instances?**
   Yes. Each `main.py` process has its own engine + `OverlayManager` with its own
   `_restarts` counter. N backends → up to N restarts → multiplicative spawning.

8. **Does `_unload()` always await renderer stop?**
   `_unload()` calls `engine.stop_overlay()` (synchronous). It does wait for the
   renderer (up to ~5 s), but it is not `await`ed as an async operation and only
   stops the renderer owned by that specific backend instance.

9. **Does the renderer daemonize / setsid?**
   Yes. `subprocess.Popen(..., start_new_session=True)` puts the renderer in a new
   session (`setsid`), detaching it from the backend's process group. This defeats
   parent-death safety and lets it outlive the backend.

10. **What is the renderer PPID?**
    With `start_new_session=True`, PPID is still the backend, but when the root
    backend uses `sudo -u deck`, the renderer's direct parent is the `sudo` process.
    This is not currently logged, so it is unverified at runtime.

11. **How is root→deck implemented?**
    `OverlayManager._spawn()` builds `["/usr/bin/sudo", "-u", "deck", "env",
    "DISPLAY=...", "HOME=/home/deck", "XDG_RUNTIME_DIR=/run/user/1000",
    [XAUTHORITY=...], <python>, renderer.py, ...]`.

12. **Who creates the runtime dir and who owns it?**
    `OverlayManager._spawn()` (running as root) creates `protocol.runtime_dir()` and
    `chmod 0o777`. Owner is `root:root`, mode `0777` — not `deck:deck 0700` as required.

13. **Do multiple `main.py` duplicate capture/OCR?**
    Yes. Each backend instance has its own `ClarifyDeckEngine` with its own
    `_capture_task`. `start_plugin` starts capture per instance. There is no global
    leader, so N backends can run N capture/OCR loops.

14. **Any `pkill` / process-name-based cleanup?**
    No `pkill` in product code. `stop_overlay()` targets the exact `Popen` handle. Good.
    (The Phase 1B notes mention `pkill -f overlay_poc.py` only as a manual test step.)

15. **Does the IPC test tool default to sending `shutdown`?**
    Yes. `scripts/overlay_ipc_test.py` always ends with `{"type":"shutdown"}`, which
    would kill a live renderer owned by the backend.

16. **Is the External Overlay setting persisted / auto-restored?**
    Not persisted, but it is auto-started on every plugin load, which is worse than a
    persisted ON setting for boot safety.

17. **Order of X11 window creation vs singleton check?**
    Wrong order. `main()` calls `renderer.open()` (XOpenDisplay, colormap, XCreateWindow,
    set `GAMESCOPE_EXTERNAL_OVERLAY`, `XMapWindow`) **before** `serve()` performs the
    socket bind / singleton check. A second instance therefore creates an overlay
    window before it discovers the conflict.

## Root cause of the accident

- The renderer singleton was keyed only on the socket path, so multiple sockets →
  multiple overlay windows.
- The renderer created its X11 window before checking for an existing instance.
- `_main()` auto-started the renderer, and `show/update` implicitly spawned it, so
  the OCR pipeline could start overlays during Steam/Decky startup.
- Auto-restart was per backend instance, so multiple backends multiplied restarts.
- `start_new_session=True` detached the renderer, defeating parent-death safety.
- There was no global leader, so multiple `main.py` also meant multiple capture/OCR
  workers.

## Required changes (implemented in Steps 1–12)

1. Background Leader Lease (`fcntl.flock`) so only one backend runs background work.
2. Global renderer lock (`renderer.lock`) independent of socket path.
3. Renderer acquires the lock **before** any X11 call / window creation.
4. External Overlay default OFF; no auto-start in `_main()`.
5. Explicit `enable`/`disable` RPC + QAM toggle.
6. `show/update/hide` are no-ops unless enabled (no implicit start).
7. Auto-restart disabled; renderer death → FAILED, user must re-enable.
8. Exact child lifecycle cleanup (no `pkill`, exact PID, no `setsid`).
9. Parent-death safety (`--parent-pid` watchdog + best-effort `PR_SET_PDEATHSIG`).
10. Runtime dir owned by `deck:deck`, mode `0700`; spawn as `deck` via `Popen(user=...)`.
11. Safety tests + `RECOVERY.md`.
12. `py_compile` / smoke / `pnpm build`.
