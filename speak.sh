#!/usr/bin/env bash
# Read-aloud (TTS) — speak selected/clipboard text in Greek with Piper.
# Toggle: run while speaking → stops. Bind to a hotkey in KDE.
#
# Text source priority: command args  →  stdin  →  highlighted (PRIMARY) selection  →  clipboard.
# So the typical use is: highlight Claude's reply, press the hotkey, hear it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPER="$HERE/piper_tts/piper/piper"
# prefer the higher-quality medium voice if present, else low
if [ -n "${PIPER_MODEL:-}" ]; then MODEL="$PIPER_MODEL"
elif [ -f "$HERE/voices/el_GR-rapunzelina-medium.onnx" ]; then MODEL="$HERE/voices/el_GR-rapunzelina-medium.onnx"
else MODEL="$HERE/voices/el_GR-rapunzelina-low.onnx"; fi
SPEED="${TTS_SPEED:-1.0}"          # length_scale: >1 = slower, <1 = faster
WAV="/tmp/voicekbd_tts_raw.wav"
PWAV="/tmp/voicekbd_tts.wav"       # padded, what we actually play
PIDF="/tmp/voicekbd_tts.pid"

notify() { command -v notify-send >/dev/null && notify-send -t 2000 "🔊 Ανάγνωση" "$1" || echo "$1"; }

# --- toggle: if already speaking, stop and exit ---
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  kill "$(cat "$PIDF")" 2>/dev/null || true
  rm -f "$PIDF"
  exit 0
fi

# --- gather text ---
if [ "$#" -gt 0 ]; then
  TEXT="$*"
elif [ ! -t 0 ]; then
  TEXT="$(cat)"
else
  TEXT="$(wl-paste --primary 2>/dev/null || true)"
  [ -z "${TEXT// }" ] && TEXT="$(wl-paste 2>/dev/null || true)"
fi

if [ -z "${TEXT// }" ]; then
  notify "Δεν βρέθηκε κείμενο — επίλεξε ή αντίγραψε κάτι πρώτα"
  exit 0
fi

# --- synthesize (sentence_silence gives breathing room between sentences) ---
printf '%s' "$TEXT" | "$PIPER" --model "$MODEL" --length_scale "$SPEED" \
  --sentence_silence 0.35 --output_file "$WAV" 2>/dev/null

# --- pad ~0.3s silence at both ends so first/last phonemes aren't clipped ---
python3 - "$WAV" "$PWAV" <<'PY'
import sys, wave
src, dst = sys.argv[1], sys.argv[2]
with wave.open(src, "rb") as w:
    p = w.getparams(); frames = w.readframes(w.getnframes())
pad = b"\x00" * int(p.framerate * 0.30) * p.sampwidth * p.nchannels
with wave.open(dst, "wb") as o:
    o.setparams(p); o.writeframes(pad + frames + pad)
PY

# --- play (store player PID so the hotkey can stop it) ---
pw-play "$PWAV" &
echo $! > "$PIDF"
wait "$(cat "$PIDF")" 2>/dev/null || true
rm -f "$PIDF"
