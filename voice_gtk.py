#!/usr/bin/env python3
"""
VOICE — GTK 4 port of the Φωνητικό Πληκτρολόγιο UI.

Faithful implementation of the canonical PixelRuller design
(Assets/VoiceGTK.pixelruller.json): sidebar + page stack with the pages
Υπαγόρευση / Ανάγνωση / Σύσκεψη / Πρόχειρο / Ρυθμίσεις, the hamburger app
menu, the modal dialogs (Παράδοση κειμένου, Ορισμός συντόμευσης, Καθαρισμός
προχείρου, Περί), and the always-on-top Mini bar with its expandable
clipboard list.

Pure GTK 4 (no libadwaita — works with the system theme on KDE/Cinnamon).
Colors come from theme tokens and style classes, never hardcoded accents;
custom CSS is geometry-only per the design's aiTheme porting notes.

Engine wiring (whisper.cpp, Piper, clipboard watcher) hooks into the same
backends as voice_keyboard.py; UI callbacks not yet wired are stubbed and
logged.
"""
import os
import sys
import time
import queue
import shutil
import hashlib
import logging
import datetime
import tempfile
import threading
import subprocess
from collections import deque

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, Gio, GLib, GObject, Pango

# shared engines with the Tkinter app (no UI is created by importing these)
import voice_keyboard as vk
import predict
import llm
import read_aloud as ra
import clipboard as clipmod
import platform_io

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "Assets", "VoiceIcon.png")

# ---------------------------------------------------------------- logging
# Persistent log — launched from a hotkey or the menu there is no terminal,
# so without this file bugs leave no trace to diagnose.
LOG_PATH = os.path.expanduser("~/.cache/voicekbd/voice-gtk.log")
LOG = logging.getLogger("voice")


def _setup_log():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    try:                                   # keep the file from growing forever
        if os.path.getsize(LOG_PATH) > 2_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass
    h = logging.FileHandler(LOG_PATH, encoding="utf-8")
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(threadName)-12s %(message)s"))
    LOG.addHandler(h)
    LOG.setLevel(logging.INFO)


def _pcm_id(pcm):
    """Short fingerprint of an audio segment, to spot duplicates in the log."""
    return f"{len(pcm)}B/{hashlib.md5(pcm).hexdigest()[:8]}"
SVGS = os.path.join(HERE, "Assets", "SVGs")
VERSION = "0.0.6~beta11"


_DARK = None


def dark_theme():
    """True when the active GTK theme is dark (bg luminance < 0.5)."""
    global _DARK
    if _DARK is None:
        ok, c = Gtk.Label().get_style_context().lookup_color(
            "theme_bg_color")
        _DARK = bool(ok) and (
            0.2126 * c.red + 0.7152 * c.green + 0.0722 * c.blue) < 0.5
    return _DARK


# icon set names by role: (light-theme variant, dark-theme variant) — the
# files are named for the theme they serve, so "Dark-Clipboard" is white ink
_ICONS = {
    "mic": ("mic-on-black", "mic-on-white"),
    "speaker": ("Speaker-black-on", "Speaker-white-on"),
    "meeting": ("meeting-black", "meeting-white"),
    "clip": ("Light-Clipboard", "Dark-Clipboard"),
    "gear": ("Gear-Darklines", "Gear-white"),
    "pin": ("pin-dark", "pin-white"),
    "stop": ("stop-black", "stop-white"),
}


def icon_for(role):
    light, dark = _ICONS[role]
    return dark if dark_theme() else light


def svg_icon(name, size=20):
    """Gtk.Image from the project's SVG set (Assets/SVGs/<name>.svg)."""
    img = Gtk.Image.new_from_file(os.path.join(SVGS, name + ".svg"))
    img.set_pixel_size(size)
    return img


def svg_button(name, size=20, css=()):
    btn = Gtk.Button()
    btn.set_child(svg_icon(name, size))
    for c in css:
        btn.add_css_class(c)
    return btn

# ---------------------------------------------------------------- CSS
# Geometry-only, plus neutral surfaces from theme tokens. No accent hexes:
# accent comes from .suggested-action / .destructive-action /
# @theme_selected_bg_color so the app follows the user's theme.
CSS = b"""
window.voice-main { background: @theme_bg_color; }

.sidebar-pane {
  background: shade(@theme_bg_color, 0.97);
  border-right: 1px solid alpha(currentColor, 0.12);
}
.sidebar-pane row {
  border-radius: 8px;
  padding: 10px 12px;
  margin: 1px 6px;
  color: @theme_fg_color;
  background: transparent;
}
.sidebar-pane row:hover {
  background-color: alpha(@theme_fg_color, 0.06);
}
.sidebar-pane row:selected {
  background-color: alpha(@theme_fg_color, 0.12);
  color: @theme_fg_color;
}
.sidebar-pane list { background: transparent; }
.brand-title { font-size: 20px; font-weight: bold; color: @theme_fg_color; }

.voice-card {
  background: @theme_base_color;
  border: 1px solid alpha(currentColor, 0.15);
  border-radius: 12px;
}
.dict-card { border-radius: 16px; padding: 34px; }

.page-header {
  background: @theme_base_color;
  border-bottom: 1px solid alpha(currentColor, 0.12);
  padding: 8px 20px;
  min-height: 54px;
}
.page-title { font-size: 16px; font-weight: bold; }

.row-divider { border-bottom: 1px solid alpha(currentColor, 0.10); }
.settings-row { padding: 12px 16px; min-height: 32px; }

.dim-label { opacity: 0.6; }
.small-label { font-size: 11px; }
.timer-label { font-size: 34px; opacity: 0.55; }
.meet-timer { font-size: 22px; opacity: 0.75; }

.record-button {
  border-radius: 9999px;
  min-width: 66px; min-height: 66px;
  background: #e01b24;   /* semantic record-red, per design */
  color: #ffffff;
}
.record-button:hover { background: #c01c28; }
.play-button {
  border-radius: 9999px;
  min-width: 62px; min-height: 62px;
}
.stop-button {
  border-radius: 9999px;
  min-width: 46px; min-height: 46px;
}
.meet-record { min-width: 54px; min-height: 54px; }

.kbd-chip {
  background: shade(@theme_bg_color, 0.95);
  border: 1px solid alpha(currentColor, 0.2);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
}

.test-area {
  background: shade(@theme_bg_color, 0.97);
  border: 1px solid alpha(currentColor, 0.15);
  border-radius: 8px;
}

.shortcut-capture {
  background: shade(@theme_bg_color, 0.98);
  border: 2px dashed @theme_selected_bg_color;
  border-radius: 8px;
  padding: 18px 16px;
}
.shortcut-key { font-size: 24px; font-weight: bold; }
.accent-text { color: @theme_selected_bg_color; }

.clip-card { border-radius: 12px; padding: 12px 8px 12px 14px; }
.link-text { color: @theme_selected_bg_color; }

/* mini bar sizes: the round buttons set the pill thickness, so each size is
   their box plus matching padding (icon sizes live in MINI_SIZES) */
.voice-mini.size-small .mini-round,
.voice-mini.size-small button.flat { min-width: 22px; min-height: 22px; }
.voice-mini.size-small label { font-size: 11px; }
.voice-mini.size-medium .mini-round,
.voice-mini.size-medium button.flat { min-width: 32px; min-height: 32px; }
.voice-mini.size-medium label { font-size: 12px; }
.voice-mini.size-large .mini-round,
.voice-mini.size-large button.flat { min-width: 44px; min-height: 44px; }
.voice-mini.size-small { border-radius: 14px; }
.voice-mini.size-medium { border-radius: 20px; }
.voice-mini.size-large { border-radius: 38px; }

window.voice-mini {
  background: shade(@theme_bg_color, 0.99);
  border: 1px solid alpha(currentColor, 0.2);
  border-radius: 14px;
}
window.voice-mini.expanded { border-radius: 14px; }
/* half the old height: the round buttons set the bar's thickness */
.mini-round { border-radius: 9999px; padding: 0; }
.mini-handle { padding: 0 4px; font-size: 10px; }
.voice-mini button.flat { padding: 0; }
.mini-round.destructive-action, .mini-round.record-red {
  background: #e01b24; color: #ffffff;
}
.mini-clip-row {
  background: shade(@theme_bg_color, 0.97);
  border-radius: 6px;
  padding: 6px 8px 6px 10px;
}
"""


def _load_css():
    display = Gdk.Display.get_default()
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # Window/taskbar icon. GTK 4 has no per-window icon-from-file: the icon
    # must come from the icon theme by name. Add the bundled hicolor tree so
    # "voice" resolves when running from a source checkout too (installed
    # copies find it in /usr/share/icons).
    theme = Gtk.IconTheme.get_for_display(display)
    theme.add_search_path(os.path.join(HERE, "Assets", "icons"))
    Gtk.Window.set_default_icon_name("voice")


# ---------------------------------------------------------------- engines
class Tts:
    """Piper text-to-speech: chunked synthesis + pw-play, one at a time."""

    def __init__(self):
        self.stop_flag = threading.Event()
        self.proc = None
        self.speaking = False

    def speak(self, text, model, speed, on_state=lambda s: None):
        if self.speaking:
            self.stop()
        text = (text or "").strip()
        if not text or not os.path.exists(model):
            on_state("no-text" if text else "no-text")
            return
        self.stop_flag.clear()
        self.speaking = True
        on_state("playing")
        threading.Thread(target=self._worker,
                         args=(ra.chunks(text), model, speed, on_state),
                         daemon=True).start()

    def _worker(self, parts, model, speed, on_state):
        raw, pwav = "/tmp/voice_read_raw.wav", "/tmp/voice_read_play.wav"
        for part in parts:
            if self.stop_flag.is_set():
                break
            try:
                # Piper's length_scale is inverse to speed
                ls = round(1.0 / max(0.1, speed), 3)
                subprocess.run(
                    [ra.PIPER, "--model", model, "--length_scale", str(ls),
                     "--sentence_silence", "0.3", "--output_file", raw],
                    input=part.encode(), capture_output=True, timeout=60)
                ra.pad_wav(raw, pwav)
            except Exception:
                continue
            if self.stop_flag.is_set():
                break
            self.proc = subprocess.Popen(["pw-play", pwav])
            self.proc.wait()
            self.proc = None
        self.speaking = False
        GLib.idle_add(on_state,
                      "stopped" if self.stop_flag.is_set() else "done")

    def stop(self):
        self.stop_flag.set()
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.speaking = False


class Recorder:
    """Streams a PulseAudio/PipeWire source. `vad=True` splits phrases with
    the shared energy VAD (continuous dictation / meeting); `vad=False`
    accumulates one take for push-to-talk."""

    def __init__(self):
        self.proc = None
        self.stop_event = None
        self.thread = None
        self.buffer = bytearray()

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, dev, on_segment=None, on_level=None, vad=True):
        LOG.info("recorder.start dev=%r vad=%s (was running=%s)",
                 dev, vad, self.running)
        self.stop_event = threading.Event()
        self.buffer = bytearray()
        self.proc = subprocess.Popen(vk.raw_capture_cmd(dev),
                                     stdout=subprocess.PIPE)
        if vad:
            target, args = vk.run_vad_capture, (
                self.proc, self.stop_event, on_segment, on_level)
        else:
            target, args = self._plain_loop, (on_level,)
        self.thread = threading.Thread(target=target, args=args, daemon=True)
        self.thread.start()

    def _plain_loop(self, on_level):
        while not self.stop_event.is_set():
            chunk = self.proc.stdout.read(vk.FRAME_BYTES)
            if not chunk:
                break
            self.buffer += chunk
            if on_level:
                on_level(min(1.0, vk.frame_rms(chunk) / 4000.0))

    def stop(self):
        LOG.info("recorder.stop")
        if self.stop_event:
            self.stop_event.set()
        proc, self.proc = self.proc, None
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()                    # never leave capture running
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                if proc.stdout:
                    proc.stdout.close()        # unblock a reader mid-frame
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=2)
        self.thread = None
        return bytes(self.buffer)


