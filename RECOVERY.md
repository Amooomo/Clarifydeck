# ClarifyDeck Recovery Guide

ClarifyDeck's Phase 1C.2 build is recovery-safe: the persistent overlay is OFF by
default, only one renderer can exist, and there is no automatic restart. The
scenarios below are emergency fallbacks if something still misbehaves.

## If Game Mode / Steam starts behaving abnormally

```bash
# 1. Stop Decky so no plugin backend is running.
sudo systemctl stop plugin_loader.service

# 2. Move the plugin out of the active plugin directory.
mkdir -p ~/homebrew/disabled-plugins
sudo mv ~/homebrew/plugins/Clarifydeck \
        ~/homebrew/disabled-plugins/Clarifydeck-broken

# 3. Bring Decky back.
sudo systemctl start plugin_loader.service
```

## Inspect for leftover processes

```bash
# ClarifyDeck backend processes
pgrep -af 'Clarifydeck|clarifydeck'

# Overlay renderer only
pgrep -af 'Clarifydeck/overlay/renderer.py'
```

Expected in a healthy recovery-safe build:

- `renderer.py` = 0 unless the user explicitly enabled the persistent overlay.
- At most one backend instance is `role=leader`; duplicates are `role=standby`.

If a renderer is left over (for example because the backend was killed hard),
it is safe to stop only that exact PID:

```bash
kill <renderer_pid>          # graceful
kill -TERM <renderer_pid>    # if needed
```

Do **not** run broad kills (`pkill python`, `pkill -f overlay`). ClarifyDeck
never does this itself, and neither should recovery.

## Clear the overlay runtime directory

Only if no renderer is running:

```bash
ls -la /run/user/1000/clarifydeck
rm -f /run/user/1000/clarifydeck/overlay.sock
rm -f /run/user/1000/clarifydeck/renderer.lock
```

The lock is a kernel flock, so it is released automatically when the owning
process exits; deleting it while a renderer is alive is unnecessary and can
confuse diagnosis.

## Re-enabling after recovery

Move the plugin back and restart Decky:

```bash
sudo mv ~/homebrew/disabled-plugins/Clarifydeck-broken \
        ~/homebrew/plugins/Clarifydeck
sudo systemctl restart plugin_loader.service
```

The persistent overlay always starts in the OFF state after a reload/reboot.
Enable it explicitly from the QAM only when you want to test it.

## What ClarifyDeck will never do

- mask/disable the Decky service
- edit `/etc/systemd/system/plugin_loader.service`
- kill Steam, Gamescope, or mangoapp
- kill processes by name
- delete the runtime directory recursively
- auto-start the overlay at plugin load or reboot
