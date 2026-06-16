#!/usr/bin/env python3
"""
Clipboard history for VOICE — a lightweight, cross-desktop clipboard manager.

A background watcher polls the system clipboard and records every copy (text or
image). History is browsable/re-copyable from the "Πρόχειρο" tab and the tray
menu. Pinned items and recent history persist to disk, so they survive reboots
(unlike most tray clipboard managers). Images are stored by reference with a
small cached thumbnail — no fancy in-clipboard image editing.

No pip dependencies: clipboard I/O goes through platform_io (xclip / wl-clipboard),
thumbnails use GdkPixbuf (already pulled in by the GTK tray), display uses Tk.
"""
import os
import json
import time
import hashlib
import threading
import urllib.parse

import platform_io

CONFIG_DIR = os.path.expanduser("~/.config/voicekbd")
STORE_PATH = os.path.join(CONFIG_DIR, "clipboard.json")
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "voicekbd", "clips")

MAX_UNPINNED = 60          # pinned items are never evicted
THUMB_PX = 64
POLL_SEC = 1.0
IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".svg")

_TEXT_TARGETS = {"UTF8_STRING", "STRING", "TEXT", "text/plain",
                 "text/plain;charset=utf-8"}


def _sha(data):
    h = hashlib.sha1(data if isinstance(data, bytes) else data.encode("utf-8"))
    return h.hexdigest()[:12]


def make_thumbnail(src_path, dest_path, px=THUMB_PX):
    """Scale an image file to a <=px PNG thumbnail. Returns dest_path or None."""
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new_from_file(src_path)
        w, h = pb.get_width(), pb.get_height()
        if w <= 0 or h <= 0:
            return None
        scale = min(px / w, px / h, 1.0)
        tw, th = max(1, int(w * scale)), max(1, int(h * scale))
        pb.scale_simple(tw, th, GdkPixbuf.InterpType.BILINEAR).savev(
            dest_path, "png", [], [])
        return dest_path
    except Exception:
        return None


class ClipStore:
    """In-memory clipboard history with JSON persistence and change listeners.

    Each entry is a dict: {id, kind('text'|'image'), text, path, thumb, pinned, ts}.
    """

    def __init__(self, path=STORE_PATH):
        self.path = path
        self.items = []
        self._lock = threading.RLock()
        self.listeners = []          # callables() invoked on any change
        self.load()

    # ---- persistence ----
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", [])
            # drop image entries whose files vanished since last run
            self.items = [it for it in items if it.get("kind") != "image"
                          or (it.get("path") and os.path.exists(it["path"]))]
        except Exception:
            self.items = []

    def save(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"items": self.items}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---- listeners ----
    def add_listener(self, fn):
        self.listeners.append(fn)

    def remove_listener(self, fn):
        try:
            self.listeners.remove(fn)
        except ValueError:
            pass

    def _notify(self):
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    # ---- queries ----
    def ordered(self):
        """Pinned first (newest first within each group), then unpinned."""
        with self._lock:
            pins = [i for i in self.items if i.get("pinned")]
            rest = [i for i in self.items if not i.get("pinned")]
            return pins + rest

    # ---- mutations ----
    def add(self, entry):
        """Insert/refresh an entry (dedup by id). Returns True if anything changed."""
        with self._lock:
            existing = next((i for i in self.items if i["id"] == entry["id"]), None)
            if existing:
                # already known — just bump recency, keep its pin state
                if self.items and self.items[0] is existing:
                    return False
                self.items.remove(existing)
                existing["ts"] = time.time()
                self.items.insert(0, existing)
            else:
                entry.setdefault("pinned", False)
                entry["ts"] = time.time()
                self.items.insert(0, entry)
            self._evict()
        self.save()
        self._notify()
        return True

    def _evict(self):
        unpinned = [i for i in self.items if not i.get("pinned")]
        for victim in unpinned[MAX_UNPINNED:]:
            self.items.remove(victim)
            self._drop_files(victim)

    def _drop_files(self, item):
        if item.get("kind") == "image":
            for key in ("thumb", "path"):
                p = item.get(key)
                # only remove files we own (live under our cache dir)
                if p and os.path.commonpath([os.path.abspath(p), CACHE_DIR]) == CACHE_DIR:
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    def set_pinned(self, item_id, pinned):
        with self._lock:
            for i in self.items:
                if i["id"] == item_id:
                    i["pinned"] = pinned
                    break
        self.save()
        self._notify()

    def delete(self, item_id):
        with self._lock:
            it = next((i for i in self.items if i["id"] == item_id), None)
            if it:
                self.items.remove(it)
                self._drop_files(it)
        self.save()
        self._notify()

    def clear(self):
        """Remove everything except pinned items."""
        with self._lock:
            keep = []
            for i in self.items:
                if i.get("pinned"):
                    keep.append(i)
                else:
                    self._drop_files(i)
            self.items = keep
        self.save()
        self._notify()