class Transcriber:
    """Runs whisper on one worker thread, newest work last.

    A queue rather than a thread per segment, for two reasons that were both
    real bugs: concurrent takes shared a single temp wav path, so one take
    could overwrite another's audio and the same speech got transcribed (and
    delivered) twice; and pending segments kept activating windows and
    pasting long after "stop" was pressed, which feels like the app clicking
    all over the desktop. `cancel()` drops the backlog so neither can happen.
    """

    def __init__(self):
        self.q = queue.Queue()
        self._gen = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def generation(self):
        with self._lock:
            return self._gen

    def submit(self, pcm, lang, on_text):
        LOG.info("transcriber.submit gen=%d pcm=%s", self.generation,
                 _pcm_id(pcm))
        self.q.put((self.generation, pcm, lang, on_text))

    def cancel(self):
        """Invalidate queued and in-flight work (called when capture stops)."""
        with self._lock:
            self._gen += 1
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def _loop(self):
        while True:
            gen, pcm, lang, on_text = self.q.get()
            if gen != self.generation:                 # stopped since queued
                continue
            if len(pcm) < vk.FRAME_BYTES * vk.MIN_SPEECH:
                continue
            fd, wav = tempfile.mkstemp(prefix="voice-gtk-", suffix=".wav")
            os.close(fd)
            try:
                vk.write_wav(wav, pcm)
                txt = vk.transcribe(wav, lang)
            except Exception:
                txt = ""
            finally:
                try:
                    os.remove(wav)
                except OSError:
                    pass
            # re-check: "stop" may have been pressed while whisper ran
            LOG.info("transcriber.result gen=%d/%d pcm=%s txt=%r",
                     gen, self.generation, _pcm_id(pcm), txt[:60])
            if txt and gen == self.generation:
                GLib.idle_add(on_text, txt)


class GdkClipWatcher:
    """Event-driven clipboard history via Gdk.Clipboard's `changed` signal.

    Replaces the polling ClipWatcher for the GTK app. The old watcher shelled
    out to wl-paste/xclip once per second, and on KDE Wayland every wl-paste
    invocation maps a brief invisible window — which dismisses open context
    menus, makes text selections flicker, and resets hover states across the
    whole desktop, once per second. In-process Gdk reads do none of that, and
    only run when the clipboard actually changes."""

    def __init__(self, store):
        self.store = store
        self.paused = False              # feature toggle: skip capture
        self._ignore = {}                # text -> expiry; app's own copies
        self._last = None
        self.clip = Gdk.Display.get_default().get_clipboard()
        self.clip.connect("changed", self._changed)

    def start(self):
        return True                      # signal-driven; nothing to start

    def ignore(self, text, ttl=10):
        now = time.time()
        self._ignore = {t: e for t, e in self._ignore.items() if e > now}
        self._ignore[text] = now + ttl

    # ---- capture
    def _changed(self, clip):
        if self.paused:
            return
        formats = clip.get_formats()
        if formats.contain_gtype(Gdk.Texture.__gtype__):
            clip.read_texture_async(None, self._got_texture)
        elif formats.contain_gtype(GObject.TYPE_STRING) or \
                formats.contain_mime_type("text/plain;charset=utf-8") or \
                formats.contain_mime_type("text/plain"):
            clip.read_text_async(None, self._got_text)

    def _got_text(self, clip, result):
        try:
            text = clip.read_text_finish(result)
        except GLib.Error:
            return
        if not text or not text.strip():
            return
        exp = self._ignore.get(text)
        if exp and exp > time.time():
            return                       # the app's own copy — don't record
        cid = hashlib.sha256(text.encode()).hexdigest()[:16]
        if cid == self._last:
            return
        self._last = cid
        self.store.add({"id": cid, "kind": "text", "text": text})

    def _got_texture(self, clip, result):
        try:
            texture = clip.read_texture_finish(result)
        except GLib.Error:
            return
        os.makedirs(clipmod.CACHE_DIR, exist_ok=True)
        data = texture.save_to_png_bytes().get_data()
        cid = hashlib.sha256(data).hexdigest()[:16]
        if cid == self._last:
            return
        self._last = cid
        raw = os.path.join(clipmod.CACHE_DIR, cid + ".png")
        if not os.path.exists(raw):
            texture.save_to_png(raw)
        thumb = os.path.join(clipmod.CACHE_DIR, cid + "_t.png")
        if not os.path.exists(thumb):
            clipmod.make_thumbnail(raw, thumb)
        self.store.add({"id": cid, "kind": "image", "path": raw,
                        "thumb": thumb if os.path.exists(thumb) else None,
                        "text": "🖼 εικόνα από το πρόχειρο"})


_deliver_lock = threading.Lock()


def deliver_text(txt, target_key=None, do_paste=True, watcher=None):
    """Copy to clipboard and paste into the target window (or wherever focus
    is if no target). Mirrors the Tkinter app's delivery path.

    Serialized: two phrases finishing close together must never interleave
    their ydotool keystrokes (Ctrl held by one while the other presses V
    pastes twice). The watcher is told to skip our own copy, so dictation
    doesn't flood the clipboard history with every phrase."""
    with _deliver_lock:
        LOG.info("deliver.begin txt=%r target=%r paste=%s",
                 txt[:60], target_key, do_paste)
        if watcher is not None:
            watcher.ignore(txt)
        platform_io.clip_copy(txt)
        if not do_paste:
            return
        if target_key:
            platform_io.activate_target(target_key)
            time.sleep(0.35)
        ok = platform_io.send_paste()
        LOG.info("deliver.paste ok=%s txt=%r", ok, txt[:30])


LANGS = {"Ελληνικά": "el", "English": "en"}
SELF = os.path.abspath(__file__)

SHORTCUTS = [  # (config key, row label, socket command, default combo)
    ("SC_DICT", "🎤  Ομιλία (έναρξη/λήξη)", "talk", "Meta+Z"),
    ("SC_READ", "🔊  Ανάγνωση επιλογής / προχείρου", "read", "Meta+R"),
    ("SC_CLIPMINI", "📋  Πρόχειρο (mini πάνω από όλα)", "clipmini",
     "Ctrl+Alt+Z"),
]


APP_ID = "io.github.kalotrapezis.voice"


def ensure_dev_icon():
    """Source-checkout runs have no .desktop file, so KWin/taskbars can't map
    our Wayland app-id to an icon. Install a user-level desktop entry + icon
    once (the package install provides these system-wide under /usr)."""
    if SELF.startswith("/usr/"):
        return
    apps = os.path.expanduser("~/.local/share/applications")
    icon_dir = os.path.expanduser(
        "~/.local/share/icons/hicolor/256x256/apps")
    desk = os.path.join(apps, f"{APP_ID}.desktop")
    icon = os.path.join(icon_dir, f"{APP_ID}.png")
    try:
        if not os.path.exists(icon):
            os.makedirs(icon_dir, exist_ok=True)
            shutil.copyfile(ICON, icon)
        os.makedirs(apps, exist_ok=True)
        with open(desk, "w") as f:
            f.write("[Desktop Entry]\nType=Application\n"
                    "Name=VOICE — Φωνή (GTK)\n"
                    f'Exec={sys.executable} "{SELF}"\n'
                    f"Icon={APP_ID}\nTerminal=false\n"
                    "Categories=Utility;Accessibility;\n"
                    "NoDisplay=false\nStartupNotify=true\n")
    except OSError:
        pass


def send_ctl(action):
    """Send a command to the running app's control socket. True on success."""
    import socket as _socket
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        s.sendto(action.encode(), vk.CTL_SOCK)
        return True
    except OSError:
        return False


def _desktop_shortcut(slug, name, command, combo):
    """KDE global shortcut via a hidden .desktop entry (same scheme as the
    Tkinter app, so re-applying updates the same entries)."""
    path = os.path.expanduser(f"~/.local/share/applications/{slug}.desktop")
    if not combo:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nType=Application\n"
                f"Name=Φωνή — {name}\nExec={command}\n"
                "NoDisplay=true\nTerminal=false\n"
                f"X-KDE-Shortcuts={combo}\n")


def shortcut_command(action):
    """Command a global shortcut should run. When installed from the package,
    use the `voice-gtk` launcher — it sets LD_LIBRARY_PATH for the bundled
    whisper.cpp libs. From a source checkout, call this script directly."""
    launcher = shutil.which("voice-gtk")
    if launcher and SELF.startswith("/usr/"):
        return f"{launcher} {action}"
    return f'{sys.executable} "{SELF}" {action}'


def register_shortcuts(cfg):
    """Register the global shortcuts with the desktop (KDE or Cinnamon)."""
    cmds = {key: shortcut_command(action)
            for key, _, action, _ in SHORTCUTS}
    if platform_io.IS_KDE:
        for key, label, action, _ in SHORTCUTS:
            _desktop_shortcut(f"voice-shortcut-{action}",
                              label.split("  ", 1)[-1],
                              cmds[key], cfg.get(key, ""))
        for kb in ("kbuildsycoca6", "kbuildsycoca5"):
            try:
                subprocess.run([kb], capture_output=True, timeout=20)
                break
            except Exception:
                continue
    else:
        vk.cinnamon_register([
            (f"voice-{action}", f"Φωνή — {label.split('  ', 1)[-1]}",
             cmds[key], cfg.get(key, ""))
            for key, label, action, _ in SHORTCUTS])


# ---------------------------------------------------------------- helpers
def list_mic_sources():
    """Microphone sources via pactl (same approach as voice_keyboard.py)."""
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True).stdout
    except FileNotFoundError:
        return []
    devs = []
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            devs.append(cols[1])
    return devs


KWIN_RULES = os.path.expanduser("~/.config/kwinrulesrc")
# rule id -> (description, window title it matches). KWin is the only thing on
# Plasma that can put a window above others: GTK 4 dropped set_keep_above and
# Wayland gives a client no say in stacking, so "Πάντα μπροστά" and the mini
# bar are dead on this session unless we write the rule ourselves.
KWIN_ABOVE_RULES = {
    "voice-main-above": ("VOICE — πάντα μπροστά", "VOICE"),
    "voice-mini-above": ("VOICE Mini — πάντα μπροστά", "VOICE Mini"),
}


# mini bar sizes: (icon px, outer margin, inner spacing). "large" is the
# original bar, "small" the slim one — medium sits between them.
MINI_SIZES = {
    "small": (13, 2, 4),
    "medium": (18, 6, 8),
    "large": (22, 10, 10),
}
MINI_SIZE_LABELS = [("small", "Μικρό"), ("medium", "Μεσαίο"),
                    ("large", "Μεγάλο")]


def mini_size():
    key = vk.load_config().get("MINI_SIZE", "medium")
    return key if key in MINI_SIZES else "medium"


def on_plasma():
    return "kde" in (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()


def kwin_keep_above(rule_id, enabled):
    """Add or drop one KWin 'keep above' rule, then make KWin re-read them.

    Edits ~/.config/kwinrulesrc in place with configparser so any rule the
    user made by hand survives untouched."""
    if not on_plasma():
        return
    import configparser
    desc, title = KWIN_ABOVE_RULES[rule_id]
    cp = configparser.RawConfigParser()
    cp.optionxform = str                      # KWin's keys are case-sensitive
    cp.read(KWIN_RULES, encoding="utf-8")
    if enabled:
        cp[rule_id] = {"Description": desc, "above": "true", "aboverule": "2",
                       "title": title, "titlematch": "1"}
    elif cp.has_section(rule_id):
        cp.remove_section(rule_id)
    ids = [s for s in cp.sections() if s != "General"]
    if not cp.has_section("General"):
        cp.add_section("General")
    cp["General"]["count"] = str(len(ids))
    cp["General"]["rules"] = ",".join(ids)
    os.makedirs(os.path.dirname(KWIN_RULES), exist_ok=True)
    with open(KWIN_RULES, "w", encoding="utf-8") as f:
        cp.write(f, space_around_delimiters=False)
    for qdbus in ("qdbus6", "qdbus"):
        try:
            subprocess.run([qdbus, "org.kde.KWin", "/KWin", "reconfigure"],
                           capture_output=True, timeout=5)
            break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue


_PREDICTOR = None


def predictor():
    """Shared word predictor. Built on first use — the dictionary index is
    only touched then, so a user who never opens the notepad never pays."""
    global _PREDICTOR
    if _PREDICTOR is None:
        _PREDICTOR = predict.Predictor(
            learned_path=os.path.join(ra.voice_data_dir(), "words.json"))
    return _PREDICTOR


def dim(label, small=True):
    label.add_css_class("dim-label")
    if small:
        label.add_css_class("small-label")
    return label


def settings_row(title, subtitle, control, divider=True):
    """GNOME-style row: title (+dim subtitle) left, control cluster right."""
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    row.add_css_class("settings-row")
    if divider:
        row.add_css_class("row-divider")
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                   hexpand=True, valign=Gtk.Align.CENTER)
    t = Gtk.Label(label=title, xalign=0)
    text.append(t)
    if subtitle:
        s = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        dim(s)
        text.append(s)
    row.append(text)
    if not isinstance(control, (list, tuple)):
        control = [control]
    for c in control:
        c.set_valign(Gtk.Align.CENTER)
        row.append(c)
    return row


