#!/usr/bin/env bash
# Voice keyboard (dictation) — toggle recording, transcribe with whisper.cpp, type with ydotool.
# Run once: start recording. Run again: stop, transcribe (Greek), type into focused app.
#
# Bind this script to a hotkey in KDE: System Settings → Shortcuts → Custom Shortcuts.
set -euo pipefail

# ---------------- config ----------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="${WHISPER_MODEL:-$HERE/models/ggml-small.bin}"
LANG_CODE="${WHISPER_LANG:-el}"          # "el" = Greek. Use "auto" for Greek+English mix.
THREADS="${WHISPER_THREADS:-8}"          # you have 8 cores
WAV="/tmp/voicekbd.wav"
RECPID="/tmp/voicekbd.rec.pid"
# ----------------------------------------

notify() { command -v notify-send >/dev/null && notify-send -t 1500 "🎙 Dictation" "$1" || echo "$1"; }

# locate whisper.cpp CLI — prefer our local CPU-only build, then any system one
WHISPER_BIN=""
for c in "$HERE/whisper.cpp/build/bin/whisper-cli" "$HERE/bin/whisper-cli" whisper-cli whisper-cpp whisper main; do
  if [ -x "$c" ] 2>/dev/null || command -v "$c" >/dev/null 2>&1; then WHISPER_BIN="$c"; break; fi
done
[ -z "$WHISPER_BIN" ] && { notify "whisper.cpp not found — install whisper-cpp"; exit 1; }

# ---------------- toggle ----------------
if [ -f "$RECPID" ] && kill -0 "$(cat "$RECPID")" 2>/dev/null; then
  # ===== STOP & TRANSCRIBE =====
  kill "$(cat "$RECPID")" 2>/dev/null || true
  rm -f "$RECPID"
  sleep 0.2
  notify "Transcribing…"

  TEXT="$("$WHISPER_BIN" -m "$MODEL" -f "$WAV" -l "$LANG_CODE" -t "$THREADS" -nt -np 2>/dev/null \
          | tr '\n' ' ' | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//; s/[[:space:]]+/ /g')"

  if [ -z "$TEXT" ]; then notify "(nothing heard)"; exit 0; fi

  # type it where the cursor is — ydotool (Wayland/uinput) or xdotool (X11)
  if [ -n "${WAYLAND_DISPLAY:-}" ] && command -v ydotool >/dev/null 2>&1; then
    ydotool type --next-delay 0 -- "$TEXT"
  elif command -v xdotool >/dev/null 2>&1; then
    xdotool type --clearmodifiers -- "$TEXT"
  elif command -v ydotool >/dev/null 2>&1; then
    ydotool type --next-delay 0 -- "$TEXT"
  else
    # fallback: copy to clipboard so you can paste manually
    if command -v wl-copy >/dev/null 2>&1; then printf '%s' "$TEXT" | wl-copy
    elif command -v xclip >/dev/null 2>&1; then printf '%s' "$TEXT" | xclip -selection clipboard
    elif command -v xsel >/dev/null 2>&1; then printf '%s' "$TEXT" | xsel --clipboard --input
    fi
    notify "no typing tool — text copied to clipboard"
  fi
  notify "✓ $TEXT"
else
  # ===== START RECORDING =====
  rm -f "$WAV"
  # use the mic selected in the GUI (shared config), else system default
  DEVICE=""
  [ -f "$HOME/.config/voicekbd/config" ] && DEVICE="$(sed -n 's/^DEVICE=//p' "$HOME/.config/voicekbd/config")"
  # 16 kHz mono 16-bit — exactly what whisper wants
  if [ -n "$DEVICE" ]; then
    pw-record --target "$DEVICE" --rate 16000 --channels 1 --format s16 "$WAV" &
  else
    pw-record --rate 16000 --channels 1 --format s16 "$WAV" &
  fi
  echo $! > "$RECPID"
  notify "Recording… (press hotkey again to stop)"
fi
