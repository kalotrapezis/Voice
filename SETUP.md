# Voice Keyboard — Setup (Linux / Wayland / KDE)

A push-to-talk dictation tool: press a hotkey, speak Greek, press again → text is typed
into whatever app is focused. 100% offline. Engines: **whisper.cpp** (STT) + **ydotool** (typing).

---

## 1. Install the engines (needs root — run in your terminal)

```bash
sudo dnf install -y ydotool gcc-c++ cmake make
```

> ⚠️ Do **not** `dnf install whisper-cpp` — Fedora builds it against ROCm + OpenVINO and
> pulls **557 packages / 1.8 GB** of GPU libraries you don't have. We build a 1 MB
> CPU-only binary from source instead (done — see `whisper.cpp/build/bin/whisper-cli`).

The Greek-capable model (`models/ggml-small.bin`, 466 MB) is already downloaded.

---

## 2. ydotool daemon (already set up on this machine)

`ydotool` types via the kernel's `/dev/uinput`. On KDE/Wayland, logind already grants your
active session ACL access (`getfacl /dev/uinput` shows `user:teo:rw-`), so **no udev rule,
input group, or logout is needed**. A user service runs the daemon and autostarts on login:

```bash
systemctl --user status ydotoold      # should be "active"
# (re)install it if ever needed:
systemctl --user enable --now ydotoold
```

> If `getfacl /dev/uinput` does NOT list your user on some other machine, fall back to a
> udev rule + `input` group (older recipe), but you don't need that here.

---

## 3. Test it from the terminal

```bash
cd ~/Έγγραφα/Claude/Coding/TTS
./dictate.sh      # → "Recording…"  (speak a sentence in Greek)
./dictate.sh      # → transcribes and types it where your cursor is
```

If `ydotool` isn't working yet, the script falls back to copying the text to your clipboard
(paste with Ctrl+V) so you can confirm transcription quality independently.

---

## 4. Bind it to a hotkey in KDE

System Settings → **Shortcuts** → **Add Command** (or *Custom Shortcuts*):

- Command: `/home/teo/Έγγραφα/Claude/Coding/TTS/dictate.sh`
- Trigger: pick a key, e.g. **Meta+Z** or a spare function key.

Now: tap the key, talk, tap again — text appears. Point it at the terminal running
Claude Code and you're dictating commands.

---

## Tuning

Environment variables (set before running, or edit the top of `dictate.sh`):

| Var | Default | Notes |
|-----|---------|-------|
| `WHISPER_LANG` | `el` | Greek. Use `auto` if you mix Greek + English. |
| `WHISPER_MODEL` | `models/ggml-small.bin` | Swap to `ggml-base.bin` (faster, less accurate) or `ggml-medium.bin` (slower, more accurate). |
| `WHISPER_THREADS` | `8` | Matches your 8 cores. |

Bigger models = better Greek but slower on CPU. `small` is the recommended balance for your machine.