def group(heading, subtitle, card):
    """Heading + dim subtitle + a .voice-card below (settings group)."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    h = Gtk.Label(label=heading, xalign=0)
    h.add_css_class("heading")
    box.append(h)
    if subtitle:
        box.append(dim(Gtk.Label(label=subtitle, xalign=0)))
    card.add_css_class("voice-card")
    box.append(card)
    return box


def dropdown(items, selected=0):
    dd = Gtk.DropDown.new_from_strings(items)
    dd.set_selected(selected)
    return dd


def page_header(title, *end_children):
    hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    hdr.add_css_class("page-header")
    t = Gtk.Label(label=title, xalign=0, hexpand=True)
    t.add_css_class("page-title")
    hdr.append(t)
    for c in end_children:
        c.set_valign(Gtk.Align.CENTER)
        hdr.append(c)
    return hdr


def scrolled(child):
    sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
    sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    sw.set_child(child)
    return sw


# ---------------------------------------------------------------- pages
class DictationPage(Gtk.Box):
    """Page: Υπαγόρευση — big mic card + Έξοδος & δοκιμή group."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.append(page_header(
            "Υπαγόρευση",
            dim(Gtk.Label(label="Πάτα για ομιλία · οι ρυθμίσεις στο ⚙"), small=False)))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                       margin_top=24, margin_bottom=24,
                       margin_start=24, margin_end=24)

        # --- main dictation card
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        card.add_css_class("voice-card")
        card.add_css_class("dict-card")

        self.wave = Gtk.DrawingArea(content_width=360, content_height=120,
                                    halign=Gtk.Align.CENTER)
        self.wave.set_draw_func(self._draw_wave)
        card.append(self.wave)

        self.timer = Gtk.Label(label="00:00:00.0", halign=Gtk.Align.CENTER)
        self.timer.add_css_class("timer-label")
        card.append(self.timer)

        mode = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                       halign=Gtk.Align.CENTER)
        lbl_ptt = Gtk.Label(label="Με πάτημα")
        lbl_ptt.add_css_class("heading")
        self.mode_toggle = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.mode_toggle.connect("notify::active", self._on_mode)
        mode.append(lbl_ptt)
        mode.append(self.mode_toggle)
        mode.append(dim(Gtk.Label(label="Συνεχής εγγραφή"), small=False))
        card.append(mode)

        self.record_btn = svg_button("mic-on-white", 28,
                                     ("destructive-action", "record-button"))
        self.record_btn.set_halign(Gtk.Align.CENTER)
        self.record_btn.connect("clicked", self._on_record)
        card.append(self.record_btn)

        self.status = dim(Gtk.Label(label="Έτοιμο", halign=Gtk.Align.CENTER),
                          small=False)
        card.append(self.status)
        body.append(card)

        # --- Έξοδος & δοκιμή
        out_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.win_keys = [None]
        self.target_drop = dropdown(["Ενεργό παράθυρο (focus)"])
        GLib.idle_add(self._refresh_targets)
        gear = svg_button(icon_for("gear"), 18, ("flat", "circular"))
        gear.connect("clicked", lambda *_: TargetDialog(self.win).present())
        out_card.append(settings_row(
            "Στόχος", "Πού πάει το κείμενο που υπαγορεύεις",
            [self.target_drop, gear]))

        test_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                           margin_top=12, margin_bottom=14,
                           margin_start=14, margin_end=14)
        test_box.append(dim(Gtk.Label(label="Δοκιμή", xalign=0)))
        self.test_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.test_view.add_css_class("test-area")
        self.test_view.set_size_request(-1, 84)
        self.test_view.get_buffer().set_text(
            "Πες κάτι… το αναγνωρισμένο κείμενο εμφανίζεται εδώ για δοκιμή.")
        test_box.append(self.test_view)
        out_card.append(test_box)

        body.append(group("Έξοδος & δοκιμή", None, out_card))
        self.append(scrolled(body))

        # engine state
        self.rec = Recorder()
        self.transcriber = Transcriber()
        self.levels = deque([0.0] * 60, maxlen=60)
        self.t0 = None
        self._tick_id = None

    # ---- targets
    def _refresh_targets(self):
        wins = platform_io.list_windows()
        self.win_keys = [None] + [k for k, _ in wins]
        names = ["Ενεργό παράθυρο (focus)"] + [t for _, t in wins]
        self.target_drop.set_model(Gtk.StringList.new(names))
        return False

    def target_key(self):
        i = self.target_drop.get_selected()
        return self.win_keys[i] if 0 <= i < len(self.win_keys) else None

    # ---- waveform / timer
    def _draw_wave(self, area, cr, w, h):
        ok, color = area.get_style_context().lookup_color(
            "theme_selected_bg_color")
        if ok:
            cr.set_source_rgba(color.red, color.green, color.blue, 0.6)
        n = len(self.levels)
        bw = w / n
        for i, lv in enumerate(self.levels):
            bh = max(2, lv * h)
            cr.rectangle(i * bw + 1, (h - bh) / 2, bw - 2, bh)
        cr.fill()

    def _on_level(self, v):
        self.levels.append(v)

    def _tick(self):
        if self.t0 is not None:
            dt = time.monotonic() - self.t0
            self.timer.set_label(
                f"{int(dt // 3600):02d}:{int(dt % 3600 // 60):02d}:"
                f"{dt % 60:04.1f}")
        self.wave.queue_draw()
        return self.rec.running

    def _start_ui(self, status):
        self.t0 = time.monotonic()
        self.status.set_label(status)
        self.record_btn.set_child(svg_icon("stop-white", 26))
        self._tick_id = GLib.timeout_add(100, self._tick)

    def _stop_ui(self, status="Έτοιμο"):
        self.t0 = None
        self.status.set_label(status)
        self.record_btn.set_child(svg_icon("mic-on-white", 28))
        self.levels.extend([0.0] * self.levels.maxlen)
        self.wave.queue_draw()

    # ---- record flow
    def _lang(self):
        return self.win.settings().dict_lang()

    def _on_record(self, btn):
        if self.rec.running:
            if self.mode_toggle.get_active():          # continuous → stop
                self.rec.stop()
                # drop anything still queued, or it keeps pasting after stop
                self.transcriber.cancel()
                self._stop_ui()
            else:                                      # PTT → transcribe take
                pcm = self.rec.stop()
                self._stop_ui("Μεταγραφή…")
                self.transcriber.submit(pcm, self._lang(), self._got_text)
            return
        dev = self.win.settings().mic_device()
        if self.mode_toggle.get_active():
            self.rec.start(dev, on_segment=self._on_segment,
                           on_level=self._on_level, vad=True)
            self._start_ui("Συνεχής εγγραφή — γράφει στις παύσεις…")
        else:
            self.rec.start(dev, on_level=self._on_level, vad=False)
            self._start_ui("Ομιλία σε εξέλιξη…")

    def _on_segment(self, pcm):
        LOG.info("dict.segment pcm=%s", _pcm_id(pcm))
        self.transcriber.submit(pcm, self._lang(), self._got_text)

    def _got_text(self, txt):
        LOG.info("dict.got_text txt=%r deliver=%s", txt[:60],
                 self.win.settings().deliver_enabled())
        if not self.rec.running:
            self.status.set_label("Έτοιμο")
        if not txt:
            return
        buf = self.test_view.get_buffer()
        old = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if old.startswith("Πες κάτι…"):
            old = ""
        buf.set_text((old + " " + txt).strip())
        if self.win.settings().deliver_enabled():
            # if VOICE itself is focused, the paste would land in our own
            # test box — which already shows the text — doubling every phrase
            paste = not self.win.is_active()
            threading.Thread(
                target=deliver_text, args=(txt, self.target_key()),
                kwargs={"watcher": self.win.app.watcher, "do_paste": paste},
                daemon=True).start()

    def _on_mode(self, sw, _):
        if self.rec.running:
            self.rec.stop()
            self._stop_ui()
        self.status.set_label("Συνεχής εγγραφή" if sw.get_active()
                              else "Έτοιμο")


