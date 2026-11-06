#!/bin/sh
# Puts the Windows pieces the Electron package needs into electron/whisper-win and electron/vcruntime:
# whisper.cpp's own Windows release (CPU, b5454), its Vulkan backend for the GPU (NVIDIA, AMD, Intel) built here
# since the release has none, and the Microsoft VC++ runtime DLLs it and onnxruntime need (app-local, from the
# official redistributable). Needs curl, unzip, 7z, python3, podman.
set -e
cd "$(dirname "$0")/.."
T=$(mktemp -d)
curl -fsSL -o "$T/w.zip" https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip
echo "6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb  $T/w.zip" | sha256sum -c --quiet
rm -rf electron/whisper-win electron/vcruntime && mkdir -p electron/whisper-win electron/vcruntime
unzip -q -j "$T/w.zip" 'Release/whisper-cli.exe' 'Release/whisper-server.exe' 'Release/whisper.dll' 'Release/ggml*.dll' -d electron/whisper-win
# ggml-vulkan.dll from the release's own commit, cross-built with MinGW and linked against the release's
# ggml-base.dll exports (not a MinGW one), so it loads beside the MSVC files; static libstdc++, so it needs only
# vulkan-1.dll, which every GPU driver installs. ggml loads it at run time; without a driver whisper stays on the CPU.
curl -fsSL -o "$T/src.tgz" https://github.com/ggml-org/whisper.cpp/archive/d1be6fde11ac6e0407606b4e42fe72d34add8037.tar.gz
mkdir "$T/src" && tar -xzf "$T/src.tgz" -C "$T/src" --strip-components=1 && cp electron/whisper-win/ggml-base.dll "$T/"
podman run --rm -v "$T:/t:Z" docker.io/library/fedora:42 sh -ec '
  dnf -q install -y cmake ninja-build gcc-c++ glslc mingw64-gcc-c++ mingw64-vulkan-headers mingw64-vulkan-loader \
    mingw64-spirv-headers mingw64-winpthreads-static >/dev/null
  cp -r /t/src /tmp/w && cd /tmp/w
  mingw64-cmake -B b -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_VULKAN=ON -DGGML_CPU=OFF \
    -DGGML_OPENMP=OFF -DWHISPER_BUILD_EXAMPLES=OFF -DWHISPER_BUILD_TESTS=OFF \
    "-DCMAKE_CXX_STANDARD_LIBRARIES=-static-libgcc -static-libstdc++ -Wl,-Bstatic -lstdc++ -lwinpthread -Wl,-Bdynamic" >/dev/null
  { echo "LIBRARY ggml-base.dll"; echo EXPORTS; x86_64-w64-mingw32-objdump -p /t/ggml-base.dll |
    awk "/\[Ordinal\/Name Pointer\] Table/{f=1;next} f&&/^\s*\[/{print \$NF} f&&/^\$/{exit}"; } > base.def
  x86_64-w64-mingw32-dlltool -d base.def -l base.a
  cmake --build b --target ggml-vulkan >/dev/null 2>&1 || true # its own link fails (MinGW ggml-base); relinked below
  cmd=$(ninja -C b -t commands bin/ggml-vulkan.dll | tail -1 | sed "s#ggml/src/libggml-base.dll.a#/tmp/w/base.a#")
  cd b && sh -c "$cmd" && cp bin/ggml-vulkan.dll /t/'
cp "$T/ggml-vulkan.dll" electron/whisper-win/
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
