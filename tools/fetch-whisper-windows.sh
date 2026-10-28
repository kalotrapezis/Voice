#!/bin/sh
# Puts the Windows pieces the Electron package needs into electron/whisper-win and electron/vcruntime:
# whisper.cpp's own Windows release (CPU, b5454) and the Microsoft VC++ runtime DLLs it and onnxruntime need
# (app-local, from the official redistributable). Needs curl, unzip, 7z, python3.
set -e
cd "$(dirname "$0")/.."
T=$(mktemp -d)
curl -fsSL -o "$T/w.zip" https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip
echo "6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb  $T/w.zip" | sha256sum -c --quiet
rm -rf electron/whisper-win electron/vcruntime && mkdir -p electron/whisper-win electron/vcruntime
unzip -q -j "$T/w.zip" 'Release/whisper-cli.exe' 'Release/whisper-server.exe' 'Release/whisper.dll' 'Release/ggml*.dll' -d electron/whisper-win
# the VC++ runtime: the redistributable is a WiX bundle; its payload cabinet holds the DLLs
curl -fsSL -o "$T/vc.exe" https://aka.ms/vs/17/release/vc_redist.x64.exe
python3 - "$T" <<'PY'
import struct, sys
t = sys.argv[1]; d = open(t + '/vc.exe', 'rb').read(); i = 0; n = 0
while (i := d.find(b'MSCF\0\0\0\0', i)) >= 0:
    size = struct.unpack('<I', d[i+8:i+12])[0]
    if 1000 < size < len(d): open(f'{t}/cab{n}.cab', 'wb').write(d[i:i+size]); n += 1
    i += 4
PY
for c in "$T"/cab*.cab; do 7z x -y -o"$T/c" "$c" >/dev/null; done
for a in "$T"/c/a*; do 7z x -y -o"$T/d" "$a" >/dev/null 2>&1 || true; done
for f in msvcp140 msvcp140_1 msvcp140_2 vcruntime140 vcruntime140_1; do
  cp "$T/d/$f.dll_amd64" "electron/vcruntime/$f.dll"; cp "$T/d/$f.dll_amd64" "electron/whisper-win/$f.dll"
done
rm -rf "$T"
ls electron/whisper-win electron/vcruntime
