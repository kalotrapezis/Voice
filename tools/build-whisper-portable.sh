#!/bin/sh
# Builds whisper.cpp for the Electron packages so it runs on any x86-64 Linux from 2022 on:
#  - the program and its CPU backends inside Ubuntu 22.04 (glibc 2.35), every CPU variant built and the
#    right one picked at run time;
#  - the Vulkan (GPU) backend inside Ubuntu 24.04, the oldest release that packages glslc. ggml loads it
#    at run time; where it cannot (an older system, no GPU driver) whisper simply stays on the CPU.
# Output: electron/whisper-bin/ (programs + libs, $ORIGIN rpath). Needs podman.
set -e
cd "$(dirname "$0")/.."
OUT=electron/whisper-bin
rm -rf "$OUT" && mkdir -p "$OUT"

build() { # image, extra cmake flags, targets, files to keep
  podman run --rm -v "$PWD/whisper.cpp:/src:ro,Z" -v "$PWD/$OUT:/out:Z" "docker.io/library/ubuntu:$1" sh -ec "
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq build-essential cmake git $2 >/dev/null
    cp -r /src /tmp/w && cd /tmp/w && rm -rf build*
    cmake -B b -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON \
          $3 -DWHISPER_BUILD_TESTS=OFF -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON \
          -DCMAKE_INSTALL_RPATH='\$ORIGIN' -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON >/dev/null
    cmake --build b -j\$(nproc) --target $4 >/dev/null
    $5"
}

# 1. programs + CPU backends, glibc 2.35
build 22.04 "" "" "whisper-cli whisper-server" \
  'cp b/bin/whisper-cli b/bin/whisper-server /out/ && find b -name "*.so*" -exec cp -P {} /out/ \;'
# 2. the Vulkan backend alone, glibc 2.39
build 24.04 "libvulkan-dev glslc spirv-headers" "-DGGML_VULKAN=ON" "ggml-vulkan" \
  'cp $(find b -name "libggml-vulkan.so") /out/'

ls -la "$OUT"