class ReadPage(Gtk.Box):
    """Page: Ανάγνωση — text card + play/pause + status."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        open_btn = Gtk.Button(label="Άνοιγμα")
        open_btn.connect("clicked", self._on_open)
        paste_btn = Gtk.Button(label="Επικόλληση")
        paste_btn.connect("clicked", self._on_paste)
        self.append(page_header("Ανάγνωση", open_btn, paste_btn))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                       margin_top=22, margin_bottom=22,
                       margin_start=26, margin_end=26)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        card.add_css_class("voice-card")
        self.text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                      vexpand=True,
                                      top_margin=18, bottom_margin=18,
                                      left_margin=20, right_margin=20)
        self.text_view.get_buffer().set_text(
            "Επίλεξε ένα κομμάτι κειμένου και πάτησε αναπαραγωγή για να το "
            "ακούσεις — ή άνοιξε ένα PDF και άφησέ το να διαβαστεί ολόκληρο.")
        card.append(scrolled(self.text_view))
        body.append(card)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16,
                           halign=Gtk.Align.CENTER)
        self.play_btn = Gtk.Button(
            icon_name="media-playback-start-symbolic")
        self.play_btn.add_css_class("suggested-action")
        self.play_btn.add_css_class("play-button")
        self.play_btn.connect("clicked", self._on_play)
        self.stop_btn = Gtk.Button(icon_name="media-playback-pause-symbolic")
        self.stop_btn.add_css_class("stop-button")
        self.stop_btn.connect("clicked", self._on_stop)
        controls.append(self.play_btn)
        controls.append(self.stop_btn)
        body.append(controls)

        self.status = dim(Gtk.Label(
            label="Έτοιμο · φωνή «Μεσαία», ρυθμός 1.0× — άλλαξέ τα στις Ρυθμίσεις",
            halign=Gtk.Align.CENTER), small=False)
        body.append(self.status)
        self.append(body)

    def _on_open(self, btn):
        d = Gtk.FileDialog(title="Άνοιγμα αρχείου")
        d.open(self.win, None, self._opened)

    def _opened(self, dialog, result):
        try:
            f = dialog.open_finish(result)
        except GLib.Error:
            return
        path = f.get_path()
        ext = os.path.splitext(path)[1].lower()
        self.status.set_label(f"Φόρτωση {os.path.basename(path)}…")

        def load():
            try:
                if ext in (".txt", ".md", ""):
                    text = open(path, encoding="utf-8",
                                errors="ignore").read()
                elif ext == ".pdf":
                    text = subprocess.run(
                        ["pdftotext", "-layout", path, "-"],
                        capture_output=True, text=True, timeout=60).stdout
                else:
                    subprocess.run(
                        ["libreoffice", "--headless", "--convert-to",
                         "txt:Text", "--outdir", "/tmp", path],
                        capture_output=True, timeout=120)
                    base = os.path.splitext(os.path.basename(path))[0]
                    text = open(f"/tmp/{base}.txt", encoding="utf-8",
                                errors="ignore").read()
            except Exception as e:
                GLib.idle_add(self.status.set_label, f"Σφάλμα: {e}")
                return
            GLib.idle_add(self._loaded, path, text)
        threading.Thread(target=load, daemon=True).start()

    def _loaded(self, path, text):
        self.text_view.get_buffer().set_text(text.strip())
        self.status.set_label(
            f"{os.path.basename(path)} · {len(text.split())} λέξεις")

    def _on_paste(self, btn):
        clip = Gdk.Display.get_default().get_clipboard()
        clip.read_text_async(None, self._pasted)

    def _pasted(self, clip, result):
        try:
            text = clip.read_text_finish(result)
        except GLib.Error:
            return
        if text:
            self.text_view.get_buffer().set_text(text)

    def _on_play(self, btn):
        if self.win.app.tts.speaking:      # the play button doubles as stop
            self.win.app.tts.stop()
            return
        buf = self.text_view.get_buffer()
        bounds = buf.get_selection_bounds()
        if bounds:
            text = buf.get_text(bounds[0], bounds[1], False)
        else:
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(),
                                False)
        st = self.win.settings()
        self.win.app.tts.speak(text, st.voice_model(), st.speed(),
                               self._tts_state)

    def _tts_state(self, state):
        msgs = {"playing": "Διαβάζω…", "done": "Τέλος ανάγνωσης",
                "stopped": "Σταμάτησε", "no-text": "Δεν υπάρχει κείμενο"}
        self.status.set_label(msgs.get(state, "Έτοιμο"))
        if state == "playing":
            self.play_btn.set_child(svg_icon("stop-white", 22))
        else:
            self.play_btn.set_child(Gtk.Image.new_from_icon_name(
                "media-playback-start-symbolic"))
        return False

    def _on_stop(self, btn):
        self.win.app.tts.stop()


class MeetingPage(Gtk.Box):
    """Page: Σύσκεψη — capture strip + live transcript card."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.append(page_header(
            "Σύσκεψη",
            dim(Gtk.Label(label="Ήχος συστήματος · Ελληνικά — άλλαξε στο ⚙"),
                small=False)))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=18, margin_bottom=20,
                       margin_start=26, margin_end=26)

        cap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        cap.add_css_class("voice-card")
        cap.set_margin_bottom(0)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16,
                        margin_top=14, margin_bottom=14,
                        margin_start=18, margin_end=18, hexpand=True)
        self.rec_btn = svg_button(
            "mic-on-white", 24,
            ("destructive-action", "record-button", "meet-record"))
        self.rec_btn.set_valign(Gtk.Align.CENTER)
        self.rec_btn.connect("clicked", self._on_record)
        inner.append(self.rec_btn)
        self.timer = Gtk.Label(label="00:00:00", valign=Gtk.Align.CENTER)
        self.timer.add_css_class("meet-timer")
        inner.append(self.timer)
        self.wave = Gtk.DrawingArea(content_height=54, hexpand=True)
        inner.append(self.wave)
        cap.append(inner)
        body.append(cap)

        tcard = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                        vexpand=True)
        tcard.add_css_class("voice-card")
        tinner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         margin_top=16, margin_bottom=16,
                         margin_start=18, margin_end=18, vexpand=True)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ht = Gtk.Label(label="Μεταγραφή", xalign=0, hexpand=True)
        ht.add_css_class("heading")
        head.append(ht)
        head.append(dim(Gtk.Label(label="Αποθηκεύεται ως .md στο τέλος")))
        tinner.append(head)
        self.transcript = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                  spacing=6)
        tinner.append(scrolled(self.transcript))
        tcard.append(tinner)
        body.append(tcard)
        self.append(body)

        self.rec = Recorder()
        self.transcriber = Transcriber()
        self.t0 = None
        self.lines = []
        self.wave.set_draw_func(self._draw_wave)
        self.levels = deque([0.0] * 80, maxlen=80)

    def _draw_wave(self, area, cr, w, h):
        ok, color = area.get_style_context().lookup_color(
            "theme_selected_bg_color")
        if ok:
            cr.set_source_rgba(color.red, color.green, color.blue, 0.6)
        bw = w / len(self.levels)
        for i, lv in enumerate(self.levels):
            bh = max(2, lv * h)
            cr.rectangle(i * bw + 1, (h - bh) / 2, bw - 2, bh)
        cr.fill()

    def add_line(self, stamp, text):
        lbl = Gtk.Label(label=f"[{stamp}]  {text}", xalign=0, wrap=True,
                        selectable=True)
        self.transcript.append(lbl)
        self.lines.append(f"[{stamp}] {text}")
        return False

    def _stamp(self):
        dt = int(time.monotonic() - self.t0)
        return f"{dt // 3600:02d}:{dt % 3600 // 60:02d}:{dt % 60:02d}"

    def _tick(self):
        if self.t0 is not None:
            self.timer.set_label(self._stamp())
        self.wave.queue_draw()
        return self.rec.running

    def _on_record(self, btn):
        if self.rec.running:
            self.rec.stop()
            self.transcriber.cancel()
            self.rec_btn.set_child(svg_icon("mic-on-white", 24))
            self._export()
            return
        st = self.win.settings()
        dev = st.meeting_device()
        if not dev:
            self.add_line("--:--:--", "Δεν βρέθηκε πηγή ήχου.")
            return
        while (c := self.transcript.get_first_child()):
            self.transcript.remove(c)
        self.lines = []
        self.t0 = time.monotonic()
        self.rec_btn.set_child(svg_icon("stop-white", 22))
        self.rec.start(dev, on_segment=self._on_segment,
                       on_level=self.levels.append, vad=True)
        GLib.timeout_add(200, self._tick)

    def _on_segment(self, pcm):
        stamp = self._stamp()
        self.transcriber.submit(
            pcm, self.win.settings().meet_lang(),
            lambda txt, s=stamp: self.add_line(s, txt))

    def _export(self):
        if not self.lines:
            return
        d = self.win.settings().export_dir()
        os.makedirs(d, exist_ok=True)
        name = datetime.datetime.now().strftime(
            "Σύσκεψη_%Y-%m-%d_%H-%M.md")
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Μεταγραφή σύσκεψης\n\n")
            f.write("\n".join(self.lines) + "\n")
        GLib.idle_add(self.add_line, self._stamp(),
                      f"Αποθηκεύτηκε: {path}")


class ClipboardPage(Gtk.Box):
    """Page: Πρόχειρο — 2-column grid of history cards (design note_2)."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.count_lbl = dim(Gtk.Label(label="0 στοιχεία"), small=False)
        clear = Gtk.Button(label="Καθαρισμός")
        clear.connect("clicked", self._on_clear)
        self.append(page_header("Πρόχειρο", self.count_lbl, clear))

        self.flow = Gtk.FlowBox(max_children_per_line=2,
                                min_children_per_line=2,
                                column_spacing=14, row_spacing=14,
                                selection_mode=Gtk.SelectionMode.NONE,
                                homogeneous=True, valign=Gtk.Align.START,
                                margin_top=16, margin_bottom=16,
                                margin_start=26, margin_end=26)
        self.append(scrolled(self.flow))

    def refresh(self):
        self.set_items(self.win.app.store.ordered())
        return False

    def set_items(self, items):
        """items: clipboard-store entries {id, kind, text, thumb?, pinned}."""
        while (c := self.flow.get_first_child()):
            self.flow.remove(c)
        for it in items:
            self.flow.append(self._card(it))
        n = len(items)
        self.count_lbl.set_label(f"{n} στοιχεία" if n != 1 else "1 στοιχείο")

    def _card(self, item):
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        card.add_css_class("voice-card")
        card.add_css_class("clip-card")
        card.set_size_request(-1, 150)

        is_url = item.get("kind") == "text" and \
            item.get("text", "").startswith(("http://", "https://"))
        if item.get("kind") == "image" and item.get("thumb") and \
                os.path.exists(item["thumb"]):
            main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                           hexpand=True)
            pic = Gtk.Picture.new_for_filename(item["thumb"])
            pic.set_vexpand(True)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            main.append(pic)
            main.append(dim(Gtk.Label(
                label=item.get("text", ""), xalign=0,
                ellipsize=Pango.EllipsizeMode.MIDDLE)))
            card.append(main)
        else:
            preview = item.get("text", "")
            if len(preview) > 220:
                preview = preview[:220].rstrip() + "…"
            txt = Gtk.Label(label=preview, xalign=0, yalign=0,
                            wrap=True, hexpand=True, vexpand=True,
                            ellipsize=Pango.EllipsizeMode.END, lines=6)
            if is_url:
                txt.add_css_class("link-text")
            card.append(txt)

        # click on the card re-copies the item
        click = Gtk.GestureClick()
        click.connect("released",
                      lambda *_, it=item: clipmod.recopy(it))
        card.add_controller(click)

        acts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       valign=Gtk.Align.CENTER)
        if item.get("kind") != "image":
            aa = Gtk.Button(label="Aa")
            aa.add_css_class("flat")
            aa.set_tooltip_text("Ανάγνωση")
            aa.connect("clicked", lambda *_, it=item: self._read(it))
            acts.append(aa)
        pin = svg_button(icon_for("pin"), 16, ("flat",))
        pin.set_tooltip_text("Καρφίτσωμα")
        if item.get("pinned"):
            pin.add_css_class("suggested-action")
        pin.connect("clicked", lambda *_, it=item: self._pin(it))
        acts.append(pin)
        rm = Gtk.Button(icon_name="user-trash-symbolic")
        rm.add_css_class("flat")
        rm.set_tooltip_text("Διαγραφή")
        rm.connect("clicked",
                   lambda *_, it=item: self.win.app.store.delete(it["id"]))
        acts.append(rm)
        card.append(acts)
        return card

    def _read(self, item):
        st = self.win.settings()
        self.win.app.tts.speak(item.get("text", ""), st.voice_model(),
                               st.speed())

    def _pin(self, item):
        self.win.app.store.set_pinned(item["id"],
                                      not item.get("pinned", False))

    def _on_clear(self, btn):
        ConfirmClearDialog(self.win).present()


class NotepadPage(Gtk.Box):
    """Page: Σημειωματάριο — a plain editor that predicts and corrects.

    Inside our own window none of the AT-SPI guesswork applies: we own the
    buffer, so the half-typed word is simply there. Space takes the first
    suggestion (that is the whole interaction — no extra chord to learn), the
    chips are clickable for the other two, and Escape dismisses them."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.suggestions = []
        self.picked = -1                   # which chip Tab has walked to

        self.rec = Recorder()
        self.transcriber = Transcriber()

        self.mic_btn = svg_button("mic-on-white", 18,
                                  ("destructive-action", "mini-round"))
        self.mic_btn.set_tooltip_text("Υπαγόρευση μέσα στο σημείωμα")
        self.mic_btn.connect("clicked", self._on_mic)
        self.speak_btn = svg_button(icon_for("speaker"), 18, ("mini-round",))
        self.speak_btn.set_tooltip_text("Ανάγνωση επιλογής ή όλου")
        self.speak_btn.connect("clicked", self._on_speak)
        paste = Gtk.Button(label="Επικόλληση")
        paste.connect("clicked", self._on_paste)
        fix = Gtk.Button(label="Διόρθωση")
        fix.connect("clicked", self._on_correct)
        self.ai_btn = Gtk.Button(label="Διόρθωση AI")
        self.ai_btn.add_css_class("suggested-action")
        self.ai_btn.set_tooltip_text(
            "Διορθώνει και σύνταξη/νόημα με το τοπικό μοντέλο")
        self.ai_btn.connect("clicked", self._on_correct_ai)
        copy = Gtk.Button(label="Αντιγραφή")
        copy.connect("clicked", self._on_copy)
        self.append(page_header("Σημειωματάριο", self.mic_btn,
                                self.speak_btn, paste, fix, self.ai_btn,
                                copy))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_top=14, margin_bottom=14,
                       margin_start=26, margin_end=26)

        self.view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 left_margin=12, right_margin=12,
                                 top_margin=10, bottom_margin=10)
        self.buf = self.view.get_buffer()
        self.buf.connect("changed", self._on_changed)
        self.buf.connect("mark-set", lambda *_: self._update_counts())
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.view.add_controller(keys)
        frame = Gtk.Box(vexpand=True)
        frame.add_css_class("voice-card")
        frame.append(scrolled(self.view))
        body.append(frame)

        # suggestions ride under the caret, not at the bottom of the page.
        # autohide=False is the load-bearing bit: an autohiding popover grabs
        # focus, and the editor you are typing in would stop receiving keys.
        self.chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                             margin_top=4, margin_bottom=4,
                             margin_start=6, margin_end=6)
        self.pop = Gtk.Popover(autohide=False, has_arrow=False,
                               position=Gtk.PositionType.BOTTOM)
        self.pop.set_parent(self.view)
        self.pop.set_child(self.chips)

        self.hint = dim(Gtk.Label(label="", xalign=0, hexpand=True),
                        small=False)
        self.counts = dim(Gtk.Label(label="λέξεις 0 · χαρακτήρες 0"),
                          small=False)
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        status.append(self.hint)
        status.append(self.counts)
        body.append(status)
        self.append(body)

    # ---- prediction
    def _word_before_cursor(self):
        """(prefix, start iter, end iter) for the word being typed."""
        end = self.buf.get_iter_at_mark(self.buf.get_insert())
        start = end.copy()
        while start.backward_char():
            if not start.get_char().isalpha():
                start.forward_char()
                break
        return self.buf.get_text(start, end, False), start, end

    def _on_changed(self, *_):
        self._update_counts()
        prefix, _, _ = self._word_before_cursor()
        self.suggestions = predictor().suggest(prefix) if prefix else []
        self.picked = -1                   # a fresh word highlights nothing
        self._show_chips()

    def _show_chips(self):
        while (c := self.chips.get_first_child()):
            self.chips.remove(c)
        if not self.suggestions:
            self.pop.popdown()
            return
        for i, word in enumerate(self.suggestions):
            b = Gtk.Button(label=word)
            b.add_css_class("suggested-action" if i == self.picked
                            else "kbd-chip")
            b.connect("clicked", lambda _b, w=word: self._accept(w))
            self.chips.append(b)
        self._point_at_caret()
        self.pop.popup()

    # the popover sits this far below the caret: pinned to the caret box
    # itself it covered the line being written
    CARET_GAP = 16

    def _point_at_caret(self):
        """Park the popover under the caret, clear of the current line."""
        it = self.buf.get_iter_at_mark(self.buf.get_insert())
        r = self.view.get_iter_location(it)
        x, y = self.view.buffer_to_window_coords(
            Gtk.TextWindowType.WIDGET, r.x, r.y)
        # Gdk.Rectangle is a boxed struct: PyGObject ignores constructor
        # kwargs (silently, with all fields left at 0), so the popover was
        # being pointed at the widget's top-left corner all along
        rect = Gdk.Rectangle()
        rect.x, rect.y = x, y
        rect.width = max(r.width, 1)
        rect.height = max(r.height, 1) + self.CARET_GAP
        self.pop.set_pointing_to(rect)

    def _accept(self, word):
        prefix, start, end = self._word_before_cursor()
        if not prefix:
            return
        self.buf.delete(start, end)
        self.buf.insert_at_cursor(word + " ")
        self._dismiss()
        self.view.grab_focus()

    def _on_key(self, ctl, keyval, keycode, state):
        """Tab walks the suggestions, Enter takes one, Esc drops them.

        Nothing is inserted until Enter, so Tab is safe to lean on: you can
        look at all three and still end up typing your own word."""
        if not self.suggestions:
            return False
        if keyval == Gdk.KEY_Escape:
            self._dismiss()
            return True
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            step = -1 if keyval == Gdk.KEY_ISO_Left_Tab else 1
            if self.picked < 0:            # -1 + -1 would land on the middle
                self.picked = 0 if step > 0 else len(self.suggestions) - 1
            else:
                self.picked = (self.picked + step) % len(self.suggestions)
            self._show_chips()
            return True                    # never a literal tab in the note
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.picked >= 0:
            self._accept(self.suggestions[self.picked])
            return True                    # and no newline either
        return False

    def _dismiss(self):
        self.suggestions = []
        self.picked = -1
        self._show_chips()

    # ---- counts
    def _update_counts(self):
        text = self.buf.get_text(*self.buf.get_bounds(), False)
        label = f"λέξεις {len(text.split())} · χαρακτήρες {len(text)}"
        sel = self.buf.get_selection_bounds()
        if sel:
            picked = self.buf.get_text(sel[0], sel[1], False)
            label = (f"λέξεις {len(text.split())} ({len(picked.split())})"
                     f" · χαρακτήρες {len(text)} ({len(picked)})")
        self.counts.set_label(label)

    # ---- actions
    def _on_paste(self, btn):
        self.get_clipboard().read_text_async(None, self._pasted)

    def _pasted(self, clipboard, result):
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error:
            return
        if text:
            self.buf.insert_at_cursor(text)
            predictor().learn(text)

    def _on_copy(self, btn):
        self.get_clipboard().set(
            self.buf.get_text(*self.buf.get_bounds(), False))

    def _on_correct_ai(self, btn):
        """Context-aware pass. Runs off the main loop — 12 s a paragraph
        would freeze the window, and the note must stay readable meanwhile."""
        if not llm.available():
            self.hint.set_label(
                "Το μοντέλο AI δεν έχει ληφθεί — Ρυθμίσεις → Διόρθωση AI")
            return
        status, msg = llm.ram_check()
        if status == "block":
            InfoDialog(self.win, "Δεν υπάρχει αρκετή μνήμη", msg).present()
            return
        sel = self.buf.get_selection_bounds()
        start, end = sel if sel else self.buf.get_bounds()
        text = self.buf.get_text(start, end, False)
        if not text.strip():
            return
        self.ai_btn.set_sensitive(False)
        self.hint.set_label("Διόρθωση AI… (κρατάει λίγο)")

        def work():
            fixed = llm.correct(text, on_progress=lambda i, n: GLib.idle_add(
                self.hint.set_label, f"Διόρθωση AI… {i}/{n}"))
            GLib.idle_add(self._ai_done, text, fixed)
        threading.Thread(target=work, daemon=True).start()

    def _ai_done(self, original, fixed):
        self.ai_btn.set_sensitive(True)
        if not fixed or fixed == original:
            self.hint.set_label("Καμία αλλαγή από το AI")
            return
        # re-resolve the range: the note may have been edited while it ran
        sel = self.buf.get_selection_bounds()
        start, end = sel if sel else self.buf.get_bounds()
        if self.buf.get_text(start, end, False) != original:
            self.hint.set_label("Το κείμενο άλλαξε — η διόρθωση ακυρώθηκε")
            return
        self.buf.delete(start, end)
        self.buf.insert(start, fixed)
        predictor().learn(fixed)
        self.hint.set_label("Έγινε διόρθωση AI")

    # ---- voice, into and out of the note
    def _on_mic(self, btn):
        """Dictate straight into the note. Continuous with pause detection:
        in an editor you talk in phrases, and each one lands at the cursor."""
        if self.rec.running:
            self.rec.stop()
            self.transcriber.cancel()      # or phrases keep landing after stop
            self.mic_btn.set_child(svg_icon("mic-on-white", 18))
            self.hint.set_label("")
            return
        dev = self.win.settings().mic_device()
        self.rec.start(dev, on_segment=self._on_segment, vad=True)
        self.mic_btn.set_child(svg_icon("stop-white", 16))
        self.hint.set_label("Ακούω… μίλα και γράφεται στον κέρσορα")

    def _on_segment(self, pcm):
        self.transcriber.submit(pcm, self.win.settings().dict_lang(),
                                self._dictated)

    def _dictated(self, text):
        text = (text or "").strip()
        if not text:
            return
        self.buf.insert_at_cursor(text + " ")
        predictor().learn(text)

    def _on_speak(self, btn):
        tts = self.win.app.tts
        if tts.speaking:                   # the button doubles as stop
            tts.stop()
            return
        sel = self.buf.get_selection_bounds()
        start, end = sel if sel else self.buf.get_bounds()
        text = self.buf.get_text(start, end, False).strip()
        if not text:
            self.hint.set_label("Δεν υπάρχει κείμενο για ανάγνωση")
            return
        st = self.win.settings()
        tts.speak(text, st.voice_model(), st.speed())

    def _on_correct(self, btn):
        """Fix the selection, or the whole note when nothing is selected."""
        sel = self.buf.get_selection_bounds()
        start, end = sel if sel else self.buf.get_bounds()
        text = self.buf.get_text(start, end, False)
        fixed, n = predictor().correct_text(text)
        if n:
            self.buf.delete(start, end)
            self.buf.insert(start, fixed)
        predictor().learn(fixed)
        self.hint.set_label(f"Διορθώθηκαν {n} λέξεις" if n
                            else "Καμία διόρθωση — όλα σωστά")
        self._show_chips() if self.suggestions else None


