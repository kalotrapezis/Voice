<p align="center">
  <img src="Assets/VoiceIcon.png" width="140" alt="VOICE">
</p>

<h1 align="center">VOICE — Greek Voice Keyboard &amp; Read-Aloud</h1>

<p align="center">
  <em>V.O.I.C.E — Voice On-device Intelligent Control Environment</em><br>
  Fully <strong>local, offline</strong> Greek speech-to-text &amp; text-to-speech for Linux — no GPU, no cloud.
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-v0.0.3-5b8cff">
  <img alt="platform" src="https://img.shields.io/badge/platform-Linux%20%C2%B7%20KDE%20%C2%B7%20Cinnamon-1e1f2b">
  <img alt="deps" src="https://img.shields.io/badge/UI-pure%20Tkinter%20(no%20pip)-3a3d52">
  <img alt="license" src="https://img.shields.io/badge/license-see%20LICENSE-lightgrey">
</p>

---

Built as the base for an accessibility tool: **talk to type**, and have **any text read back** to you. All inference runs on-device — your voice never leaves your computer.

- **🎤 Dictation (STT)** — speak Greek, it types/pastes into any app. Engine: [whisper.cpp](https://github.com/ggerganov/whisper.cpp) (CPU).
- **📖 Read-aloud (TTS)** — load a PDF, paste, or type, then hear it in Greek. Engine: [Piper](https://github.com/rhasspy/piper).
- **📝 Meeting transcription** — capture **system audio** (Teams/Zoom, video playback) or your **microphone** (in-person), live-transcribe every phrase with a timestamp, then export a Markdown file (`meeting-YYYY-MM-DD-HHMMSS.md`). Runs in the background — minimize it and it keeps writing.
- **🎯 Voice remote** — pick a target app (Claude, KWrite, browser…); dictated text lands there via KWin window activation + clipboard paste.
- **📋 Clipboard manager** — a background watcher keeps a history of everything you copy (text **and** images, shown as a thumbnail + path). Browse it in the **Πρόχειρο** tab — click to re-copy, **pin** items so they survive reboots, copy text as lower / UPPER / Title case, or **Clear all**. Pinned + recent items are also in the tray menu. A shortcut pops an always-on-top **mini view** (5 items, scrollable) for a Win+V-style quick paste.
- **🔁 Hands-free** continuous mode with automatic pause detection (VAD).
- **🔽 Mini mode** — a tiny always-on-top floating bar with 🎤 / 🔊 / 📋, where the clipboard list expands inline.
- **📍 System-tray icon** — close to the tray; right-click to talk, read, or quit.
- **⌨ Global shortcuts** + in-app keyboard navigation.

## Requirements

| | |
|---|---|
| **OS** | **Fedora KDE** (Fedora 44, Plasma) and **Debian/Ubuntu/Linux Mint/Kubuntu** (tested on Mint 22.3 Cinnamon) |
| **Session** | **KDE Plasma** (Wayland *or* X11) → KWin `WindowsRunner` + `ydotool`. **Cinnamon / other X11** → `wmctrl` + `xdotool` + `xclip`. Picked automatically at runtime. |
| **CPU** | 64-bit x86_64, 4 cores minimum — **8 cores recommended** (whisper.cpp runs on CPU) |
| **RAM** | 8 GB |
| **Disk** | ~0.6 GB for the speech models (downloaded once on first launch into `~/.local/share/voice/`) |
| **Internet** | Only for the one-time model download — everything after runs fully offline |

> The desktop-specific bits (window activation for the voice remote, the paste
> keystroke, the clipboard, and global shortcuts) are abstracted in
> [`platform_io.py`](platform_io.py) and selected by session type + desktop:
> **KWin/ydotool/wl-clipboard** on Plasma, **wmctrl/xdotool/xclip** on Cinnamon
> and other X11 desktops. GNOME-on-Wayland can't be driven for window activation
> (no portable API) — dictation into the focused window still works there.

## Screenshots

| 🎤 Dictation | 📖 Read-aloud | ⚙ Settings |
|:---:|:---:|:---:|
| ![Dictation panel](Screenshots/dictation.png) | ![Read-aloud panel](Screenshots/read-aloud.png) | ![Settings panel](Screenshots/settings.png) |
| Pick where text goes, talk, live mic level, continuous mode | Read selection / clipboard / PDF, pick voice &amp; speed | Mic, global shortcuts, in-app keys |

| 📝 Meeting | 🔽 Mini mode |
|:---:|:---:|
| ![Meeting panel](Screenshots/Meeting.png) | ![Mini mode](Screenshots/Mini-mode.png) |
| Capture system audio or mic, live timestamped transcript, export to `.md` | Tiny always-on-top 🎤 / 🔊 bar over any window |

| 📋 Clipboard manager | 📋 Mini clipboard (Ctrl+Alt+Z) |
|:---:|:---:|
| ![Clipboard manager](Screenshots/ClipboardManager.png) | ![Mini clipboard](Screenshots/mini-clipboard-Ctrl_Alt_Z.png) |
| History with image thumbnails + path, pin (survives reboots), case-copy, clear all | Always-on-top quick view — 5 items, scrollable, click to re-copy |

## Install (Fedora / RPM)

The easiest way on Fedora KDE. `dnf` pulls every runtime dependency automatically.

```bash
# grab the .rpm from the latest release, then:
sudo dnf install ./voice-0.0.3-1.fc44.x86_64.rpm
```

Launch **“VOICE — Φωνή”** from the app menu, or run `voice`. The speech models
(~0.6 GB) are **not** in the package — they download once on first launch into
`~/.local/share/voice/`.

**Build the RPM yourself** (needs `whisper.cpp` built + `piper_tts/` present — see [SETUP.md](SETUP.md)):

```bash
sudo dnf install -y rpm-build patchelf
./build_rpm.sh        # → build/rpm/RPMS/x86_64/voice-*.rpm
```

## Install (Debian / Ubuntu / Linux Mint / Kubuntu — DEB)

`apt` pulls every runtime dependency automatically.

```bash
# grab the .deb from the latest release, then:
sudo apt install ./voice_0.0.3_amd64.deb
```

**Build the DEB yourself** (needs `whisper.cpp` built + `piper_tts/` present):

```bash
sudo apt install -y dpkg-dev patchelf
./build_deb.sh        # → build/deb/voice_<ver>_amd64.deb
```

> ⚠ **Build whisper.cpp on the oldest glibc you target.** The bundled
> `whisper-cli` links against the build machine's glibc. A binary built on
> Fedora 44 (glibc 2.43) will **not** run on Ubuntu/Mint 24.04 (glibc 2.39).
> Build whisper.cpp on Ubuntu/Mint and it runs on both. The `.deb` here bundles
> the locally built `whisper.cpp/build`.

## Run from source

See [SETUP.md](SETUP.md) for the full walkthrough (permissions, ydotool daemon, Piper voice). In short:

```bash
# 1. engines
sudo dnf install -y ydotool gcc-c++ cmake make poppler-utils

# 2. build whisper.cpp (CPU-only, small binary)
git clone --depth 1 https://github.com/ggerganov/whisper.cpp.git
cmake -S whisper.cpp -B whisper.cpp/build -DGGML_NATIVE=ON && cmake --build whisper.cpp/build -j8

# 3. models (Greek)
mkdir -p models voices piper_tts
curl -L -o models/ggml-small.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin
# Piper binary + el_GR-rapunzelina voice — see SETUP.md

# 4. run
python3 voice_keyboard.py
```

> **Note:** the large `models/`, `voices/`, and `whisper.cpp/` directories are **not** versioned — they are fetched by the setup steps above.

## Usage

- **Dictation tab** — choose **Στόχος** (target app, or "active window"), press the mic (or the global shortcut) and speak. Text is pasted into the target.
- **Continuous mode** — tick *Συνεχής λειτουργία*; it transcribes and delivers on every pause.
- **Read-aloud tab** — open a PDF / paste / type, select text, press *Διάβασε επιλογή* (or *Διάβασε όλα*).
- **Meeting tab (Σύσκεψη)** — pick **🔊 Ήχος συστήματος** (records the speakers — remote Teams/Zoom participants, video) or **🎤 Μικρόφωνο** (your voice + the room, for in-person), press *Έναρξη*, and each phrase is appended with a timestamp. Press *Ολοκλήρωση & αποθήκευση* to export a `.md` file. Set the export folder in **Ρυθμίσεις → Φάκελος εξαγωγής συσκέψεων** (defaults to your home folder).
- **Mini mode** — collapse to a floating 🎤 / 🔊 bar; great for overlaying any window.
- **Tray** — closing the window keeps VOICE running in the system tray; use **Έξοδος** to quit fully.

Global shortcuts are set in **Ρυθμίσεις** (press *Όρισε* and hit the combo) — dictate, read, open the clipboard tab, and the always-on-top **mini clipboard** (default `Ctrl+Alt+Z`). On KDE they register via `.desktop` + `kbuildsycoca`; on Cinnamon/GNOME via `gsettings` custom keybindings.

## Components

| File | Role |
|------|------|
| `voice_keyboard.py` | Main app — tabbed window (Dictation / Reading / Meeting / Clipboard / Settings) + tray |
| `read_aloud.py` | Reading view (also runnable standalone) |
| `clipboard.py` | Clipboard history: background watcher, store + JSON persistence, thumbnails |
| `platform_io.py` | Desktop/session abstraction — KWin vs wmctrl, ydotool vs xdotool, wl-clipboard vs xclip |
| `dictate.sh` | Push-to-talk dictation, for a global hotkey |
| `speak.sh` | Read selection/clipboard aloud, for a global hotkey |
| `SETUP.md` | Full install &amp; permission setup |

## Stack

- **STT**: whisper.cpp + `ggml-small` (Greek)
- **TTS**: Piper + `el_GR-rapunzelina-medium`
- **Typing/paste**: ydotool (Wayland uinput)
- **Window control**: KWin `WindowsRunner` over D-Bus
- **Tray**: StatusNotifierItem via AppIndicator
- **UI**: pure standard-library Tkinter (no pip dependencies)

All inference is local and offline.

## License

See [LICENSE](LICENSE).
