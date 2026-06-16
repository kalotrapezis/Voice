#!/usr/bin/env bash
# Build the VOICE .deb from this working tree (Debian/Ubuntu/Linux Mint/Kubuntu).
#
# Needs: dpkg-deb (dpkg-dev). patchelf is optional (the launcher also sets
# LD_LIBRARY_PATH). Like the RPM, the bundled whisper.cpp + Piper binaries are
# packaged; the large speech models are NOT (fetched on first launch).
set -euo pipefail

PROJ="$(cd "$(dirname "$0")" && pwd)"
VERSION="${VERSION:-0.0.3}"
ARCH="${ARCH:-amd64}"
PKG="voice_${VERSION}_${ARCH}"
OUT="$PROJ/build/deb"
STAGE="$OUT/$PKG"

# sanity: bundled binaries must exist (built once — see SETUP.md)
[ -x "$PROJ/whisper.cpp/build/bin/whisper-cli" ] || {
  echo "✗ whisper.cpp not built ($PROJ/whisper.cpp/build/bin/whisper-cli missing)"
  echo "  build it first — see SETUP.md"; exit 1; }
[ -x "$PROJ/piper_tts/piper/piper" ] || {
  echo "✗ piper not found ($PROJ/piper_tts/piper/piper missing) — see SETUP.md"; exit 1; }

rm -rf "$STAGE"
APP="$STAGE/usr/lib/voice"
install -d "$APP/whisper.cpp/build/bin" "$APP/whisper.cpp/build/src" \
           "$APP/whisper.cpp/build/ggml/src" \
           "$STAGE/usr/bin" "$STAGE/usr/share/applications" \
           "$STAGE/usr/share/icons/hicolor/256x256/apps" \
           "$STAGE/usr/share/doc/voice" "$STAGE/DEBIAN"

# python app + runtime icon (skip .psd/.ico sources)
install -m644 "$PROJ/voice_keyboard.py" "$APP/"
install -m644 "$PROJ/read_aloud.py"     "$APP/"
install -m644 "$PROJ/platform_io.py"    "$APP/"
install -m644 "$PROJ/clipboard.py"      "$APP/"
install -Dm644 "$PROJ/Assets/VoiceIcon.png" "$APP/Assets/VoiceIcon.png"
cp -a "$PROJ/piper_tts" "$APP/"

# whisper-cli + its private libs
install -m755 "$PROJ/whisper.cpp/build/bin/whisper-cli" "$APP/whisper.cpp/build/bin/"
cp -aP "$PROJ"/whisper.cpp/build/src/libwhisper.so*    "$APP/whisper.cpp/build/src/"
cp -aP "$PROJ"/whisper.cpp/build/ggml/src/libggml*.so* "$APP/whisper.cpp/build/ggml/src/"

# make the bundled whisper relocatable if patchelf is available (the launcher
# sets LD_LIBRARY_PATH too, so this is belt-and-suspenders)
if command -v patchelf >/dev/null 2>&1; then
  patchelf --set-rpath '$ORIGIN/../src:$ORIGIN/../ggml/src' \
    "$APP/whisper.cpp/build/bin/whisper-cli" || true
  for f in "$APP"/whisper.cpp/build/src/libwhisper.so.*.*; do
    [ -f "$f" ] && patchelf --set-rpath '$ORIGIN/../ggml/src' "$f" || true
  done
  for f in "$APP"/whisper.cpp/build/ggml/src/libggml*.so.*.*; do
    [ -f "$f" ] && patchelf --set-rpath '$ORIGIN' "$f" || true
  done
fi

# launcher, desktop entry, icon, license
install -Dm755 "$PROJ/packaging/voice.launcher" "$STAGE/usr/bin/voice"
install -Dm644 "$PROJ/packaging/voice.desktop"  "$STAGE/usr/share/applications/voice.desktop"
install -Dm644 "$PROJ/Assets/VoiceIcon.png" \
  "$STAGE/usr/share/icons/hicolor/256x256/apps/voice.png"
install -Dm644 "$PROJ/LICENSE" "$STAGE/usr/share/doc/voice/copyright"

INSTALLED_KB="$(du -sk "$STAGE/usr" | cut -f1)"

# ---- control: deps cover both sessions ----
# X11 (Cinnamon/GNOME-on-X11): xdotool, xclip, wmctrl
# Wayland (KDE Plasma): ydotool, wl-clipboard  → Recommends (apt installs by default)
cat > "$STAGE/DEBIAN/control" <<EOF
Package: voice
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Maintainer: Theologos Kalotrapezis <kalotrapezis@gmail.com>
Installed-Size: $INSTALLED_KB
Depends: python3, python3-tk, python3-gi, gir1.2-ayatanaappindicator3-0.1, pipewire-bin, pulseaudio-utils, poppler-utils, xdotool, xclip, wmctrl
Recommends: ydotool, wl-clipboard, libreoffice
Homepage: https://github.com/kalotrapezis/Voice
Description: Greek voice keyboard & read-aloud (offline STT/TTS)
 VOICE (Voice On-device Intelligent Control Environment) is a fully local,
 offline Greek voice toolkit: dictation (speech-to-text via whisper.cpp) that
 types into any app, and read-aloud (text-to-speech via Piper). Includes a
 voice-remote target picker, continuous hands-free mode, a mini floating bar,
 a system-tray icon, meeting transcription and global shortcuts.
 .
 Works on KDE Plasma (Wayland) and on X11 desktops such as Cinnamon (Linux
 Mint). The large speech models (~0.6 GB) are not shipped in the package; they
 download once, on first launch, into ~/.local/share/voice/.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ -x /usr/bin/gtk-update-icon-cache ]; then
  gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
fi
if [ -x /usr/bin/update-desktop-database ]; then
  update-desktop-database -q 2>/dev/null || true
fi
exit 0
EOF

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ -x /usr/bin/gtk-update-icon-cache ]; then
  gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
fi
if [ -x /usr/bin/update-desktop-database ]; then
  update-desktop-database -q 2>/dev/null || true
fi
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

echo "▶ building .deb …"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT/$PKG.deb" >/dev/null

echo
echo "✓ built: $OUT/$PKG.deb"
echo
echo "Install with:   sudo apt install \"$OUT/$PKG.deb\""
echo "Then launch from the app menu (\"VOICE — Φωνή\") or run: voice"
