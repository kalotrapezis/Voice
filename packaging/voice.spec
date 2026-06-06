# Prebuilt binaries are bundled (whisper.cpp + Piper) — no compilation here.
%global debug_package %{nil}
%global __brp_mangle_shebangs %{nil}
# don't leak the bundled private .so as system Provides, and don't require them
# from the system (they ship inside this package, under /usr/lib/voice)
%global __provides_exclude_from ^/usr/lib/voice/.*\\.so.*$
%global __requires_exclude ^(libwhisper|libggml.*|libpiper_phonemize|libonnxruntime|libespeak-ng)\\.so.*$

Name:           voice
Version:        0.0.1
Release:        1%{?dist}
Summary:        Greek voice keyboard & read-aloud (offline STT/TTS) for KDE Wayland

License:        GPL-3.0-or-later
URL:            https://github.com/kalotrapezis/Voice
BuildArch:      x86_64

BuildRequires:  patchelf

# runtime services live on the host; dnf resolves them
Requires:       python3
Requires:       python3-tkinter
Requires:       python3-gobject
Requires:       libappindicator-gtk3
Requires:       ydotool
Requires:       wl-clipboard
Requires:       pipewire-utils
Requires:       pulseaudio-utils
Requires:       poppler-utils
Requires:       glib2

%description
VOICE (Voice On-device Intelligent Control Environment) is a fully local,
offline Greek voice toolkit: dictation (speech-to-text via whisper.cpp) that
types into any app, and read-aloud (text-to-speech via Piper). Includes a
voice-remote target picker, continuous hands-free mode, a mini floating bar,
a system-tray icon and global shortcuts.

The large speech models (~0.6 GB) are not shipped in the package; they are
downloaded once, on first launch, into ~/.local/share/voice/.

%install
rm -rf %{buildroot}
APP=%{buildroot}%{_prefix}/lib/voice
install -d $APP $APP/whisper.cpp/build/bin $APP/whisper.cpp/build/src $APP/whisper.cpp/build/ggml/src

# python app + assets (only the runtime PNG icon — skip .psd/.ico sources)
install -m644 %{projdir}/voice_keyboard.py $APP/
install -m644 %{projdir}/read_aloud.py    $APP/
install -Dm644 %{projdir}/Assets/VoiceIcon.png $APP/Assets/VoiceIcon.png
cp -a %{projdir}/piper_tts $APP/

# whisper-cli + its private libs
install -m755 %{projdir}/whisper.cpp/build/bin/whisper-cli $APP/whisper.cpp/build/bin/
cp -aP %{projdir}/whisper.cpp/build/src/libwhisper.so*       $APP/whisper.cpp/build/src/
cp -aP %{projdir}/whisper.cpp/build/ggml/src/libggml*.so*    $APP/whisper.cpp/build/ggml/src/

# the bundled whisper carries the build machine's absolute RUNPATH — rewrite it
# to be relocatable ($ORIGIN-relative) so it loads its libs after install
patchelf --set-rpath '$ORIGIN/../src:$ORIGIN/../ggml/src' \
  $APP/whisper.cpp/build/bin/whisper-cli
for f in $APP/whisper.cpp/build/src/libwhisper.so.*.*; do
  [ -f "$f" ] && patchelf --set-rpath '$ORIGIN/../ggml/src' "$f"
done
for f in $APP/whisper.cpp/build/ggml/src/libggml*.so.*.*; do
  [ -f "$f" ] && patchelf --set-rpath '$ORIGIN' "$f"
done

# launcher
install -Dm755 %{projdir}/packaging/voice.launcher %{buildroot}%{_bindir}/voice

# desktop entry + icon
install -Dm644 %{projdir}/packaging/voice.desktop \
  %{buildroot}%{_datadir}/applications/voice.desktop
install -Dm644 %{projdir}/Assets/VoiceIcon.png \
  %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/voice.png

# license
install -Dm644 %{projdir}/LICENSE \
  %{buildroot}%{_datadir}/licenses/%{name}/LICENSE

%files
%license %{_datadir}/licenses/%{name}/LICENSE
%{_prefix}/lib/voice
%{_bindir}/voice
%{_datadir}/applications/voice.desktop
%{_datadir}/icons/hicolor/256x256/apps/voice.png

%post
/usr/bin/touch --no-create %{_datadir}/icons/hicolor &>/dev/null || :
/usr/bin/gtk-update-icon-cache %{_datadir}/icons/hicolor &>/dev/null || :
/usr/bin/update-desktop-database &>/dev/null || :

%postun
/usr/bin/gtk-update-icon-cache %{_datadir}/icons/hicolor &>/dev/null || :
/usr/bin/update-desktop-database &>/dev/null || :

%changelog
* Sat Jun 06 2026 Theologos Kalotrapezis <kalotrapezis@gmail.com> - 0.0.1-1
- Initial RPM packaging of VOICE