class SettingsPage(Gtk.Box):
    """Page: Ρυθμίσεις — grouped GNOME-style settings cards."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.append(page_header("Ρυθμίσεις"))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                       margin_top=16, margin_bottom=20,
                       margin_start=26, margin_end=26)

        # Γενικά
        gen = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.mini_toggle = Gtk.Switch(active=True)
        gen.append(settings_row(
            "Mini λειτουργία",
            "Όταν κλείνεις το παράθυρο, εμφανίζεται μια μικρή μπάρα "
            "(🎤 🔊 📋) πάνω από όλα", self.mini_toggle))
        self.mini_size_drop = dropdown([l for _, l in MINI_SIZE_LABELS])
        gen.append(settings_row(
            "Μέγεθος mini μπάρας",
            "Μεγάλο είναι η αρχική μπάρα, μικρό η λεπτή", self.mini_size_drop))
        self.top_toggle = Gtk.Switch()
        gen.append(settings_row(
            "Πάντα μπροστά",
            "Το παράθυρο μένει πάνω από τις άλλες εφαρμογές",
            self.top_toggle, divider=False))
        body.append(group("Γενικά", "Γενική συμπεριφορά της εφαρμογής", gen))

        # Υπαγόρευση
        dic = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        dic.append(settings_row("Στόχος (πού πάει το κείμενο)", None,
                                dropdown(["Ενεργό παράθυρο"])))
        self.lang_drop = dropdown(list(LANGS.keys()))
        dic.append(settings_row("Γλώσσα", None, self.lang_drop))
        self.stt_keys = list(vk.WHISPER_MODELS)
        self.stt_drop = dropdown([vk.WHISPER_MODELS[k][3]
                                  for k in self.stt_keys])
        self.stt_sub = dim(Gtk.Label(label="", xalign=0, wrap=True))
        stt_row = settings_row("Μοντέλο υπαγόρευσης (whisper)", None,
                               self.stt_drop)
        dic.append(stt_row)
        sub_box = Gtk.Box(margin_start=14, margin_end=12, margin_bottom=10)
        sub_box.append(self.stt_sub)
        dic.append(sub_box)
        self.deliver_toggle = Gtk.Switch(active=True)
        dic.append(settings_row("Παράδοση στην εφαρμογή", None,
                                self.deliver_toggle))
        self.cont_toggle = Gtk.Switch()
        dic.append(settings_row("Συνεχής λειτουργία (γράφει στις παύσεις)",
                                None, self.cont_toggle))
        dic.append(settings_row("Εμφάνιση πλαισίου κειμένου", None,
                                Gtk.Switch(), divider=False))
        ai = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.ai_status = dim(Gtk.Label(label="", xalign=0, wrap=True))
        self.ai_get = Gtk.Button()
        self.ai_get.connect("clicked", self._on_get_llm)
        ai.append(settings_row(
            "Μοντέλο διόρθωσης", "Τοπικό, offline — διορθώνει σύνταξη και "
            "νόημα στο Σημειωματάριο", self.ai_get, divider=False))
        sub_ai = Gtk.Box(margin_start=14, margin_end=12, margin_bottom=10)
        sub_ai.append(self.ai_status)
        ai.append(sub_ai)
        body.append(group("🧠  Διόρθωση AI",
                          "Το βαρύ μοντέλο που διαβάζει ολόκληρη πρόταση", ai))

        body.append(group("🎤  Υπαγόρευση",
                          "Πώς συμπεριφέρεται η υπαγόρευση φωνής", dic))

        # Ανάγνωση
        rd = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.voice_drop = dropdown(list(ra.VOICES.keys()))
        rd.append(settings_row("Φωνή ανάγνωσης (piper)", None,
                               self.voice_drop))
        self.speed_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.5, 2.0, 0.05)
        self.speed_scale.set_value(1.0)
        self.speed_scale.set_hexpand(True)
        self.speed_scale.set_size_request(300, -1)
        self.speed_val = dim(Gtk.Label(label="1.0×"), small=False)
        self.speed_scale.connect(
            "value-changed",
            lambda s: self.speed_val.set_label(f"{s.get_value():.1f}×"))
        rd.append(settings_row("Ρυθμός", None,
                               [self.speed_scale, self.speed_val]))
        rd.append(settings_row("Auto-ανάγνωση προχείρου", None,
                               Gtk.Switch(), divider=False))
        body.append(group("📖  Ανάγνωση",
                          "Προεπιλεγμένη φωνή και ταχύτητα για την ανάγνωση",
                          rd))

        # Σύσκεψη
        mt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.meet_src_drop = dropdown(["Ήχος συστήματος", "Μικρόφωνο"])
        mt.append(settings_row("Πηγή ήχου", None, self.meet_src_drop))
        self.meet_lang_drop = dropdown(list(LANGS.keys()))
        mt.append(settings_row("Γλώσσα", None, self.meet_lang_drop))
        exp_btn = Gtk.Button(label="Επιλογή…")
        exp_btn.connect("clicked", self._pick_export_dir)
        self._export_dir = vk.load_config().get("MEETING_DIR") or \
            os.path.expanduser("~")
        self.export_sub = dim(Gtk.Label(label=self._export_dir, xalign=0))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("settings-row")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                       hexpand=True, valign=Gtk.Align.CENTER)
        text.append(Gtk.Label(label="Φάκελος εξαγωγής", xalign=0))
        text.append(self.export_sub)
        row.append(text)
        exp_btn.set_valign(Gtk.Align.CENTER)
        row.append(exp_btn)
        mt.append(row)
        body.append(group("📝  Σύσκεψη",
                          "Πηγή ήχου, γλώσσα και φάκελος εξαγωγής", mt))

        # Μικρόφωνο
        mic = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                      margin_top=12, margin_bottom=12,
                      margin_start=14, margin_end=12)
        mics = list_mic_sources() or ["(καμία συσκευή)"]
        self.mic_drop = dropdown(mics)
        self.mic_drop.set_hexpand(True)
        mic.append(self.mic_drop)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.connect("clicked", self._refresh_mics)
        mic.append(refresh)
        body.append(group("🎙  Μικρόφωνο",
                          "Συσκευή εισόδου για υπαγόρευση και σύσκεψη", mic))

        # Λειτουργίες (feature toggles — hide pages everywhere, note_1)
        feat = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.feature_toggles = {}
        feats = [("dictation", "🎤  Υπαγόρευση"), ("read", "📖  Ανάγνωση"),
                 ("meeting", "📝  Σύσκεψη"), ("notepad", "📝  Σημειωματάριο"),
                 ("clipboard", "📋  Πρόχειρο")]
        for i, (key, label) in enumerate(feats):
            sw = Gtk.Switch(active=True)
            sw.connect("notify::active",
                       lambda s, _, k=key: self.win.set_feature(
                           k, s.get_active()))
            self.feature_toggles[key] = sw
            feat.append(settings_row(label, None, sw,
                                     divider=(i < len(feats) - 1)))
        body.append(group("🧩  Λειτουργίες",
                          "Ενεργοποίησε ή απενεργοποίησε καρτέλες", feat))

        # Συντομεύσεις
        sc = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        cfg = vk.load_config()
        self.sc_chips = {}
        for i, (ckey, label, action, default) in enumerate(SHORTCUTS):
            combo = cfg.get(ckey, default)
            chip = Gtk.Label(label=combo or "—")
            chip.add_css_class("kbd-chip")
            self.sc_chips[ckey] = chip
            btn = Gtk.Button(label="Όρισε")
            btn.connect(
                "clicked",
                lambda b, k=ckey, l=label, c=combo: ShortcutDialog(
                    self.win, l, self.sc_chips[k].get_label(),
                    on_save=lambda combo, k=k: self._set_shortcut(k, combo)
                ).present())
            sc.append(settings_row(label, None, [chip, btn],
                                   divider=(i < len(SHORTCUTS) - 1)))
        body.append(group(
            "⌨  Συντομεύσεις",
            "Καθολικές — δουλεύουν παντού. Πάτα «Όρισε» και τον συνδυασμό.",
            sc))

        self.append(scrolled(body))
        self._load_cfg()

    # ---- persistence
    def _load_cfg(self):
        cfg = vk.load_config()

        def sel(drop, value, values):
            if value in values:
                drop.set_selected(values.index(value))
        self.mini_toggle.set_active(cfg.get("MINI", "1") == "1")
        keys = [k for k, _ in MINI_SIZE_LABELS]
        self.mini_size_drop.set_selected(
            keys.index(mini_size()) if mini_size() in keys else 1)
        self.mini_size_drop.connect("notify::selected",
                                    lambda *_: self._mini_size_changed())
        self.top_toggle.set_active(cfg.get("TOPMOST", "0") == "1")
        self.deliver_toggle.set_active(cfg.get("DELIVER", "1") == "1")
        sel(self.lang_drop, cfg.get("LANG", "el"), list(LANGS.values()))
        sel(self.meet_lang_drop, cfg.get("MEET_LANG", "el"),
            list(LANGS.values()))
        sel(self.voice_drop, cfg.get("VOICE", ""), list(ra.VOICES.keys()))
        self.meet_src_drop.set_selected(int(cfg.get("MEET_SRC", "0") or 0))
        # the switch on the dictation card is the single source of truth —
        # the settings one was a mockup leftover wired to nothing
        self.cont_toggle.set_active(cfg.get("CONT", "0") == "1")
        self.cont_toggle.bind_property(
            "active", self.win.pages["dictation"].mode_toggle, "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self._stt_key = cfg.get("STT_MODEL", "small")
        if self._stt_key not in self.stt_keys:
            self._stt_key = "small"
        self.stt_drop.set_selected(self.stt_keys.index(self._stt_key))
        self._stt_describe()
        self.stt_drop.connect("notify::selected", self._stt_changed)
        self._llm_describe()
        try:
            self.speed_scale.set_value(float(cfg.get("SPEED", "1.0")))
        except ValueError:
            pass
        model = self.mic_drop.get_model()
        if isinstance(model, Gtk.StringList):
            names = [model.get_string(i)
                     for i in range(model.get_n_items())]
            sel(self.mic_drop, cfg.get("DEVICE", ""), names)
        for key, sw in self.feature_toggles.items():
            on = cfg.get(f"FEAT_{key.upper()}", "1") == "1"
            if not on:
                sw.set_active(False)

        # persist on any change (after initial values are in place)
        for w in (self.mini_toggle, self.top_toggle, self.deliver_toggle,
                  self.cont_toggle, *self.feature_toggles.values()):
            w.connect("notify::active", lambda *_: self._persist())
        for d in (self.lang_drop, self.meet_lang_drop, self.voice_drop,
                  self.meet_src_drop, self.mic_drop):
            d.connect("notify::selected", lambda *_: self._persist())
        self.speed_scale.connect("value-changed",
                                 lambda *_: self._persist())
        self.top_toggle.connect("notify::active",
                                lambda *_: self.win.apply_topmost())

    def _mini_size_changed(self):
        """Persist, then rebuild the bar — size is baked in at construction."""
        cfg = vk.load_config()
        cfg["MINI_SIZE"] = MINI_SIZE_LABELS[
            self.mini_size_drop.get_selected()][0]
        vk.save_config(cfg)
        app = self.win.app
        if app.mini is not None and app.mini.get_visible():
            app.mini.close()
            app.mini = None
            app.show_mini()

    def _persist(self):
        cfg = vk.load_config()
        cfg.update(
            MINI="1" if self.mini_toggle.get_active() else "0",
            TOPMOST="1" if self.top_toggle.get_active() else "0",
            DELIVER="1" if self.deliver_toggle.get_active() else "0",
            CONT="1" if self.cont_toggle.get_active() else "0",
            LANG=self.dict_lang(), MEET_LANG=self.meet_lang(),
            VOICE=list(ra.VOICES.keys())[self.voice_drop.get_selected()],
            MEET_SRC=str(self.meet_src_drop.get_selected()),
            SPEED=f"{self.speed_scale.get_value():.2f}",
            DEVICE=self.mic_device() or "",
        )
        for key, sw in self.feature_toggles.items():
            cfg[f"FEAT_{key.upper()}"] = "1" if sw.get_active() else "0"
        vk.save_config(cfg)

    # ---- speech model (download + RAM gating)
    def _stt_describe(self):
        key = self._stt_key
        _, dl_gb, need, _, note = vk.WHISPER_MODELS[key]
        status, msg = vk.model_ram_check(key)
        if msg:
            self.stt_sub.set_label(("⚠  " if status == "warn" else "⛔  ") + msg)
        else:
            self.stt_sub.set_label(
                f"{note} · ~{need:.1f} GB RAM · λήψη {dl_gb:.1f} GB"
                + ("" if vk.model_installed(key) else " (δεν έχει ληφθεί)"))

    def _stt_revert(self):
        self.stt_drop.set_selected(self.stt_keys.index(self._stt_key))

    def _stt_accept(self, key):
        self._stt_key = key
        cfg = vk.load_config()
        cfg["STT_MODEL"] = key
        vk.save_config(cfg)
        self._stt_describe()

    def _stt_changed(self, *_):
        i = self.stt_drop.get_selected()
        if i == Gtk.INVALID_LIST_POSITION:
            return
        key = self.stt_keys[i]
        if key == self._stt_key:
            return
        status, msg = vk.model_ram_check(key)
        if status == "block":
            self._stt_revert()
            InfoDialog(self.win, "Δεν υπάρχει αρκετή μνήμη", msg).present()
            return
        if not vk.model_installed(key):
            self._stt_revert()
            ModelDownloadDialog(self.win, key, msg,
                                on_done=lambda: self._stt_accept(key)).present()
            return
        self._stt_accept(key)

    # ---- correction model
    def _llm_describe(self):
        _, dl_gb, need, label, note, _ = llm.LLM_MODELS[llm.DEFAULT_MODEL]
        have = llm.installed()
        self.ai_get.set_label("Ληφθέν ✓" if have else "Λήψη…")
        self.ai_get.set_sensitive(not have)
        status, msg = llm.ram_check()
        if not llm.binary():
            self.ai_status.set_label(
                "Δεν βρέθηκε το llama.cpp σε αυτή την εγκατάσταση.")
        elif msg:
            self.ai_status.set_label(
                ("⚠  " if status == "warn" else "⛔  ") + msg)
        else:
            self.ai_status.set_label(
                f"{label} · {note} · λήψη {dl_gb:.1f} GB · ~{need:.1f} GB RAM"
                + ("" if have else " (δεν έχει ληφθεί)"))

    def _on_get_llm(self, btn):
        key = llm.DEFAULT_MODEL
        _, dl_gb, need, label, note, url = llm.LLM_MODELS[key]
        status, msg = llm.ram_check()
        if status == "block":
            InfoDialog(self.win, "Δεν υπάρχει αρκετή μνήμη", msg).present()
            return
        ModelDownloadDialog(self.win, key, msg, on_done=self._llm_describe,
                            url=url, dest=llm.model_path(key),
                            spec=(dl_gb, need, label, note)).present()

    def _set_shortcut(self, key, combo):
        self.sc_chips[key].set_label(combo or "—")
        cfg = vk.load_config()
        cfg[key] = combo
        vk.save_config(cfg)
        threading.Thread(target=register_shortcuts, args=(cfg,),
                         daemon=True).start()

    def mini_enabled(self):
        return self.mini_toggle.get_active()

    def _refresh_mics(self, btn):
        mics = list_mic_sources() or ["(καμία συσκευή)"]
        self.mic_drop.set_model(Gtk.StringList.new(mics))

    def _pick_export_dir(self, btn):
        d = Gtk.FileDialog(title="Φάκελος εξαγωγής")
        d.select_folder(self.win, None, self._picked_dir)

    def _picked_dir(self, dialog, result):
        try:
            f = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        self._export_dir = f.get_path()
        self.export_sub.set_label(self._export_dir)
        cfg = vk.load_config()
        cfg["MEETING_DIR"] = self._export_dir
        vk.save_config(cfg)

    # ---- accessors used by the pages
    def mic_device(self):
        model = self.mic_drop.get_model()
        i = self.mic_drop.get_selected()
        if isinstance(model, Gtk.StringList) and i != Gtk.INVALID_LIST_POSITION:
            name = model.get_string(i)
            if name and not name.startswith("("):
                return name
        return None

    def meeting_device(self):
        if self.meet_src_drop.get_selected() == 0:      # system audio
            return vk.default_sink_monitor() or None
        return self.mic_device()

    def dict_lang(self):
        return list(LANGS.values())[self.lang_drop.get_selected()]

    def meet_lang(self):
        return list(LANGS.values())[self.meet_lang_drop.get_selected()]

    def voice_model(self):
        return list(ra.VOICES.values())[self.voice_drop.get_selected()]

    def speed(self):
        return self.speed_scale.get_value()

    def deliver_enabled(self):
        return self.deliver_toggle.get_active()

    def export_dir(self):
        return self._export_dir


# ---------------------------------------------------------------- dialogs
class VoiceDialog(Gtk.Window):
    """Base modal dialog: title + body + trailing action bar."""

    def __init__(self, parent, title, width=440):
        super().__init__(transient_for=parent, modal=True,
                         resizable=False, title=title)
        self.set_default_size(width, -1)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                           margin_top=22, margin_bottom=16,
                           margin_start=22, margin_end=22)
        t = Gtk.Label(label=title, xalign=0)
        t.add_css_class("title-3")
        self.box.append(t)
        self.set_child(self.box)

    def action_bar(self, *buttons, leading=None):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                      margin_top=8)
        if leading is not None:
            bar.append(leading)
        bar.append(Gtk.Box(hexpand=True))
        for b in buttons:
            bar.append(b)
        self.box.append(bar)


class TargetDialog(VoiceDialog):
    """Παράδοση κειμένου — target app + delivery toggles."""

    def __init__(self, parent):
        super().__init__(parent, "Παράδοση κειμένου", 460)
        self.box.append(dim(Gtk.Label(
            label="Πού και πώς παραδίδεται το κείμενο που υπαγορεύεις",
            xalign=0), small=False))

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.append(Gtk.Label(label="Εφαρμογή-στόχος", xalign=0, hexpand=True))
        row.append(dropdown(["Ενεργό παράθυρο (focus)"]))
        self.box.append(row)

        for label, active in [("Αυτόματη επικόλληση στην εφαρμογή", True),
                              ("Πάτημα Enter μετά την παράδοση", True),
                              ("Επιστροφή εστίασης στο VOICE", True)]:
            r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            r.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            r.append(Gtk.Switch(active=active, valign=Gtk.Align.CENTER))
            self.box.append(r)

        cancel = Gtk.Button(label="Άκυρο")
        cancel.connect("clicked", lambda *_: self.close())
        ok = Gtk.Button(label="Εντάξει")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", lambda *_: self.close())
        self.action_bar(cancel, ok)


class InfoDialog(VoiceDialog):
    """One message + Εντάξει. Used for the RAM refusal."""

    def __init__(self, parent, title, message):
        super().__init__(parent, title, 460)
        self.box.append(dim(Gtk.Label(label=message, xalign=0, wrap=True),
                            small=False))
        ok = Gtk.Button(label="Εντάξει")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", lambda *_: self.close())
        self.action_bar(ok)


class ModelDownloadDialog(VoiceDialog):
    """Confirm + download a whisper model, with the RAM warning up front.

    The worker thread never touches widgets — it hands progress to the GTK
    main loop via GLib.idle_add (same rule as the Tkinter first-run dialog)."""

    def __init__(self, parent, key, warning, on_done=None, url=None,
                 dest=None, spec=None):
        dl_gb, need, label, note = spec or vk.WHISPER_MODELS[key][1:]
        super().__init__(parent, "Λήψη μοντέλου", 470)
        self.key, self.on_done = key, on_done
        self.url = url or f"{vk.HF_WHISPER}/{vk.WHISPER_MODELS[key][0]}"
        self.dest = dest or vk.model_path(key)

        self.box.append(dim(Gtk.Label(
            label=f"{label} — {note}\nΛήψη ~{dl_gb:.1f} GB · χρειάζεται "
                  f"~{need:.1f} GB RAM στη χρήση.",
            xalign=0, wrap=True), small=False))
        if warning:
            w = Gtk.Label(label="⚠  " + warning, xalign=0, wrap=True)
            w.add_css_class("accent-text")
            self.box.append(w)

        self.status = dim(Gtk.Label(label="", xalign=0), small=False)
        self.bar = Gtk.ProgressBar(hexpand=True)
        self.bar.set_visible(False)
        self.box.append(self.status)
        self.box.append(self.bar)

        self.cancel = Gtk.Button(label="Άκυρο")
        self.cancel.connect("clicked", lambda *_: self.close())
        self.go = Gtk.Button(label="Λήψη")
        self.go.add_css_class("suggested-action")
        self.go.connect("clicked", self._start)
        self.action_bar(self.cancel, self.go)

    def _start(self, btn):
        self.go.set_sensitive(False)
        self.cancel.set_sensitive(False)
        self.bar.set_visible(True)
        self.status.set_label("Σύνδεση…")
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        dest = self.dest
        try:
            vk._download(self.url, dest,
                         lambda done, total: GLib.idle_add(
                             self._progress, done, total))
        except Exception as e:                      # network / disk / timeout
            try:
                os.remove(dest + ".part")
            except OSError:
                pass
            GLib.idle_add(self._failed, str(e))
            return
        GLib.idle_add(self._finished)

    def _progress(self, done, total):
        self.bar.set_fraction(done / total if total else 0.0)
        self.status.set_label(f"{done / 1e9:0.2f} GB"
                              + (f" / {total / 1e9:0.2f} GB" if total else ""))

    def _failed(self, err):
        self.bar.set_visible(False)
        self.status.set_label(f"Η λήψη απέτυχε: {err}")
        self.cancel.set_label("Κλείσιμο")
        self.cancel.set_sensitive(True)
        self.go.set_sensitive(True)

    def _finished(self):
        if self.on_done:
            self.on_done()
        self.close()


class ShortcutDialog(VoiceDialog):
    """Ορισμός συντόμευσης — dashed accent capture box; records the next
    key combination pressed and hands it to on_save."""

    _MODS = [(Gdk.ModifierType.SUPER_MASK, "Meta"),
             (Gdk.ModifierType.CONTROL_MASK, "Ctrl"),
             (Gdk.ModifierType.ALT_MASK, "Alt"),
             (Gdk.ModifierType.SHIFT_MASK, "Shift")]

    def __init__(self, parent, what, current, on_save=None):
        super().__init__(parent, "Ορισμός συντόμευσης", 440)
        self.on_save = on_save
        self.combo = current
        self.box.append(dim(Gtk.Label(label=what, xalign=0), small=False))

        cap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        cap.add_css_class("shortcut-capture")
        self.key_lbl = Gtk.Label(label=current or "—",
                                 halign=Gtk.Align.CENTER)
        self.key_lbl.add_css_class("shortcut-key")
        cap.append(self.key_lbl)
        hint = Gtk.Label(label="Πάτησε τον νέο συνδυασμό πλήκτρων…",
                         halign=Gtk.Align.CENTER)
        hint.add_css_class("accent-text")
        hint.add_css_class("small-label")
        cap.append(hint)
        self.box.append(cap)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        reset = Gtk.Button(label="Καθαρισμός")
        reset.connect("clicked", self._reset)
        cancel = Gtk.Button(label="Άκυρο")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Αποθήκευση")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        self.action_bar(cancel, save, leading=reset)

    def _on_key(self, ctl, keyval, keycode, state):
        name = Gdk.keyval_name(keyval) or ""
        if name in ("Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L",
                    "Shift_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
                    "ISO_Level3_Shift"):
            return False
        mods = [label for mask, label in self._MODS if state & mask]
        key = name.upper() if len(name) == 1 else name
        self.combo = "+".join(mods + [key])
        self.key_lbl.set_label(self.combo)
        return True

    def _reset(self, btn):
        self.combo = ""
        self.key_lbl.set_label("—")

    def _save(self, btn):
        if self.on_save:
            self.on_save(self.combo)
        self.close()


class ConfirmClearDialog(VoiceDialog):
    """Καθαρισμός προχείρου; — destructive confirm."""

    def __init__(self, parent):
        super().__init__(parent, "Καθαρισμός προχείρου;", 420)
        self.box.append(dim(Gtk.Label(
            label="Θα διαγραφούν τα στοιχεία από το ιστορικό. "
                  "Τα καρφιτσωμένα στοιχεία διατηρούνται.",
            xalign=0, wrap=True), small=False))
        cancel = Gtk.Button(label="Άκυρο")
        cancel.connect("clicked", lambda *_: self.close())
        do = Gtk.Button(label="Καθαρισμός")
        do.add_css_class("destructive-action")
        do.connect("clicked", self._do_clear)
        self.action_bar(cancel, do)

    def _do_clear(self, btn):
        self.get_transient_for().app.store.clear()
        self.close()


def show_about(parent):
    """Περί VOICE — native AboutDialog with the real app icon (note_4)."""
    about = Gtk.AboutDialog(
        transient_for=parent, modal=True,
        program_name="VOICE",
        # no bare "&" here: AboutDialog parses this as Pango markup, and a
        # markup error makes GTK silently fall back to "version 1.0"
        version=f"v{VERSION} · Φωνή, τοπικά, offline",
        comments="Ελληνική υπαγόρευση, ανάγνωση, μεταγραφή συσκέψεων και "
                 "πρόχειρο — όλα στη συσκευή σου.",
        website="https://github.com/kalotrapezis",
        website_label="github.com/kalotrapezis",
        license_type=Gtk.License.MIT_X11)
    if os.path.exists(ICON):
        about.set_logo(Gdk.Texture.new_from_filename(ICON))
    about.present()


# ---------------------------------------------------------------- mini bar
class MiniWindow(Gtk.Window):
    """VOICE Mini — undecorated pill bar; clip button expands the inline
    clipboard list (design note_5: one window, two states)."""

    def __init__(self, app, main_win):
        super().__init__(application=app, decorated=False, resizable=False,
                         title="VOICE Mini")
        self.set_icon_name("voice")
        self.main_win = main_win
        self.add_css_class("voice-mini")
        self.size_key = mini_size()
        self.add_css_class("size-" + self.size_key)
        ico, pad, gap = MINI_SIZES[self.size_key]
        self.expanded = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=gap - 2,
                        margin_top=pad, margin_bottom=pad,
                        margin_start=pad + 4, margin_end=pad + 2)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=gap)
        handle = Gtk.Label(label="⠿")
        dim(handle, small=False)
        handle.add_css_class("mini-handle")
        bar.append(handle)

        self.mic_btn = svg_button("mic-on-white", ico,
                                  ("destructive-action", "mini-round"))
        self.mic_btn.connect("clicked", self._mini_mic)
        bar.append(self.mic_btn)

        self.speak_btn = svg_button(icon_for("speaker"), ico, ("mini-round",))
        self.speak_btn.connect("clicked", self._mini_speak)
        bar.append(self.speak_btn)

        self.clip_btn = svg_button(icon_for("clip"), ico, ("mini-round",))
        self.clip_btn.connect("clicked", self._toggle_clip)
        bar.append(self.clip_btn)

        self.status = dim(Gtk.Label(label="Έτοιμο"), small=False)
        bar.append(self.status)
        bar.append(Gtk.Box(hexpand=True))

        restore = Gtk.Button(icon_name="view-restore-symbolic")
        restore.add_css_class("flat")
        restore.connect("clicked", self._restore)
        bar.append(restore)
        close = Gtk.Button(icon_name="window-close-symbolic")
        close.add_css_class("flat")
        close.connect("clicked", lambda *_: self.close())
        bar.append(close)
        outer.append(bar)

        # expandable clipboard list
        self.clip_rev = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        clip_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.clip_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 spacing=6)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                       margin_top=8, margin_bottom=8,
                       margin_start=8, margin_end=8)
        card.append(self.clip_list)
        wrap = Gtk.Box()
        wrap.add_css_class("voice-card")
        wrap.append(card)
        clip_box.append(wrap)
        clip_box.append(dim(Gtk.Label(
            label="Κλικ για επανα-αντιγραφή · 📌 καρφίτσωμα")))
        self.clip_rev.set_child(clip_box)
        outer.append(self.clip_rev)

        # drag the ⠿ handle to move the window — the handle only, or the
        # gesture's begin_move() steals the pointer grab and every button on
        # the bar (including ✕) drags instead of clicking
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        handle.add_controller(drag)
        handle.set_cursor(Gdk.Cursor.new_from_name("grab"))

        self.set_child(outer)

    def refresh_clips(self):
        self.set_clip_items(self.main_win.app.store.ordered()[:6])
        return False

    def apply_features(self):
        """Hide buttons of disabled features (design note: everywhere)."""
        f = self.main_win.settings().feature_toggles
        self.mic_btn.set_visible(f["dictation"].get_active())
        self.speak_btn.set_visible(f["read"].get_active())
        clip_on = f["clipboard"].get_active()
        self.clip_btn.set_visible(clip_on)
        if not clip_on and self.expanded:
            self._toggle_clip(None)

    def set_clip_items(self, items):
        while (c := self.clip_list.get_first_child()):
            self.clip_list.remove(c)
        for it in items:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("mini-clip-row")
            prefix = "🖼 " if it.get("kind") == "image" else ""
            # single-line preview: flatten newlines (a copied transcript is
            # one item, not a wall), and max_width_chars caps the label's
            # NATURAL width — without it an ellipsized label still asks for
            # the full text width and the mini window balloons to fit
            flat = " ".join(it.get("text", "").split())
            lbl = Gtk.Label(label=prefix + flat, xalign=0,
                            hexpand=True, max_width_chars=38,
                            ellipsize=Pango.EllipsizeMode.END)
            if it.get("text", "").startswith(("http://", "https://")):
                lbl.add_css_class("link-text")
            row.append(lbl)
            click = Gtk.GestureClick()
            click.connect("released", lambda *_, i=it: clipmod.recopy(i))
            row.add_controller(click)
            pin = svg_button(icon_for("pin"), 14, ("flat",))
            if it.get("pinned"):
                pin.add_css_class("suggested-action")
            pin.connect("clicked",
                        lambda *_, i=it: self.main_win.app.store.set_pinned(
                            i["id"], not i.get("pinned", False)))
            row.append(pin)
            self.clip_list.append(row)

    def _mini_mic(self, btn):
        page = self.main_win.pages["dictation"]
        page._on_record(btn)
        rec = page.rec.running
        self.mic_btn.set_child(svg_icon(
            "stop-white" if rec else "mic-on-white",
            MINI_SIZES[self.size_key][0]))
        self.status.set_label("Εγγραφή…" if rec else "Έτοιμο")

    def _mini_speak(self, btn):
        tts = self.main_win.app.tts
        if tts.speaking:                 # second click = stop
            tts.stop()
            return
        st = self.main_win.settings()
        text = platform_io.clip_read(primary=True) or platform_io.clip_read()
        tts.speak(text, st.voice_model(), st.speed(), self._speak_state)

    def _speak_state(self, state):
        playing = state == "playing"
        self.speak_btn.set_child(svg_icon(
            icon_for("stop") if playing else icon_for("speaker"),
            MINI_SIZES[self.size_key][0]))
        self.status.set_label("Διαβάζω…" if playing else "Έτοιμο")
        return False

    def _toggle_clip(self, btn):
        self.expanded = not self.expanded
        self.clip_rev.set_reveal_child(self.expanded)
        if self.expanded:
            self.add_css_class("expanded")
        else:
            self.remove_css_class("expanded")

    def _restore(self, btn):
        self.close()
        self.main_win.set_visible(True)
        self.main_win.present()

    def _drag_begin(self, gesture, x, y):
        # Native move via the compositor (works on Wayland and X11).
        surface = self.get_surface()
        if hasattr(surface, "begin_move"):
            dev = gesture.get_device()
            surface.begin_move(dev, 1, x, y, Gdk.CURRENT_TIME)


# ---------------------------------------------------------------- main win
class MainWindow(Gtk.ApplicationWindow):
    PAGES = [("dictation", "Υπαγόρευση", "mic"),
             ("read", "Ανάγνωση", "speaker"),
             ("meeting", "Σύσκεψη", "meeting"),
             ("notepad", "Σημειωματάριο", "notepad"),
             ("clipboard", "Πρόχειρο", "clip")]

    def __init__(self, app):
        super().__init__(application=app, title="VOICE",
                         default_width=1200, default_height=760)
        self.set_icon_name("voice")     # X11 titlebar/taskbar icon
        self.add_css_class("voice-main")
        self.app = app

        # header bar with hamburger menu
        header = Gtk.HeaderBar()
        menu = Gio.Menu()
        menu.append("Mini λειτουργία", "app.mini")
        menu.append("Ρυθμίσεις", "app.settings")
        sec = Gio.Menu()
        sec.append("Περί VOICE", "app.about")
        sec.append("Έξοδος", "app.quit")
        menu.append_section(None, sec)
        burger = Gtk.MenuButton(icon_name="open-menu-symbolic",
                                menu_model=menu)
        header.pack_end(burger)
        self.set_titlebar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        # --- sidebar
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                       width_request=236)
        side.add_css_class("sidebar-pane")

        brand = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                        margin_top=18, margin_bottom=14,
                        margin_start=18, margin_end=18)
        bt = Gtk.Label(label="VOICE", xalign=0)
        bt.add_css_class("brand-title")
        brand.append(bt)
        brand.append(dim(Gtk.Label(label="Φωνή · τοπικά, offline", xalign=0)))
        side.append(brand)

        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.nav.add_css_class("navigation-sidebar")
        self.nav_rows = {}
        for key, label, icon in self.PAGES:
            self.nav.append(self._nav_row(key, label, icon))
        self.nav.connect("row-selected", self._on_nav)
        side.append(self.nav)

        side.append(Gtk.Box(vexpand=True))

        ver = dim(Gtk.Label(label=f"v{VERSION}", xalign=0,
                            margin_start=22, margin_bottom=10, margin_top=8))
        side.append(ver)
        body.append(side)

        # --- page stack
        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        GLib.timeout_add(500, lambda: self.stack.set_transition_type(
            Gtk.StackTransitionType.CROSSFADE) or False)
        # built one at a time, not as a dict literal: SettingsPage reaches for
        # self.pages["dictation"] while constructing, and a literal would only
        # bind self.pages after every page was already built
        self.pages = {}
        for key, cls in (("dictation", DictationPage), ("read", ReadPage),
                         ("meeting", MeetingPage), ("notepad", NotepadPage),
                         ("clipboard", ClipboardPage),
                         ("settings", SettingsPage)):
            self.pages[key] = cls(self)
        for key, page in self.pages.items():
            self.stack.add_named(page, key)
        body.append(self.stack)

        self.set_child(body)
        self.nav.select_row(self.nav.get_row_at_index(0))

        # live clipboard history
        self.pages["clipboard"].refresh()
        self.app.store.add_listener(
            lambda: GLib.idle_add(self._clips_changed))

        # close → mini bar (when enabled in settings)
        self.connect("close-request", self._on_close)
        GLib.idle_add(self.apply_topmost)

    def _on_close(self, *args):
        if self.settings().mini_enabled():
            self.set_visible(False)
            self.app.show_mini()
            return True          # keep the app alive
        return False

    def apply_topmost(self):
        """Keep-above for the main window. GTK 4 has no API for it: X11 →
        wmctrl, Plasma → a KWin rule we write ourselves (note: on Wayland the
        client cannot ask, so without the rule the switch did nothing)."""
        on = self.settings().top_toggle.get_active()
        if os.environ.get("XDG_SESSION_TYPE") == "x11":
            subprocess.run(["wmctrl", "-r", "VOICE", "-b",
                            f"{'add' if on else 'remove'},above"],
                           capture_output=True)
        else:
            kwin_keep_above("voice-main-above", on)
        return False

    def settings(self):
        return self.pages["settings"]

    def _clips_changed(self):
        self.pages["clipboard"].refresh()
        if self.app.mini is not None:
            self.app.mini.refresh_clips()
        return False

        start = os.environ.get("VOICE_PAGE")
        if start in self.pages:
            # after map: initial keyboard focus auto-selects the first
            # sidebar row, which would override an immediate switch
            def _go():
                idx = [k for k, *_ in self.PAGES]
                if start in idx:
                    self.nav.select_row(self.nav.get_row_at_index(
                        idx.index(start)))
                else:
                    self.show_page(start)
                    # keep initial focus off the sidebar, whose
                    # select-on-focus would override the page
                    self.pages[start].child_focus(Gtk.DirectionType.DOWN)
                return False
            GLib.timeout_add(200, _go)

    def _nav_row(self, key, label, icon):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        role = "gear" if icon == "settings-gear" else icon
        if role in _ICONS:
            box.append(svg_icon(icon_for(role), 18))
        else:                              # no SVG drawn for it yet
            img = Gtk.Image.new_from_icon_name("document-edit-symbolic")
            img.set_pixel_size(18)
            box.append(img)
        box.append(Gtk.Label(label=label, xalign=0))
        row.set_child(box)
        row.page_key = key
        self.nav_rows[key] = row
        return row

    def _on_nav(self, listbox, row):
        if row is not None:
            self.stack.set_visible_child_name(row.page_key)

    def show_page(self, key):
        if key in ("settings",):
            self.nav.unselect_all()
        self.stack.set_visible_child_name(key)

    def set_feature(self, key, enabled):
        """Feature toggle: hide the page everywhere (design note_1)."""
        row = self.nav_rows.get(key)
        if row is not None:
            row.set_visible(enabled)
        if not enabled and self.stack.get_visible_child_name() == key:
            self.show_page("settings")
        # the feature disappears everywhere (design note): mini bar too
        if self.app.mini is not None:
            self.app.mini.apply_features()


class VoiceApp(Gtk.Application):
    def __init__(self):
        # Single-instance is load-bearing: with close-to-mini the app keeps
        # running after its window closes, and a NON_UNIQUE second launch
        # would run a second recorder + whisper + delivery pipeline on the
        # same mic — every phrase typed twice, and "stop" in one instance
        # leaves the other pasting into windows forever.
        super().__init__(application_id="io.github.kalotrapezis.voice")
        self.mini = None

    def do_shutdown(self):
        """Stop every capture/playback process so nothing outlives the app —
        an orphaned recorder kept transcribing and pasting after exit."""
        for w in self.get_windows():
            if isinstance(w, MainWindow):
                for key in ("dictation", "meeting"):
                    page = w.pages.get(key)
                    try:
                        if page and page.rec.running:
                            page.rec.stop()
                            page.transcriber.cancel()
                    except Exception:
                        pass
        try:
            self.tts.stop()
        except Exception:
            pass
        Gtk.Application.do_shutdown(self)

    def do_activate(self):
        _load_css()
        win = self.props.active_window
        if not win:
            ensure_dev_icon()
            self.tts = Tts()
            self.store = clipmod.ClipStore()
            self.watcher = GdkClipWatcher(self.store)
            self._start_ctl_server()
            # keep the desktop's global shortcuts pointing at this copy
            cfg = vk.load_config()
            if any(cfg.get(k) for k, *_ in SHORTCUTS):
                threading.Thread(target=register_shortcuts, args=(cfg,),
                                 daemon=True).start()
            win = MainWindow(self)
            for name, cb in [("mini", self.show_mini),
                             ("settings", lambda *_: win.show_page("settings")),
                             ("about", lambda *_: show_about(win)),
                             ("quit", lambda *_: self.quit())]:
                act = Gio.SimpleAction.new(name, None)
                act.connect("activate", cb)
                self.add_action(act)
        win.present()

    # ---- global-shortcut control socket (same protocol as the Tk app)
    def _start_ctl_server(self):
        import socket as _socket
        try:
            os.unlink(vk.CTL_SOCK)
        except OSError:
            pass
        try:
            self._ctl = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
            self._ctl.bind(vk.CTL_SOCK)
        except OSError:
            return
        threading.Thread(target=self._ctl_loop, daemon=True).start()

    def _ctl_loop(self):
        while True:
            try:
                data, _ = self._ctl.recvfrom(64)
            except OSError:
                break
            cmd = data.decode("utf-8", "ignore").strip()
            GLib.idle_add(self._ctl_dispatch, cmd)

    def _ctl_dispatch(self, cmd):
        LOG.info("ctl.command %r", cmd)
        win = self.props.active_window or (
            self.get_windows()[0] if self.get_windows() else None)
        main = win if isinstance(win, MainWindow) else None
        if main is None:
            for w in self.get_windows():
                if isinstance(w, MainWindow):
                    main = w
        if main is None:
            return False
        if cmd == "talk":
            main.pages["dictation"]._on_record(None)
        elif cmd == "read":
            st = main.settings()
            text = platform_io.clip_read(primary=True) or \
                platform_io.clip_read()
            self.tts.speak(text, st.voice_model(), st.speed())
        elif cmd == "show":
            main.set_visible(True)
            main.present()
        elif cmd == "clip":
            main.set_visible(True)
            main.present()
            main.show_page("clipboard")
            main.nav.select_row(main.nav_rows["clipboard"])
        elif cmd == "clipmini":
            self.show_mini(expand_clip=True)
        return False

    def show_mini(self, *args, expand_clip=False):
        win = None
        for w in self.get_windows():
            if isinstance(w, MainWindow):
                win = w
        if self.mini is None or not self.mini.get_visible():
            self.mini = MiniWindow(self, win)
            self.mini.refresh_clips()
            self.mini.apply_features()
        if expand_clip and not self.mini.expanded:
            self.mini._toggle_clip(None)
        self.mini.present()
        # The mini bar floats over everything by definition — no setting.
        GLib.timeout_add(300, self._keep_mini_above)

    def _keep_mini_above(self):
        if os.environ.get("XDG_SESSION_TYPE") == "x11":
            subprocess.run(["wmctrl", "-r", "VOICE Mini", "-b",
                            "add,above"], capture_output=True)
        else:
            kwin_keep_above("voice-mini-above", True)
        return False


if __name__ == "__main__":
    _setup_log()
    LOG.info("main pid=%d argv=%r", os.getpid(), sys.argv[1:])
    # `voice_gtk.py talk|read|show|clip|clipmini` → forward to the running
    # instance (this is what the registered global shortcuts execute)
    if len(sys.argv) > 1 and sys.argv[1] in (
            "talk", "read", "show", "clip", "clipmini"):
        if send_ctl(sys.argv[1]):
            sys.exit(0)
        # no instance running: start one; it will pick up "show"-like intent
        cmd = sys.argv.pop(1)
        app = VoiceApp()
        GLib.timeout_add(1500, lambda: (send_ctl(cmd), False)[1])
        sys.exit(app.run(sys.argv))
    app = VoiceApp()
    sys.exit(app.run(sys.argv))
