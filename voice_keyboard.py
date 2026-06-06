#!/usr/bin/env python3
"""
Φωνητικό Πληκτρολόγιο — Greek voice keyboard / voice remote for KDE Wayland.

Pick a microphone and a target app, then talk. In continuous mode it transcribes
on each pause and delivers the text straight into the chosen app window
(activate + clipboard paste, so Greek works reliably). Has an always-on-top
mini mode so it can float as a small mic over whatever you're doing.

Engines: whisper.cpp (local CPU build) + ydotool (paste) + KWin WindowsRunner
(window activation over D-Bus). Pure standard-library Tkinter — no pip deps.
"""
import os
import re
import sys
import time
import math
import wave
import array
import queue
import signal
import socket
import subprocess
import threading
import urllib.request
import tkinter as tk
from tkinter import ttk
from read_aloud import Reader, voice_data_dir, asset_dir, VOICES_DIR

CTL_SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "voice.sock")

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "Assets", "VoiceIcon.png")
MODELS_DIR = asset_dir("models")
MODEL = os.environ.get("WHISPER_MODEL") or os.path.join(MODELS_DIR, "ggml-small.bin")
WHISPER = os.path.join(HERE, "whisper.cpp", "build", "bin", "whisper-cli")

# first-run downloads (lean package: models fetched once into voice_data_dir)
WHISPER_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin"
VOICE_BASE = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
              "el/el_GR/rapunzelina")
WAV = "/tmp/voicekbd_gui.wav"
CONFIG_DIR = os.path.expanduser("~/.config/voicekbd")
CONFIG = os.path.join(CONFIG_DIR, "config")

# ---- continuous-mode (VAD) tuning ----
SR = 16000
FRAME = 480                 # 30 ms @ 16 kHz
FRAME_BYTES = FRAME * 2
SILENCE_HANG = 18           # ~0.55 s of quiet ends a phrase
MIN_SPEECH = 7              # ignore blips shorter than ~0.2 s
PREROLL = 6                 # ~0.18 s kept before speech onset

# ---- colors ----
BG = "#1e1f2b"
FG = "#e6e6f0"
ACCENT = "#5b8cff"
GLOW_ON = "#ff4d5e"
GLOW_IDLE = "#3a3d52"
PANEL = "#2a2c3d"

LAST_ACTIVE = "🎯 Ενεργό παράθυρο (αυτό με focus)"
KWIN = ["gdbus", "call", "--session", "--dest", "org.kde.KWin",
        "--object-path", "/WindowsRunner", "--method"]


# ============ audio / device helpers ============
def list_sources():
    out = subprocess.run(["pactl", "list", "short", "sources"],
                         capture_output=True, text=True).stdout
    devices = []
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) < 2 or cols[1].endswith(".monitor"):
            continue
        name = cols[1]
        pretty = name.replace("alsa_input.", "").replace("_", " ").replace(".", " · ")
        devices.append((name, pretty))
    return devices


# whisper.cpp emits non-speech annotations on silence/noise, e.g.
# "[BLANK_AUDIO]", "[ Silence ]", "[ Music ]", "(clears throat)", "*laughs*".
# These must never be typed into the user's apps — strip them before delivery.
_NONSPEECH_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|[♪♫🎵]")


def clean_transcript(text):
    """Drop whisper's bracketed non-speech markers; return '' if nothing remains."""
    text = _NONSPEECH_RE.sub(" ", text)
    text = " ".join(text.split()).strip()
    # if all that's left is stray punctuation/brackets, treat as empty
    if not re.search(r"\w", text):
        return ""
    return text


def write_wav(path, pcm_bytes):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm_bytes)


