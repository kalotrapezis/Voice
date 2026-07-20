# Changelog

All notable changes to VOICE are documented here.

## v0.0.5-b — desktop disruption, duplicate text, mini bar

A bug-fix revision of the GTK 4 UI, found by using it. Two of these had been
"fixed" before from the wrong diagnosis; this round added a persistent log
first and let the evidence pick the cause.

### Added

- **Flight-recorder log** at `~/.cache/voicekbd/voice-gtk.log`. Launched from
  a hotkey or the app menu there is no terminal, so failures used to leave no
  trace at all. Every stage of the dictation chain now logs: each captured
  audio segment with a fingerprint (so a segment captured twice is visible at
  a glance), every whisper submit/result, every paste with its success flag,
  every hotkey command, and the process id — so a stray second instance can't
  hide. Rotates at 2 MB.
- **Stop icons** (`stop-black.svg` / `stop-white.svg`). The dictation mic, the
  meeting mic, the mini-bar mic and the read-aloud Play button now turn into a
  stop square while active and back when idle, instead of staying a mic and
  leaving you guessing whether it's recording.

### Fixed

- **VOICE disrupted the whole desktop while running** — context menus closed
  by themselves, text selections flickered about once a second, and menu items
  wouldn't take a hover. This was reported as "the app is left-clicking
  everywhere", and had previously been blamed on input injection. It was not
  input at all: the clipboard watcher polled by shelling out to `wl-paste`
  every second, and on KDE Wayland *each invocation briefly maps a window*.
  Every one of those windows broke the pointer grab that keeps a menu open and
  reset selection/hover state, once per second, for as long as VOICE ran. The
  GTK app now watches the clipboard through GTK's own in-process
  `Gdk.Clipboard::changed` signal: no polling, no subprocesses, no windows,
  and it does nothing at all until the clipboard actually changes.
  (The Tk app still uses the polling watcher.)
- **Dictated text arriving twice, in continuous mode.** The earlier fix
  addressed a real overlapping-takes bug but not this one. The log showed
  VOICE pasting each phrase exactly once, which pointed at the destination:
  when VOICE's own window had focus, the paste landed in its own Δοκιμή box —
  which already displays the recognised text — so the phrase appeared twice,
  adjacent and identical. Delivery is now skipped while VOICE itself is
  focused; the text is still copied and shown.
- **Paste keystrokes could interleave.** Two phrases finishing close together
  each sent Ctrl+V through `ydotool`; one could press V while the other still
  held Ctrl. Delivery is now serialised behind a lock.
- **whisper could starve the input path.** Transcription saturates every core,
  and at normal priority it could stall a keystroke between press and release.
  It now runs at `nice -n 10`, which keeps input responsive and costs
  transcription almost nothing.
- **Dictation flooded the clipboard history.** Delivering text copies it, and
  the watcher recorded every phrase as a new clipboard item. The watcher is
  now told to ignore the app's own copies.
- **The mini bar stretched across the screen.** A multi-line clipboard item (a
  copied transcript) was rendered with all its lines, and because the window
  is not resizable it grew to fit. Previews are now flattened to one line and
  capped with `max-width-chars` — an ellipsized label still *requests* its full
  natural width without that.
- **The mini bar's ✕ dragged the window instead of closing it.** The drag
  gesture was attached to the whole bar, so `begin_move()` stole the pointer
  grab from every button on it. Dragging is now bound to the ⠿ handle alone,
  which also gets a grab cursor.
- **The mini bar's speaker had no stop.** A second click now stops playback,
  and the button shows a stop square while reading.
- **Icons were black on dark themes.** Icon files are named for the theme they
  serve, which is easy to invert by accident (`Dark-Clipboard` is the *white*
  one). Icons are now requested by role — mic, speaker, meeting, clip, gear,
  pin, stop — and resolved to the right variant from the theme's background
  luminance.
- **A second instance could run.** A development flag left the app
  non-unique. Because closing the window collapses to the mini bar and keeps
  the app alive, launching it again started a twin with its own recorder,
  whisper and delivery pipeline on the same microphone — every phrase typed
  twice, and one instance's stop button could not reach the other's. The app
  is single-instance again, and quitting now stops every capture and playback
  process so nothing outlives it.

## v0.0.5 — GTK 4 interface

The interface was rebuilt in **GTK 4**, from a PixelRuller design
(`Assets/VoiceGTK.pixelruller.json`, kept in the repo as the source of truth
for the UI). The new UI ships as `voice-gtk`; the original Tk interface stays
available as `voice`, unchanged.

![Old vs new](Screenshots/gtk4/old-vs-new.png)

