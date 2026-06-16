# Voice Keyboard — Setup (Linux · KDE Plasma or Cinnamon/X11)

A push-to-talk dictation tool: press a hotkey, speak Greek, press again → text is typed
into whatever app is focused. 100% offline. Engines: **whisper.cpp** (STT) + **Piper** (TTS).
Typing/paste is **ydotool** on Wayland and **xdotool** on X11 — picked automatically.

---

## 1. Install the engines (needs root — run in your terminal)

**Fedora KDE:**
```bash
sudo dnf install -y ydotool wl-clipboard gcc-c++ cmake make poppler-utils
# X11 desktops (Cinnamon, GNOME-on-X11) also want:
sudo dnf install -y xdotool xclip wmctrl
```

**Debian / Ubuntu / Linux Mint / Kubuntu:**
```bash
sudo apt install -y g++ cmake make poppler-utils \
  xdotool xclip wmctrl ydotool wl-clipboard \
  python3-tk python3-gi gir1.2-ayatanaappindicator3-0.1
```

> ⚠️ Do **not** install the distro `whisper-cpp` package — Fedora builds it against
> ROCm + OpenVINO (≈1.8 GB of GPU libs). We build a ~1 MB CPU-only binary from source
> (see `whisper.cpp/build/bin/whisper-cli`).
>
> ⚠️ **Build whisper.cpp on the oldest glibc you target.** A binary built on Fedora 44
> (glibc 2.43) will not run on Ubuntu/Mint 24.04 (glibc 2.39: `GLIBC_2.43 not found`).
> Build it on Ubuntu/Mint and it runs on both:
> ```bash
> cmake -S whisper.cpp -B whisper.cpp/build -DGGML_NATIVE=OFF -DCMAKE_BUILD_TYPE=Release
> cmake --build whisper.cpp/build -j"$(nproc)"
> ```

The Greek-capable model (`models/ggml-small.bin`, 466 MB) is already downloaded.

---

## 2. Typing daemon — Wayland only (X11 needs nothing)

On **X11 (Cinnamon/Linux Mint)** typing/paste uses `xdotool`, which needs no daemon — skip
this section. On **KDE/Wayland** `ydotool` types via the kernel's `/dev/uinput`:

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
