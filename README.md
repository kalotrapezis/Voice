# Φωνή — Greek Voice Keyboard & Read-Aloud (Linux / Wayland / KDE)

A fully **local, offline** Greek voice toolkit for Linux — no GPU, no cloud.
Built as the base for an accessibility tool: talk to type, and have any text read back.

- **🎤 Dictation (STT)** — speak Greek, it types/pastes into any app. Engine: [whisper.cpp](https://github.com/ggerganov/whisper.cpp) (CPU).
- **📖 Read-aloud (TTS)** — select text / PDF / clipboard and hear it in Greek. Engine: [Piper](https://github.com/rhasspy/piper).
- **Voice remote** — pick a target app (Claude, KWrite, browser…); dictated text lands there via KWin window activation + clipboard paste.
- **Hands-free** continuous mode with automatic pause detection (VAD).
- **Mini mode** — tiny floating bar with 🎤 / 🔊.
- **Global shortcuts** + in-app keyboard navigation.

Runs comfortably on an 8-core CPU / 8 GB RAM machine.

## Components
| File | Role |
|------|------|
| `voice_keyboard.py` | Main app — tabbed window (Dictation / Reading / Settings) |
| `read_aloud.py` | Reading view (also runnable standalone) |
| `dictate.sh` | Push-to-talk dictation, for a global hotkey |
| `speak.sh` | Read selection/clipboard aloud, for a global hotkey |
| `SETUP.md` | Full install & permission setup |

## Quick setup
See [SETUP.md](SETUP.md). In short:

```bash
# 1. engines
sudo dnf install -y ydotool gcc-c++ cmake make poppler-utils

# 2. build whisper.cpp (CPU-only, ~1 MB binary)
git clone --depth 1 https://github.com/ggerganov/whisper.cpp.git
cmake -S whisper.cpp -B whisper.cpp/build -DGGML_NATIVE=ON && cmake --build whisper.cpp/build -j8

# 3. models (Greek)
mkdir -p models voices piper_tts
curl -L -o models/ggml-small.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin
# Piper binary + el_GR-rapunzelina voice — see SETUP.md

# 4. run
python3 voice_keyboard.py
```

## Stack
- **STT**: whisper.cpp + `ggml-small` (Greek)
- **TTS**: Piper + `el_GR-rapunzelina-medium`
- **Typing/paste**: ydotool (Wayland uinput)
- **Window control**: KWin `WindowsRunner` over D-Bus
- **UI**: pure standard-library Tkinter (no pip dependencies)

All inference is local and offline.