def recopy(item):
    """Put an entry back on the clipboard. Returns True on success."""
    if item.get("kind") == "image":
        return platform_io.clip_copy_image(item.get("path", ""))
    return platform_io.clip_copy(item.get("text", ""))


def _capture():
    """Inspect the clipboard once and build an entry dict, or None if empty/
    unchanged-uninteresting. Detects raw images, copied image files, and text."""
    targets = platform_io.clip_targets()
    tset = set(targets)
    img_mimes = [t for t in targets if t.startswith("image/")]
    has_uri = "text/uri-list" in tset or "x-special/gnome-copied-files" in tset
    has_text = bool(tset & _TEXT_TARGETS)

    os.makedirs(CACHE_DIR, exist_ok=True)

    # 1) raw image bytes (e.g. a screenshot) — no file path behind it
    if img_mimes and not has_uri:
        mime = "image/png" if "image/png" in img_mimes else img_mimes[0]
        data = platform_io.clip_read_bytes(mime)
        if not data:
            return None
        cid = _sha(data)
        raw = os.path.join(CACHE_DIR, cid + ".png")
        if not os.path.exists(raw):
            # normalise to PNG via GdkPixbuf so re-copy + thumb are uniform
            tmp = os.path.join(CACHE_DIR, cid + ".raw")
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                if not make_thumbnail(tmp, raw, px=10**6):   # huge px = full size
                    os.replace(tmp, raw)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        thumb = os.path.join(CACHE_DIR, cid + "_t.png")
        if not os.path.exists(thumb):
            make_thumbnail(raw, thumb)
        return {"id": cid, "kind": "image", "path": raw,
                "thumb": thumb if os.path.exists(thumb) else None,
                "text": "🖼 εικόνα από το πρόχειρο"}

    # 2) a copied file — if it's a single image file, show it as an image
    if has_uri:
        raw = platform_io.clip_read_bytes(
            "text/uri-list" if "text/uri-list" in tset else "x-special/gnome-copied-files")
        uris = [ln.strip() for ln in raw.decode("utf-8", "ignore").splitlines()
                if ln.strip() and not ln.startswith("copy")]
        paths = [urllib.parse.unquote(u[7:]) for u in uris if u.startswith("file://")]
        paths = [os.path.realpath(p) for p in paths]
        imgs = [p for p in paths if p.lower().endswith(IMG_EXTS) and os.path.exists(p)]
        if len(paths) == 1 and imgs:
            src = imgs[0]
            cid = _sha("f:" + src)
            thumb = os.path.join(CACHE_DIR, cid + "_t.png")
            if not os.path.exists(thumb):
                make_thumbnail(src, thumb)
            return {"id": cid, "kind": "image", "path": src,
                    "thumb": thumb if os.path.exists(thumb) else None,
                    "text": src}
        if paths:                          # non-image file(s) → store as text path(s)
            txt = "\n".join(paths)
            return {"id": _sha("t:" + txt), "kind": "text", "text": txt}

    # 3) plain text
    if has_text:
        txt = platform_io.clip_read()
        if txt and txt.strip():
            return {"id": _sha("t:" + txt), "kind": "text", "text": txt}
    return None


class ClipWatcher:
    """Background thread that records clipboard changes into a ClipStore."""

    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self._last = None
        self._thread = None

    def start(self):
        if not (platform_io._has("xclip") or platform_io._has("xsel")
                or platform_io._has("wl-paste")):
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self):
        while not self.stop.is_set():
            try:
                entry = _capture()
                if entry and entry["id"] != self._last:
                    self._last = entry["id"]
                    self.store.add(entry)
            except Exception:
                pass
            self.stop.wait(POLL_SEC)
