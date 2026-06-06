#!/usr/bin/env bash
# Build the VOICE RPM from this working tree.
#
# Needs: rpm-build  (sudo dnf install rpm-build)
# Also needs whisper.cpp built and piper_tts/ present (see SETUP.md) — these are
# bundled into the package; the large speech models are NOT (fetched on first run).
set -euo pipefail

PROJ="$(cd "$(dirname "$0")" && pwd)"
TOP="${RPM_TOPDIR:-$PROJ/build/rpm}"
SPEC="$PROJ/packaging/voice.spec"

# sanity: bundled binaries must exist
[ -x "$PROJ/whisper.cpp/build/bin/whisper-cli" ] || {
  echo "✗ whisper.cpp not built ($PROJ/whisper.cpp/build/bin/whisper-cli missing)"
  echo "  build it first — see SETUP.md"; exit 1; }
[ -x "$PROJ/piper_tts/piper/piper" ] || {
  echo "✗ piper not found ($PROJ/piper_tts/piper/piper missing) — see SETUP.md"; exit 1; }

mkdir -p "$TOP"/{BUILD,BUILDROOT,RPMS,SRPMS,SOURCES,SPECS}

echo "▶ building RPM into $TOP/RPMS …"
rpmbuild -bb "$SPEC" \
  --define "_topdir $TOP" \
  --define "projdir $PROJ"

RPM="$(find "$TOP/RPMS" -name 'voice-*.rpm' | head -1)"
echo
echo "✓ built: $RPM"
echo
echo "Install with:   sudo dnf install \"$RPM\""
echo "Then launch from the app menu (\"VOICE — Φωνή\") or run: voice"