### Added

- **GTK 4 interface (`voice-gtk`)** — sidebar + page stack replacing the tab
  bar, with the pages Υπαγόρευση / Ανάγνωση / Σύσκεψη / Πρόχειρο and a
  Ρυθμίσεις page reached from the hamburger menu.
- **Native theming** — no hardcoded accent colours. Buttons use GTK style
  classes (`.suggested-action`, `.destructive-action`) and custom CSS is
  geometry-only, so the app follows whatever theme and accent colour the
  desktop is set to, in light or dark.
- **Live waveform** — dictation and meeting capture draw the real microphone
  level as a bar waveform while recording, next to a running timer.
- **Modal dialogs** — Παράδοση κειμένου (delivery target + paste/Enter/focus
  toggles), Ορισμός συντόμευσης (captures the key combination you actually
  press), Καθαρισμός προχείρου, and a native About dialog.
- **Two-column clipboard grid** — history cards with image thumbnails, pin,
  delete, and read-aloud per item.
- **Mini bar** — an undecorated always-on-top pill with 🎤 / 🔊 / 📋 whose
  clipboard list expands inline; the main window collapses into it on close.
- **Settings persistence** — language, voice, speed, microphone, delivery,
  meeting source and feature toggles are written to
  `~/.config/voicekbd/config` and restored on start. The microphone key stays
  compatible with `dictate.sh`.
- **Feature toggles** — switching a feature off in Ρυθμίσεις → Λειτουργίες
  now hides it everywhere: its sidebar page *and* its mini-bar button.
- Project icon set under `Assets/SVGs/` (microphone on/off, speaker on/off,
  meeting, clipboard, pin, gear) in matching light and dark variants.

### Fixed

- **Dictated text arriving twice.** Every take was written to one fixed temp
  file (`/tmp/voice_gtk_dict.wav`) and transcribed on its own thread, so
  overlapping takes overwrote each other's audio and the same speech came back
  twice. Transcription now runs on a single worker queue with a unique temp
  file per take.
- **Input flood after pressing stop.** Segments already queued kept being
  transcribed, activating windows and pasting long after capture stopped —
  which feels like the app clicking all over the desktop. Stopping now drops
  the backlog and invalidates work already in flight, so nothing is delivered
  after you stop. (Same class of bug as the v0.0.4 fix for the Tk app.)
- **Capture process left running** after stop in some cases; the recorder now
  terminates, then kills, and reaps it.
- **Missing window icon.** GTK 4 only takes window icons from the icon theme,
  so the app now ships a `hicolor` copy of its icon and registers it, and the
  GTK desktop entry is named after the application-id
  (`io.github.kalotrapezis.voice.desktop`) so Wayland compositors match the
  window to it.

### Changed

- Global shortcuts are registered by the GTK app the same way as before
  (`.desktop` entries with `X-KDE-Shortcuts` on KDE, `gsettings` custom
  keybindings on Cinnamon/GNOME) and drive the **same control socket**, so
  both interfaces respond to the same hotkeys.
- The `.deb` now installs both `voice` and `voice-gtk`, with a desktop entry
  for each, and depends on `gir1.2-gtk-4.0`.

### Notes

- The GTK app requires GTK 4 (`gir1.2-gtk-4.0`); libadwaita is **not** needed.
- "Πάντα μπροστά" uses `wmctrl` and therefore applies on X11 sessions. On
  Wayland, GTK 4 exposes no keep-above API — add a KWin window rule for the
  window title *VOICE Mini* to pin the mini bar.
- The system-tray icon currently lives only in the Tk interface (`voice`).

## v0.0.4

- Fixed a paste backlog that could fire after stopping continuous dictation.
- Added per-tab feature toggles.
- Fixed the mini clipboard: pin button stays visible, opens at the pointer on
  top.
- Fixed continuous (auto-pause) capture by using `parec` instead of
  `pw-record --raw`, which did not reliably convert to the requested format.

## v0.0.3

- Cross-distro support: Debian/Ubuntu/Linux Mint/Kubuntu alongside Fedora, with
  the desktop/session differences abstracted in `platform_io.py` (KWin vs
  wmctrl, ydotool vs xdotool, wl-clipboard vs xclip) and a `.deb` build script.
- Clipboard manager: history with image thumbnails, pinning, case-copy, and an
  always-on-top mini view.

## v0.0.2

- Meeting transcription: system-audio or microphone capture with a live
  timestamped transcript, exported to Markdown.

## v0.0.1

- First release: Greek dictation (whisper.cpp), read-aloud (Piper), voice
  remote, continuous mode, mini mode, tray icon and global shortcuts.
