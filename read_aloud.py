#!/usr/bin/env python3
"""
📖 Ανάγνωση — Greek read-aloud view (Piper TTS).

Load a PDF/email or paste text, SELECT with the mouse what you want, and it reads
it in Greek. Optional auto-read: speaks whatever you copy (e.g. a Claude reply).

Engine: Piper (local CPU). Pure standard-library Tkinter — no pip deps.
"""
import os
import re
import sys
import wave
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog

HERE = os.path.dirname(os.path.abspath(__file__))
PIPER = os.path.join(HERE, "piper_tts", "piper", "piper")
VOICES = {
    "Μεσαία (πιο φυσική)": os.path.join(HERE, "voices", "el_GR-rapunzelina-medium.onnx"),
    "Χαμηλή (πιο γρήγορη)": os.path.join(HERE, "voices", "el_GR-rapunzelina-low.onnx"),
}

BG = "#1e1f2b"; FG = "#e6e6f0"; ACCENT = "#5b8cff"; PANEL = "#2a2c3d"

# split into speakable chunks: by sentence enders, and very long runs by comma
_SPLIT = re.compile(r"(?<=[.!;?·\n])\s+")


def chunks(text):
    out = []
    for part in _SPLIT.split(text):
        part = part.strip()
        if not part:
            continue
        while len(part) > 280:
            cut = part.rfind(",", 0, 280)
            cut = cut if cut > 80 else 280
            out.append(part[:cut + 1].strip())
            part = part[cut + 1:].strip()
        out.append(part)
    return out


def pad_wav(src, dst, secs=0.25):
    with wave.open(src, "rb") as w:
        p = w.getparams(); frames = w.readframes(w.getnframes())
    pad = b"\x00" * int(p.framerate * secs) * p.sampwidth * p.nchannels
    with wave.open(dst, "wb") as o:
        o.setparams(p); o.writeframes(pad + frames + pad)