def frame_rms(b):
    if len(b) < 2:
        return 0.0
    a = array.array("h")
    a.frombytes(b[: len(b) // 2 * 2])
    return (sum(x * x for x in a) / len(a)) ** 0.5 if a else 0.0


def rms_level(path):
    try:
        size = os.path.getsize(path)
        if size <= 44:
            return 0.0
        with open(path, "rb") as f:
            f.seek(max(44, size - 8000))
            raw = f.read()
        a = array.array("h")
        a.frombytes(raw[: len(raw) // 2 * 2])
        if not a:
            return 0.0
        return min(1.0, (sum(x * x for x in a) / len(a)) ** 0.5 / 8000.0)
    except Exception:
        return 0.0


# ============ KWin window helpers ============
_WIN_RE = re.compile(r"\('(0_\{[^}]+\})',\s*'((?:[^'\\]|\\.)*)',\s*'((?:[^'\\]|\\.)*)'")


def list_windows():
    """Return [(appclass, 'title — appclass'), ...] of open windows."""
    try:
        out = subprocess.run(KWIN + ["org.kde.krunner1.Match", ""],
                             capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return []
    wins, seen = [], set()
    SKIP = {"xwaylandvideobridge", "plasmashell"}
    for mid, title, appcls in _WIN_RE.findall(out):
        title = title.replace("\\'", "'")
        if appcls in SKIP or appcls in seen:
            continue
        seen.add(appcls)
        wins.append((appcls, f"{title}  ·  {appcls}"))
    return wins


def resolve_window(appclass):
    """Get a live match-id for an app-class (ids change every session)."""
    try:
        out = subprocess.run(KWIN + ["org.kde.krunner1.Match", appclass],
                             capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return None
    for mid, _title, appcls in _WIN_RE.findall(out):
        if appcls == appclass:
            return mid
    m = re.search(r"0_\{[^}]+\}", out)
    return m.group(0) if m else None


def activate_window(match_id):
    subprocess.run(KWIN + ["org.kde.krunner1.Run", match_id, ""],
                   capture_output=True, timeout=4)


# ============ config ============
def load_config():
    cfg = {}
    try:
        with open(CONFIG) as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    cfg[k] = v
    except FileNotFoundError:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG, "w") as f:
        for k, v in cfg.items():
            f.write(f"{k}={v}\n")


# ============ scrollable container ============
class ScrollFrame(tk.Frame):
    """A frame whose content scrolls vertically when it doesn't fit."""
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        vsb = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(
            self._win, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self._wheel(True))
        self.canvas.bind("<Leave>", lambda e: self._wheel(False))

    def _wheel(self, on):
        seqs = ("<MouseWheel>", "<Button-4>", "<Button-5>")
        if on:
            for s in seqs:
                self.canvas.bind_all(s, self._scroll)
        else:
            for s in seqs:
                self.canvas.unbind_all(s)

    def _scroll(self, e):
        d = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        self.canvas.yview_scroll(d, "units")


# ============ app ============
class VoiceKeyboard:
    def __init__(self, root, parent=None, notebook=None, tab_read=None, reader=None):
        self.root = root
        self.parent = parent if parent is not None else root
        self.notebook = notebook
        self.tab_read = tab_read
        self.reader = reader
        self.cfg = load_config()
        self.recording = False
        self.rec_proc = None
        self.pulse = 0.0
        self.mini = False
        # continuous
        self.cont_on = False
        self.cont_proc = None
        self.cont_stop = threading.Event()
        self.seg_queue = queue.Queue()
        self.live_level = 0.0
        self.seg_counter = 0
        self._have_focus = False        # does OUR window hold keyboard focus?
        self.text_shown = False

        if self.parent is root:
            root.title("Φωνητικό Πληκτρολόγιο")
            root.geometry("540x640")
            root.minsize(300, 320)
        self.parent.configure(bg=BG)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        for s in ("TCombobox",):
            style.configure(s, fieldbackground=PANEL, background=PANEL, foreground=FG)
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.configure("TRadiobutton", background=BG, foreground=FG)

        # ---------- HEADER (target selector) — hidden in mini ----------
        self.header = tk.Frame(self.parent, bg=BG)
        tk.Label(self.header, text="🎯  Στόχος (πού πάει το κείμενο)", bg=BG, fg=FG,
                 font=("Sans", 10, "bold")).pack(anchor="w")
        r2 = tk.Frame(self.header, bg=BG); r2.pack(fill="x", pady=(2, 0))
        self.target_var = tk.StringVar()
        self.target_box = ttk.Combobox(r2, textvariable=self.target_var, state="readonly")
        self.target_box.pack(side="left", fill="x", expand=True)
        self.target_box.bind("<<ComboboxSelected>>", lambda e: self.save())
        tk.Button(r2, text="⟳", command=self.refresh_windows, bg=PANEL, fg=FG,
                  relief="flat", width=3).pack(side="left", padx=(6, 0))
        self.header.pack(fill="x", padx=16, pady=(14, 4))

        # ---------- MIC AREA — always visible ----------
        self.mic_area = tk.Frame(self.parent, bg=BG)
        topbar = tk.Frame(self.mic_area, bg=BG); topbar.pack(fill="x")
        self.mini_btn = tk.Button(topbar, text="🔽 mini", command=self.toggle_mini,
                                  bg=PANEL, fg=FG, relief="flat")
        self.mini_btn.pack(side="right")
        self.topmost_var = tk.BooleanVar(value=self.cfg.get("TOP", "0") == "1")
        ttk.Checkbutton(topbar, text="📌 Πάντα μπροστά", variable=self.topmost_var,
                        command=self.apply_topmost).pack(side="right", padx=8)
        self.canvas = tk.Canvas(self.mic_area, width=240, height=240, bg=BG,
                                highlightthickness=0)
        self.canvas.pack()
        self.canvas.bind("<Button-1>", lambda e: self.toggle())
        self.status = tk.Label(self.mic_area, text="Έτοιμο — πάτησε το μικρόφωνο",
                               bg=BG, fg="#9aa0b5", font=("Sans", 10), wraplength=300)
        self.status.pack(pady=(2, 4))
        self.level = tk.Canvas(self.mic_area, width=300, height=12, bg=PANEL,
                               highlightthickness=0)
        self.level.pack(pady=(0, 4))
        self.level_bar = self.level.create_rectangle(0, 0, 0, 12, fill=ACCENT, width=0)
        self.mic_area.pack(fill="x", padx=16)

        # ---------- CONTROLS — hidden in mini ----------
        self.controls = tk.Frame(self.parent, bg=BG)
        opt = tk.Frame(self.controls, bg=BG); opt.pack(fill="x", pady=(6, 0))
        self.lang_var = tk.StringVar(value=self.cfg.get("LANG", "el"))
        tk.Label(opt, text="Γλώσσα:", bg=BG, fg=FG).pack(side="left")
        ttk.Radiobutton(opt, text="Ελληνικά", variable=self.lang_var, value="el",
                        command=self.save).pack(side="left", padx=4)
        ttk.Radiobutton(opt, text="Auto", variable=self.lang_var, value="auto",
                        command=self.save).pack(side="left", padx=4)
        self.deliver_var = tk.BooleanVar(value=self.cfg.get("TYPE", "1") == "1")
        ttk.Checkbutton(opt, text="Παράδοση στην εφαρμογή", variable=self.deliver_var,
                        command=self.save).pack(side="right")
        opt2 = tk.Frame(self.controls, bg=BG); opt2.pack(fill="x", pady=(2, 0))
        self.cont_var = tk.BooleanVar(value=self.cfg.get("CONT", "0") == "1")
        ttk.Checkbutton(opt2, text="🔁 Συνεχής λειτουργία (γράφει στις παύσεις)",
                        variable=self.cont_var, command=self.save).pack(side="left")
        # text panel — optional, hidden by default (toggle in Settings)
        self.text_panel = tk.Frame(self.controls, bg=BG)
        tk.Label(self.text_panel, text="Κείμενο:", bg=BG, fg=FG,
                 font=("Sans", 10, "bold")).pack(anchor="w", pady=(8, 0))
        self.text = tk.Text(self.text_panel, height=5, bg=PANEL, fg=FG,
                            insertbackground=FG, relief="flat", wrap="word",
                            font=("Sans", 11))
        self.text.pack(fill="both", expand=True, pady=(2, 6))
        bottom = tk.Frame(self.text_panel, bg=BG); bottom.pack(fill="x", pady=(0, 12))
        tk.Button(bottom, text="📋 Αντιγραφή", command=self.copy_text, bg=PANEL, fg=FG,
                  relief="flat").pack(side="left")
        tk.Button(bottom, text="🗑 Καθαρισμός", command=lambda: self.text.delete("1.0", "end"),
                  bg=PANEL, fg=FG, relief="flat").pack(side="left", padx=6)
        self.controls.pack(fill="both", expand=True, padx=16)

        # ---------- MINI BAR — two compact buttons (built once, shown in mini) ----------
        self.mini_bar = tk.Frame(self.parent, bg=BG)
        self.mini_mic = tk.Button(self.mini_bar, text="🎤", command=self.toggle,
                                  bg=ACCENT, fg="#0b1020", relief="flat",
                                  font=("Sans", 18), takefocus=0)
        self.mini_mic.pack(side="left", expand=True, fill="both", padx=(3, 2), pady=3)
        self.mini_spk = tk.Button(self.mini_bar, text="🔊", command=self.read_clipboard,
                                  bg=PANEL, fg=FG, relief="flat",
                                  font=("Sans", 18), takefocus=0)
        self.mini_spk.pack(side="left", expand=True, fill="both", padx=2, pady=3)
        self.mini_expand = tk.Button(self.mini_bar, text="⤢", command=self.toggle_mini,
                                     bg=PANEL, fg="#9aa0b5", relief="flat",
                                     font=("Sans", 10), takefocus=0)
        self.mini_expand.pack(side="left", fill="y", padx=(2, 3), pady=3)

        self.refresh_windows()
        self.apply_topmost()
        self.set_text_panel(self.cfg.get("SHOW_TEXT", "0") == "1")
        self.setup_keys()
        self.draw_mic()
        self.animate()

    # ---------- selectors ----------
    def refresh_windows(self):
        self.windows = list_windows()
        labels = [LAST_ACTIVE] + [lbl for _, lbl in self.windows]
        self.target_box["values"] = labels
        saved = self.cfg.get("TARGET", "")
        keys = [""] + [k for k, _ in self.windows]
        if saved in keys:
            self.target_box.current(keys.index(saved))
        else:
            self.target_box.current(0)

    def current_device(self):
        return load_config().get("DEVICE", "") or None    # set in Settings tab

    def target_key(self):
        i = self.target_box.current()
        if i <= 0:
            return ""                       # active / focused window
        return self.windows[i - 1][0]

    def save(self, _=None):
        cfg = load_config()                 # merge — Settings owns other keys
        cfg.update(TARGET=self.target_key(),
                   LANG=self.lang_var.get(),
                   TYPE="1" if self.deliver_var.get() else "0",
                   CONT="1" if self.cont_var.get() else "0",
                   TOP="1" if self.topmost_var.get() else "0")
        save_config(cfg)
        self.cfg = cfg

    # ---------- text panel toggle ----------
    def set_text_panel(self, show):
        self.text_shown = bool(show)
        if show:
            self.text_panel.pack(fill="both", expand=True)
        else:
            self.text_panel.pack_forget()
        if getattr(self, "_fit", None):           # grow/shrink the window to match
            self.root.after(10, self._fit)

    # ---------- keyboard navigation ----------
    def setup_keys(self):
        r = self.root
        if self.notebook is not None:
            r.bind_all("<Control-Key-1>", lambda e: self._go_tab(0))
            r.bind_all("<Control-Key-2>", lambda e: self._go_tab(1))
            r.bind_all("<Control-Key-3>", lambda e: self._go_tab(2))
        # action keys — work anywhere in the app, incl. mini mode
        r.bind_all("<F2>", lambda e: self.toggle())               # 🎤 talk
        r.bind_all("<F4>", lambda e: self.read_clipboard())       # 🔊 read
        r.bind_all("<Control-space>", lambda e: (self.toggle(), "break")[1])
        # track whether the OS gives keyboard focus to our toplevel — so we can
        # step aside before pasting into the "active window" (else we paste into us)
        r.bind("<FocusIn>", self._track_focus, add="+")
        r.bind("<FocusOut>", self._track_focus, add="+")

    def _track_focus(self, e):
        if e.widget is self.root:
            self._have_focus = (e.type == tk.EventType.FocusIn)

    def _go_tab(self, i):
        try:
            if self.mini:                       # leave mini before switching tabs
                self.toggle_mini()
            self.notebook.select(i)
        except Exception:
            pass
        return "break"

    # ---------- window chrome ----------
    def apply_topmost(self):
        self.root.attributes("-topmost", bool(self.topmost_var.get()))
        self.save()

    def toggle_mini(self):
        self.mini = not self.mini
        if self.mini:
            self.header.pack_forget()
            self.mic_area.pack_forget()
            self.controls.pack_forget()
            self.mini_bar.pack(fill="both", expand=True)
            self.topmost_var.set(True); self.apply_topmost()
            if self.notebook is not None:
                self.notebook.select(self.parent)            # show dictation
                if self.tab_read is not None:
                    self.notebook.hide(self.tab_read)        # drop reading tab
                self.notebook.configure(style="Headless.TNotebook")  # hide tab strip
            self.root.minsize(120, 48)                       # allow it to be tiny
            self.root.geometry("190x60")
        else:
            self.mini_bar.pack_forget()
            self.mini_btn.config(text="🔽 mini")
            self.mic_area.pack(fill="x", padx=16)
            self.header.pack(fill="x", padx=16, pady=(14, 4), before=self.mic_area)
            self.controls.pack(fill="both", expand=True, padx=16, after=self.mic_area)
            if self.notebook is not None:
                self.notebook.configure(style="TNotebook")
                if self.tab_read is not None:
                    self.notebook.add(self.tab_read)         # restore reading tab
            self.root.minsize(300, 320)
            self.root.geometry("560x660")
        self.draw_mic()

    def read_clipboard(self):
        """Mini speaker button: speak clipboard (Ctrl+C'd text), or stop if speaking."""
        if not self.reader:
            return
        if self.reader.speaking:
            self.reader.stop()
            return
        txt = self.reader._clip()
        if txt.strip():
            self.reader._start(txt)
        else:
            self.status.config(text="📋 Άδειο πρόχειρο — κάνε Ctrl+C πρώτα")

    # ---------- mic drawing ----------
    def draw_mic(self):
        c = self.canvas
        c.delete("all")
        w = int(c["width"]); h = int(c["height"])
        cx, cy = w // 2, h // 2
        R = min(w, h) // 2
        active = self.recording or self.cont_on
        glow = GLOW_ON if active else GLOW_IDLE
        rg = (R - 5) + (int(0.10 * R * abs(self.pulse)) if active else 0)
        c.create_oval(cx - rg, cy - rg, cx + rg, cy + rg, outline=glow,
                      width=max(3, R // 18))
        rd = int(R * 0.62)
        c.create_oval(cx - rd, cy - rd, cx + rd, cy + rd,
                      fill=(GLOW_ON if active else ACCENT), outline="")
        c.create_text(cx, cy, text="🎤", font=("Sans", max(18, int(R * 0.55))))

    def animate(self):
        active = self.recording or self.cont_on
        if active:
            self.pulse = math.sin(time.time() * 6)
        if self.mini:
            self.mini_mic.config(bg=(GLOW_ON if active else ACCENT),
                                 text=("⏹" if active else "🎤"))
            spk = bool(self.reader and self.reader.speaking)
            self.mini_spk.config(bg=("#7a2330" if spk else PANEL),
                                 text=("⏹" if spk else "🔊"))
        elif active:
            self.draw_mic()
            lvl = self.live_level if self.cont_on else rms_level(WAV)
            mw = int(self.level["width"])
            self.level.coords(self.level_bar, 0, 0, int(mw * lvl), 12)
            col = "#ff4d5e" if lvl > 0.7 else (ACCENT if lvl > 0.05 else "#555a72")
            self.level.itemconfig(self.level_bar, fill=col)
        self.root.after(60, self.animate)

    # ---------- toggle dispatch ----------
    def toggle(self):
        if self.cont_on:
            self.stop_continuous()
        elif self.recording:
            self.stop_and_transcribe()
        elif self.cont_var.get():
            self.start_continuous()
        else:
            self.start()

    # ---------- push-to-talk ----------
    def start(self):
        dev = self.current_device()
        if not dev:
            self.status.config(text="⚠ Διάλεξε μικρόφωνο"); return
        try:
            os.remove(WAV)
        except FileNotFoundError:
            pass
        self.rec_proc = subprocess.Popen(
            ["pw-record", "--target", dev, "--rate", "16000", "--channels", "1",
             "--format", "s16", WAV], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.recording = True
        self.status.config(text="🔴 Ηχογράφηση… (πάτησε ξανά για στοπ)")
        self.draw_mic()

    def stop_and_transcribe(self):
        self.recording = False
        if self.rec_proc:
            self.rec_proc.send_signal(signal.SIGINT)
            try:
                self.rec_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.rec_proc.kill()
            self.rec_proc = None
        self.level.coords(self.level_bar, 0, 0, 0, 12)
        self.draw_mic()
        self.status.config(text="⏳ Μεταγραφή…")
        threading.Thread(target=self._ptt_worker, daemon=True).start()

    def _ptt_worker(self):
        txt = self._run_whisper(WAV)
        self.root.after(0, lambda: self._show_result(txt))

    def _show_result(self, txt):
        if not txt:
            self.status.config(text="🤔 Δεν ακούστηκε τίποτα — έλεγξε το μικρόφωνο")
            return
        self._append(txt)
        self.status.config(text="✓ Έτοιμο")
        if self.deliver_var.get():
            threading.Thread(target=self.deliver, args=(txt + " ", self._have_focus),
                             daemon=True).start()

    # ---------- continuous ----------
    def start_continuous(self):
        dev = self.current_device()
        if not dev:
            self.status.config(text="⚠ Διάλεξε μικρόφωνο"); return
        self.cont_on = True
        self.cont_stop.clear()
        self.seg_counter = 0
        while not self.seg_queue.empty():
            try:
                self.seg_queue.get_nowait()
            except queue.Empty:
                break
        self.cont_proc = subprocess.Popen(
            ["pw-record", "--target", dev, "--rate", "16000", "--channels", "1",
             "--format", "s16", "--raw", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._worker_loop, daemon=True).start()
        self.draw_mic()
        self.status.config(text="🎧 Ακούω… μίλα ελεύθερα")

    def stop_continuous(self):
        self.cont_on = False
        self.cont_stop.set()
        if self.cont_proc:
            try:
                self.cont_proc.terminate()
            except Exception:
                pass
            self.cont_proc = None
        self.live_level = 0.0
        self.level.coords(self.level_bar, 0, 0, 0, 12)
        self.draw_mic()
        self.status.config(text="⏹ Σταμάτησε")

    def _read_frame(self, proc):
        buf = b""
        while len(buf) < FRAME_BYTES:
            try:
                chunk = proc.stdout.read(FRAME_BYTES - len(buf))
            except Exception:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def _capture_loop(self):
        proc = self.cont_proc
        noise, calib = 200.0, []
        in_speech, silence, speech_frames = False, 0, 0
        seg, preroll, seen = bytearray(), [], 0

        def emit(buf):
            self.seg_counter += 1
            p = f"/tmp/voicekbd_seg_{self.seg_counter}.wav"
            write_wav(p, bytes(buf))
            self.seg_queue.put(p)

        while not self.cont_stop.is_set():
            fb = self._read_frame(proc)
            if fb is None:
                break
            r = frame_rms(fb)
            self.live_level = min(1.0, r / 4000.0)
            seen += 1
            if seen <= 13:
                calib.append(r); noise = sum(calib) / len(calib); continue
            thresh = max(noise * 2.5 + 120, 280)
            if not in_speech:
                preroll.append(fb)
                if len(preroll) > PREROLL:
                    preroll.pop(0)
                if r > thresh:
                    in_speech, silence, speech_frames = True, 0, 1
                    seg = bytearray(b"".join(preroll)); seg += fb
                else:
                    noise = 0.95 * noise + 0.05 * r
            else:
                seg += fb
                if r > thresh:
                    silence, speech_frames = 0, speech_frames + 1
                else:
                    silence += 1
                    if silence >= SILENCE_HANG:
                        if speech_frames >= MIN_SPEECH:
                            emit(seg)
                        in_speech, seg, preroll = False, bytearray(), []
        if in_speech and speech_frames >= MIN_SPEECH:
            emit(seg)
        self.seg_queue.put(None)

    def _worker_loop(self):
        while True:
            path = self.seg_queue.get()
            if path is None:
                break
            txt = self._run_whisper(path)
            try:
                os.remove(path)
            except OSError:
                pass
            if txt:
                self.root.after(0, lambda t=txt: self._emit_chunk(t))

    def _emit_chunk(self, txt):
        self._append(txt)
        if self.deliver_var.get():
            threading.Thread(target=self.deliver, args=(txt + " ", self._have_focus),
                             daemon=True).start()

    # ---------- whisper ----------
    def _run_whisper(self, path):
        try:
            res = subprocess.run(
                [WHISPER, "-m", MODEL, "-f", path, "-l", self.lang_var.get(),
                 "-t", "8", "-nt", "-np"],
                capture_output=True, text=True, timeout=120)
            return clean_transcript(res.stdout)
        except Exception:
            return ""

    # ---------- delivery ----------
    def _append(self, txt):
        self.text.insert("end", txt + " ")
        self.text.see("end")

    def deliver(self, txt, had_focus=False):
        """Send text to the chosen target: activate window + clipboard paste."""
        key = self.target_key()
        try:
            subprocess.run(["wl-copy"], input=txt.encode(), timeout=5)
        except Exception:
            pass
        if key:                                   # specific app window
            mid = resolve_window(key)
            if mid:
                activate_window(mid)
                time.sleep(0.18)
        elif had_focus:
            # "active window" mode but WE hold focus → hide so the window we were
            # over regains focus, otherwise Ctrl+V would paste into ourselves.
            # Remember geometry so re-showing keeps its place (no recentering).
            box = {}
            ev = threading.Event()

            def hide():
                box["geo"] = self.root.geometry()
                self.root.withdraw()
                ev.set()

            self.root.after(0, hide)
            ev.wait(1)
            time.sleep(0.3)
        else:
            time.sleep(0.35)                      # let focus settle on last window
        try:
            subprocess.run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"],
                           timeout=10)            # Ctrl+V
        except Exception:
            pass
        if not key and had_focus:                 # bring our window back afterwards
            self.root.after(0, lambda: self._reshow_after_deliver(box.get("geo")))

    def _reshow_after_deliver(self, geo=None):
        try:
            if geo:
                self.root.geometry(geo)           # restore exact position/size
            self.root.deiconify()                 # NOTE: no lift() — we don't grab
            # focus back, so the target keeps it and further pastes need no hiding
        except Exception:
            pass

    def copy_text(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.text.get("1.0", "end").strip())
        self.status.config(text="📋 Αντιγράφηκε")

    # ---------- control socket: global shortcuts drive THIS window ----------
    def start_control_server(self):
        try:
            os.unlink(CTL_SOCK)
        except OSError:
            pass
        try:
            self._ctl = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self._ctl.bind(CTL_SOCK)
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
            if cmd == "talk":
                self.root.after(0, self.toggle)
            elif cmd == "read":
                self.root.after(0, self.read_clipboard)
            elif cmd == "show":
                self.root.after(0, self._raise)

    def _raise(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes(
            "-topmost", bool(self.topmost_var.get())))


# ============ settings tab (global shortcuts) ============
_MODS = [(0x40, "Meta"), (0x4, "Ctrl"), (0x8, "Alt"), (0x1, "Shift")]
_BARE = {"Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
         "Super_L", "Super_R", "Meta_L", "Meta_R", "ISO_Level3_Shift"}


def kde_combo(event):
    """Build a KDE shortcut string like 'Meta+Z' from a Tk key event."""
    if event.keysym in _BARE:
        return None
    mods = [name for bit, name in _MODS if event.state & bit]
    key = event.keysym
    if len(key) == 1:
        key = key.upper()
    return "+".join(mods + [key])


class SettingsTab:
    def __init__(self, root, parent, vk=None):
        self.root = root
        self.vk = vk
        self.cfg = load_config()
        parent.configure(bg=BG)

        # --- microphone (moved here from the dictation tab) ---
        tk.Label(parent, text="🎙  Μικρόφωνο", bg=BG, fg=FG,
                 font=("Sans", 12, "bold")).pack(anchor="w", padx=16, pady=(16, 2))
        mrow = tk.Frame(parent, bg=BG); mrow.pack(fill="x", padx=16)
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(mrow, textvariable=self.device_var,
                                        state="readonly")
        self.device_box.pack(side="left", fill="x", expand=True)
        self.device_box.bind("<<ComboboxSelected>>", self.on_device)
        tk.Button(mrow, text="⟳", command=self.refresh_devices, bg=PANEL, fg=FG,
                  relief="flat", width=3).pack(side="left", padx=(6, 0))

        # --- appearance ---
        self.show_text = tk.BooleanVar(value=self.cfg.get("SHOW_TEXT", "0") == "1")
        ttk.Checkbutton(parent, text="Εμφάνιση πλαισίου κειμένου στην Υπαγόρευση",
                        variable=self.show_text, command=self.on_showtext).pack(
                        anchor="w", padx=16, pady=(10, 4))
        ttk.Separator(parent).pack(fill="x", padx=16, pady=6)

        tk.Label(parent, text="⌨  Συντομεύσεις (καθολικές — δουλεύουν παντού)",
                 bg=BG, fg=FG, font=("Sans", 12, "bold")).pack(anchor="w",
                 padx=16, pady=(6, 4))
        tk.Label(parent, text="Πάτα «Όρισε» και μετά τον συνδυασμό πλήκτρων που θες.",
                 bg=BG, fg="#9aa0b5").pack(anchor="w", padx=16)

        self.sc_dict = tk.StringVar(value=self.cfg.get("SC_DICT", "Meta+Z"))
        self.sc_read = tk.StringVar(value=self.cfg.get("SC_READ", "Meta+R"))
        self._row(parent, "🎤  Ομιλία (έναρξη/λήξη υπαγόρευσης)", self.sc_dict)
        self._row(parent, "🔊  Ανάγνωση επιλογής / προχείρου", self.sc_read)

        btns = tk.Frame(parent, bg=BG); btns.pack(fill="x", padx=16, pady=14)
        tk.Button(btns, text="💾 Αποθήκευση & Ενεργοποίηση", command=self.apply,
                  bg=ACCENT, fg="#0b1020", relief="flat",
                  font=("Sans", 11, "bold")).pack(side="left")
        tk.Button(btns, text="Άνοιγμα ρυθμίσεων KDE", command=self.open_kde,
                  bg=PANEL, fg=FG, relief="flat").pack(side="left", padx=8)

        self.status = tk.Label(parent, text="", bg=BG, fg="#9aa0b5",
                               wraplength=480, justify="left")
        self.status.pack(fill="x", padx=16, pady=(4, 0))
        ttk.Separator(parent).pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(parent, text="⌨  Πλήκτρα μέσα στην εφαρμογή (και στο mini)",
                 bg=BG, fg=FG, font=("Sans", 11, "bold")).pack(anchor="w", padx=16)
        tk.Label(parent, text="Ctrl+1 / 2 / 3 → καρτέλες  ·  F2 → μικρόφωνο (μίλα)  ·  "
                              "F4 → ανάγνωση προχείρου  ·  Tab → μετακίνηση, Space → ενεργοποίηση",
                 bg=BG, fg="#9aa0b5", justify="left", wraplength=500).pack(
                 anchor="w", padx=16, pady=(2, 0))
        tk.Label(parent, text="Σημ.: η ομιλία τρέχει το dictate.sh, η ανάγνωση το speak.sh. "
                              "Αν μια καθολική συντόμευση δεν πιάσει, όρισέ την χειροκίνητα "
                              "από τις ρυθμίσεις KDE → Συντομεύσεις → Προσθήκη Εντολής.",
                 bg=BG, fg="#6b7088", justify="left", wraplength=500).pack(
                 anchor="w", padx=16, pady=(10, 0))
        self.refresh_devices()

    def refresh_devices(self):
        self.devices = list_sources()
        self.device_box["values"] = [p for _, p in self.devices]
        saved = self.cfg.get("DEVICE", "")
        names = [n for n, _ in self.devices]
        if saved in names:
            self.device_box.current(names.index(saved))
        elif self.devices:
            self.device_box.current(0)
            self.on_device()

    def on_device(self, _=None):
        i = self.device_box.current()
        dev = self.devices[i][0] if 0 <= i < len(self.devices) else ""
        cfg = load_config(); cfg["DEVICE"] = dev; save_config(cfg); self.cfg = cfg

    def on_showtext(self):
        cfg = load_config()
        cfg["SHOW_TEXT"] = "1" if self.show_text.get() else "0"
        save_config(cfg); self.cfg = cfg
        if self.vk:
            self.vk.set_text_panel(self.show_text.get())

    def _row(self, parent, label, var):
        row = tk.Frame(parent, bg=BG); row.pack(fill="x", padx=16, pady=6)
        tk.Label(row, text=label, bg=BG, fg=FG, width=34, anchor="w").pack(side="left")
        disp = tk.Entry(row, textvariable=var, state="readonly", width=16,
                        readonlybackground=PANEL, fg=FG, relief="flat",
                        justify="center")
        disp.pack(side="left", padx=6)
        tk.Button(row, text="⌨ Όρισε", command=lambda: self._capture(var, disp),
                  bg=PANEL, fg=FG, relief="flat").pack(side="left")

    def _capture(self, var, disp):
        disp.config(readonlybackground=ACCENT)
        self.status.config(text="🎹 Πάτα τώρα τον συνδυασμό πλήκτρων…")

        def on_key(e):
            combo = kde_combo(e)
            if combo is None:        # lone modifier, keep waiting
                return "break"
            var.set(combo)
            disp.config(readonlybackground=PANEL)
            self.status.config(text=f"Ορίστηκε: {combo} (πάτα Αποθήκευση)")
            self.root.unbind("<KeyPress>")
            return "break"

        self.root.bind("<KeyPress>", on_key)

    def _desktop(self, slug, name, script, combo):
        path = os.path.expanduser(f"~/.local/share/applications/{slug}.desktop")
        if not combo:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        with open(path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\n"
                    f"Name=Φωνή — {name}\nExec={script}\n"
                    "NoDisplay=true\nTerminal=false\n"
                    f"X-KDE-Shortcuts={combo}\n")

    def apply(self):
        # route through the running app so the UI reacts (mic glows, status, etc.)
        app = os.path.join(HERE, "voice_keyboard.py")
        self._desktop("voice-shortcut-dictate", "Ομιλία",
                      f'{sys.executable} "{app}" talk', self.sc_dict.get())
        self._desktop("voice-shortcut-read", "Ανάγνωση",
                      f'{sys.executable} "{app}" read', self.sc_read.get())
        rebuilt = False
        for kb in ("kbuildsycoca6", "kbuildsycoca5"):
            try:
                subprocess.run([kb], capture_output=True, timeout=20)
                rebuilt = True
                break
            except FileNotFoundError:
                continue
            except Exception:
                break
        cfg = load_config()
        cfg.update(SC_DICT=self.sc_dict.get(), SC_READ=self.sc_read.get())
        save_config(cfg); self.cfg = cfg
        self.status.config(text="✅ Αποθηκεύτηκαν. " + (
            "Δοκίμασε τις συντομεύσεις." if rebuilt else
            "Αν δεν δουλεύουν, κάνε αποσύνδεση/σύνδεση ή όρισέ τες από τις ρυθμίσεις KDE."))

    def open_kde(self):
        for cmd in (["kcmshell6", "kcm_keys"], ["systemsettings", "kcm_keys"],
                    ["kcmshell5", "khotkeys"]):
            try:
                subprocess.Popen(cmd)
                return
            except FileNotFoundError:
                continue


# ============ system tray (StatusNotifierItem via AppIndicator) ============
def start_tray(root, vk, reader):
    """Add a KDE/SNI tray icon. Returns the GLib loop thread, or None if no SNI.

    Runs GTK's main loop in a daemon thread; menu callbacks hop back to the Tk
    thread via root.after(). The menu is exported over D-Bus (DBusMenu) and
    rendered by Plasma, so we never draw GTK widgets ourselves.
    """
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3 as AppInd
        except (ValueError, ImportError):
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as AppInd
        from gi.repository import Gtk, GLib
    except Exception:
        return None

    def ui(fn):
        root.after(0, fn)

    def quit_all():
        try:
            vk.cont_stop.set()
        except Exception:
            pass
        for p in (getattr(vk, "rec_proc", None), getattr(vk, "cont_proc", None)):
            try:
                if p:
                    p.terminate()
            except Exception:
                pass
        try:
            if reader:
                reader.stop()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        os._exit(0)

    def run():
        try:
            Gtk.init(None)
        except Exception:
            pass
        ind = AppInd.Indicator.new(
            "voice-keyboard", "VoiceIcon",
            AppInd.IndicatorCategory.APPLICATION_STATUS)
        ind.set_icon_theme_path(os.path.join(HERE, "Assets"))
        ind.set_icon_full("VoiceIcon", "VOICE")
        ind.set_title("VOICE — Φωνή")
        ind.set_status(AppInd.IndicatorStatus.ACTIVE)

        menu = Gtk.Menu()

        def item(label, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda *_: ui(cb))
            menu.append(mi)
            return mi

        open_item = item("🪟  Άνοιγμα", vk._raise)
        item("🎤  Ομιλία (έναρξη/λήξη)", vk.toggle)
        item("🔊  Ανάγνωση προχείρου", vk.read_clipboard)
        menu.append(Gtk.SeparatorMenuItem())
        item("✖  Έξοδος", quit_all)
        menu.show_all()
        ind.set_menu(menu)
        ind.set_secondary_activate_target(open_item)   # primary click → open
        GLib.MainLoop().run()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def send_action(action):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(action.encode(), CTL_SOCK)
        s.close()
        return True
    except OSError:
        return False


def ensure_assets(root):
    """On first run (lean install) download the whisper model + Piper voices."""
    jobs = []
    if not os.path.exists(MODEL):
        jobs.append((WHISPER_URL, MODEL))
    for quality, stem in (("medium", "el_GR-rapunzelina-medium"),
                          ("low", "el_GR-rapunzelina-low")):
        for ext in (".onnx", ".onnx.json"):
            dest = os.path.join(VOICES_DIR, stem + ext)
            if not os.path.exists(dest):
                jobs.append((f"{VOICE_BASE}/{quality}/{stem}{ext}", dest))
    if not jobs:
        return

    dlg = tk.Toplevel(root)
    dlg.title("Λήψη μοντέλων…")
    dlg.configure(bg=BG)
    dlg.geometry("440x150")
    dlg.transient(root)
    dlg.grab_set()
    tk.Label(dlg, text="Πρώτη εκτέλεση — κατεβάζω τα μοντέλα φωνής (≈0.6 GB).\n"
                       "Γίνεται μόνο μία φορά.", bg=BG, fg=FG, justify="left",
             font=("Sans", 10)).pack(padx=16, pady=(16, 8), anchor="w")
    info = tk.Label(dlg, text="", bg=BG, fg="#9aa0b5")
    info.pack(padx=16, anchor="w")
    bar = ttk.Progressbar(dlg, length=400, maximum=100)
    bar.pack(padx=16, pady=12)
    state = {"err": None}

    def work():
        try:
            for i, (url, dest) in enumerate(jobs, 1):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                name = os.path.basename(dest)

                def hook(blocks, bs, total, name=name, i=i):
                    pct = (blocks * bs / total * 100) if total > 0 else 0
                    root.after(0, lambda: (bar.config(value=pct), info.config(
                        text=f"[{i}/{len(jobs)}]  {name}  —  {pct:0.0f}%")))

                tmp = dest + ".part"
                urllib.request.urlretrieve(url, tmp, hook)
                os.replace(tmp, dest)
        except Exception as e:
            state["err"] = str(e)
        root.after(0, dlg.destroy)

    threading.Thread(target=work, daemon=True).start()
    root.wait_window(dlg)
    if state["err"]:
        print("⚠ asset download failed:", state["err"], file=sys.stderr)


def main():
    # if launched with an action and an instance is already running, hand it over
    action = (sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in
              ("talk", "read", "show") else None)
    if action and send_action(action):
        return
    if not action and send_action("show"):
        return                       # single instance: raise the existing window
    if not os.path.exists(WHISPER):
        print("whisper-cli not found at", WHISPER, file=sys.stderr)
        sys.exit(1)
    root = tk.Tk()
    root.title("VOICE — Φωνή")
    root.configure(bg=BG)
    root.geometry("560x600")
    root.resizable(False, False)
    try:
        root._voice_icon = tk.PhotoImage(file=ICON)   # keep a ref so it's not GC'd
        root.iconphoto(True, root._voice_icon)
    except Exception:
        pass

    # dropdown popup lists don't inherit the ttk theme — fix contrast here
    root.option_add("*TCombobox*Listbox.background", PANEL)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#0b1020")

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    # readonly combobox field: keep our colors instead of white-on-white highlight
    style.map("TCombobox",
              fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)],
              selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", FG)])
    # check/radio: stop white-on-white on hover/focus
    for s in ("TCheckbutton", "TRadiobutton"):
        style.configure(s, background=BG, foreground=FG, focuscolor=BG)
        style.map(s, background=[("active", BG)], foreground=[("active", FG)])
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=[4, 6, 4, 0])
    style.configure("TNotebook.Tab", background="#21232f", foreground="#8a90a6",
                    padding=(16, 6), borderwidth=0, focuscolor=BG)
    style.map("TNotebook.Tab",
              background=[("selected", ACCENT), ("active", "#33374d")],
              foreground=[("selected", "#0b1020"), ("active", FG)],
              padding=[("selected", (20, 9))],
              expand=[("selected", (1, 1, 1, 0))])
    # headless variant for mini mode (no tab strip)
    style.configure("Headless.TNotebook", background=BG, borderwidth=0)
    style.layout("Headless.TNotebook.Tab", [])

    ensure_assets(root)          # first-run: fetch models/voices if missing

    nb = ttk.Notebook(root)
    tab_dict = tk.Frame(nb, bg=BG)
    tab_read = tk.Frame(nb, bg=BG)
    tab_set = tk.Frame(nb, bg=BG)
    nb.add(tab_dict, text="🎤  Υπαγόρευση")
    nb.add(tab_read, text="📖  Ανάγνωση")
    nb.add(tab_set, text="⚙  Ρυθμίσεις")
    nb.pack(fill="both", expand=True)

    reader = Reader(root, parent=tab_read)
    vk = VoiceKeyboard(root, parent=tab_dict, notebook=nb, tab_read=tab_read,
                       reader=reader)
    SettingsTab(root, parent=tab_set, vk=vk)
    vk.start_control_server()

    # tray icon: closing the window hides to tray; "Έξοδος" truly quits
    tray = start_tray(root, vk, reader)
    if tray:
        root.protocol("WM_DELETE_WINDOW", root.withdraw)

    # auto-fit window height per tab (no wasted space)
    heights = {0: 540, 1: 560, 2: 680}

    def on_tab(_=None):
        if vk.mini:
            return
        try:
            idx = nb.index(nb.select())
        except Exception:
            return
        w = root.winfo_width()
        h = heights.get(idx, 600)
        if idx == 0 and getattr(vk, "text_shown", False):
            h += 210                       # room for the text box + copy/clear row
        root.geometry(f"{w if w > 100 else 560}x{h}")

    vk._fit = on_tab                       # let set_text_panel re-fit on toggle
    nb.bind("<<NotebookTabChanged>>", on_tab)
    root.after(120, on_tab)
    if action:                       # started fresh via a shortcut → do it
        if action == "talk":         # show compact mic for live feedback + control
            root.after(300, vk.toggle_mini)
        root.after(900, lambda: vk.toggle() if action == "talk"
                   else vk.read_clipboard())
    root.mainloop()


if __name__ == "__main__":
    main()