class Reader:
    def __init__(self, root, parent=None):
        self.root = root
        parent = parent if parent is not None else root
        self.model = list(VOICES.values())[0]
        self.speed = 1.0
        self.stop_flag = threading.Event()
        self.play_proc = None
        self.speaking = False
        self.auto_on = False
        self.last_clip = None

        if parent is root:
            root.title("📖 Ανάγνωση — Φωνή Claude/PDF")
            root.geometry("640x620")
            root.minsize(420, 400)
        parent.configure(bg=BG)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG)
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.configure("Horizontal.TScale", background=BG)

        # --- top toolbar: sources ---
        bar = tk.Frame(parent, bg=BG); bar.pack(fill="x", padx=12, pady=(12, 4))
        tk.Button(bar, text="📂 Άνοιγμα αρχείου", command=self.open_document,
                  bg=PANEL, fg=FG, relief="flat").pack(side="left")
        tk.Button(bar, text="📋 Επικόλληση", command=self.from_clipboard, bg=PANEL, fg=FG,
                  relief="flat").pack(side="left", padx=6)
        tk.Button(bar, text="🗑 Καθαρισμός", command=lambda: self.text.delete("1.0", "end"),
                  bg=PANEL, fg=FG, relief="flat").pack(side="left")

        # --- text area (select with mouse) ---
        wrap = tk.Frame(parent, bg=BG); wrap.pack(fill="both", expand=True, padx=12, pady=4)
        self.text = tk.Text(wrap, height=6, bg=PANEL, fg=FG, insertbackground=FG,
                            relief="flat", wrap="word", font=("Sans", 12), padx=10, pady=8)
        self.text.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(wrap, command=self.text.yview)
        sb.pack(side="right", fill="y")
        self.text.config(yscrollcommand=sb.set)
        self.text.tag_configure("speaking", background="#3a4a7a")
        self.text.insert("1.0", "Φόρτωσε ένα PDF, κάνε επικόλληση, ή γράψε εδώ.\n"
                                "Μετά επίλεξε με το ποντίκι ό,τι θέλεις και πάτησε «Διάβασε επιλογή».")

        # --- voice + speed ---
        opt = tk.Frame(parent, bg=BG); opt.pack(fill="x", padx=12, pady=(2, 0))
        tk.Label(opt, text="Φωνή:", bg=BG, fg=FG).pack(side="left")
        self.voice_var = tk.StringVar(value=list(VOICES.keys())[0])
        vb = ttk.Combobox(opt, textvariable=self.voice_var, state="readonly",
                          values=list(VOICES.keys()), width=20)
        vb.pack(side="left", padx=6)
        vb.bind("<<ComboboxSelected>>", self.on_voice)
        tk.Label(opt, text="  Ρυθμός:", bg=BG, fg=FG).pack(side="left")
        self.speed_scale = ttk.Scale(opt, from_=0.8, to=1.4, value=1.0,
                                     command=self.on_speed, length=130)
        self.speed_scale.pack(side="left")
        self.speed_lbl = tk.Label(opt, text="1.00×", bg=BG, fg="#9aa0b5", width=6)
        self.speed_lbl.pack(side="left")

        # --- actions ---
        act = tk.Frame(parent, bg=BG); act.pack(fill="x", padx=12, pady=8)
        tk.Button(act, text="🔊 Διάβασε επιλογή", command=self.speak_selection,
                  bg=ACCENT, fg="#0b1020", relief="flat",
                  font=("Sans", 11, "bold")).pack(side="left")
        tk.Button(act, text="🔊 Διάβασε όλα", command=self.speak_all, bg=PANEL, fg=FG,
                  relief="flat").pack(side="left", padx=6)
        tk.Button(act, text="⏹ Στοπ", command=self.stop, bg="#7a2330", fg=FG,
                  relief="flat").pack(side="left")
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(act, text="🔁 Auto-ανάγνωση clipboard", variable=self.auto_var,
                        command=self.toggle_auto).pack(side="right")

        self.status = tk.Label(parent, text="Έτοιμο", bg=BG, fg="#9aa0b5", anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 8))

    # ---------- sources ----------
    def open_document(self):
        path = filedialog.askopenfilename(
            title="Διάλεξε αρχείο",
            filetypes=[("Έγγραφα", "*.pdf *.txt *.md *.docx *.odt *.doc *.rtf *.epub"),
                       ("PDF", "*.pdf"), ("Κείμενο", "*.txt *.md"),
                       ("Word/ODT", "*.docx *.odt *.doc *.rtf"), ("Όλα", "*.*")])
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        self.status.config(text="📂 Φόρτωση…"); self.root.update_idletasks()
        try:
            text = self._extract(path, ext)
        except Exception as e:
            self.status.config(text=f"⚠ Σφάλμα: {e}"); return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text.strip())
        self.status.config(text=f"📂 {os.path.basename(path)} ({len(text.split())} λέξεις)")

    def _extract(self, path, ext):
        if ext in (".txt", ".md", ""):
            return open(path, encoding="utf-8", errors="ignore").read()
        if ext == ".pdf":
            return subprocess.run(["pdftotext", "-layout", path, "-"],
                                  capture_output=True, text=True, timeout=60).stdout
        # docx / odt / doc / rtf / epub → LibreOffice headless → txt
        subprocess.run(["libreoffice", "--headless", "--convert-to", "txt:Text",
                        "--outdir", "/tmp", path], capture_output=True, timeout=120)
        base = os.path.splitext(os.path.basename(path))[0]
        return open(f"/tmp/{base}.txt", encoding="utf-8", errors="ignore").read()

    def from_clipboard(self):
        txt = self._clip()
        if txt:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", txt)
            self.status.config(text="📋 Επικολλήθηκε από το πρόχειρο")

    def _clip(self):
        try:
            return subprocess.run(["wl-paste"], capture_output=True, text=True,
                                  timeout=4).stdout.rstrip("\n")
        except Exception:
            return ""

    # ---------- options ----------
    def on_voice(self, _=None):
        self.model = VOICES[self.voice_var.get()]

    def on_speed(self, val):
        self.speed = float(val)
        self.speed_lbl.config(text=f"{self.speed:.2f}×")

    # ---------- speak ----------
    def speak_selection(self):
        try:
            txt = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            txt = ""
        if not txt.strip():
            self.speak_all(); return
        self._start(txt)

    def speak_all(self):
        self._start(self.text.get("1.0", "end"))

    def _start(self, text):
        if self.speaking:
            self.stop()
        text = text.strip()
        if not text:
            self.status.config(text="⚠ Δεν υπάρχει κείμενο"); return
        self.stop_flag.clear()
        self.speaking = True
        self.status.config(text="🔊 Διαβάζω…")
        threading.Thread(target=self._worker, args=(chunks(text),), daemon=True).start()

    def _worker(self, parts):
        raw, pwav = "/tmp/read_raw.wav", "/tmp/read_play.wav"
        for part in parts:
            if self.stop_flag.is_set():
                break
            try:
                subprocess.run([PIPER, "--model", self.model, "--length_scale",
                                str(self.speed), "--sentence_silence", "0.3",
                                "--output_file", raw], input=part.encode(),
                               capture_output=True, timeout=60)
                pad_wav(raw, pwav)
            except Exception:
                continue
            if self.stop_flag.is_set():
                break
            self.play_proc = subprocess.Popen(["pw-play", pwav])
            self.play_proc.wait()
            self.play_proc = None
        self.speaking = False
        self.root.after(0, lambda: self.status.config(
            text="✓ Τέλος" if not self.stop_flag.is_set() else "⏹ Σταμάτησε"))

    def stop(self):
        self.stop_flag.set()
        if self.play_proc:
            try:
                self.play_proc.terminate()
            except Exception:
                pass
        self.speaking = False
        self.status.config(text="⏹ Σταμάτησε")

    # ---------- auto-read clipboard ----------
    def toggle_auto(self):
        self.auto_on = self.auto_var.get()
        if self.auto_on:
            self.last_clip = self._clip()      # don't read what's already there
            self.status.config(text="🔁 Auto-ανάγνωση ΟΝ — αντίγραψε κείμενο για ανάγνωση")
            self._poll_clip()
        else:
            self.status.config(text="Auto-ανάγνωση OFF")

    def _poll_clip(self):
        if not self.auto_on:
            return
        cur = self._clip()
        if cur and cur != self.last_clip:
            self.last_clip = cur
            self._start(cur)
        self.root.after(1000, self._poll_clip)


def main():
    if not os.path.exists(PIPER):
        print("piper not found at", PIPER, file=sys.stderr); sys.exit(1)
    root = tk.Tk()
    Reader(root)
    root.mainloop()


if __name__ == "__main__":
    main()
