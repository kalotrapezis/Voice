#!/usr/bin/env python3
"""
Cross-desktop / cross-session helpers for VOICE.

Abstracts the parts that differ between the original target (KDE Plasma on
Wayland — Fedora KDE, Kubuntu) and X11 desktops like Cinnamon (Linux Mint) or
GNOME, so the same app runs everywhere:

  - clipboard copy / read   → wl-clipboard (Wayland) vs xclip / xsel (X11)
  - the "paste" keystroke    → ydotool (Wayland uinput) vs xdotool (X11)
  - listing & activating windows → KWin WindowsRunner (Plasma) vs wmctrl (X11)

Selection is by session type + desktop, with a tool-availability fallback so a
missing utility degrades gracefully instead of crashing the app.
"""
import os
import re
import shutil
import subprocess

SESSION = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
DESKTOP = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
IS_WAYLAND = SESSION == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))
IS_X11 = (not IS_WAYLAND) and (SESSION == "x11" or bool(os.environ.get("DISPLAY")))
IS_KDE = "kde" in DESKTOP or "plasma" in DESKTOP


def _has(cmd):
    return shutil.which(cmd) is not None


def _run(cmd, timeout=5, **kw):
    return subprocess.run(cmd, timeout=timeout, **kw)


# ============ clipboard ============
def clip_copy(text):
    """Put text on the system clipboard. Returns True on success."""
    data = text.encode()
    cmds = []
    if IS_WAYLAND and _has("wl-copy"):
        cmds.append(["wl-copy"])
    if _has("xclip"):
        cmds.append(["xclip", "-selection", "clipboard"])
    if _has("xsel"):
        cmds.append(["xsel", "--clipboard", "--input"])
    if _has("wl-copy"):                       # last resort (e.g. XWayland)
        cmds.append(["wl-copy"])
    for c in cmds:
        try:
            _run(c, input=data)
            return True
        except Exception:
            continue
    return False


def clip_read(primary=False):
    """Read the clipboard (or the PRIMARY/X selection). '' on failure."""
    cmds = []
    if IS_WAYLAND and _has("wl-paste"):
        cmds.append(["wl-paste", "--primary"] if primary else ["wl-paste"])
    if _has("xclip"):
        sel = "primary" if primary else "clipboard"
        cmds.append(["xclip", "-selection", sel, "-o"])
    if _has("xsel"):
        cmds.append(["xsel", "--primary", "--output"] if primary
                    else ["xsel", "--clipboard", "--output"])
    if _has("wl-paste"):
        cmds.append(["wl-paste", "--primary"] if primary else ["wl-paste"])
    for c in cmds:
        try:
            return _run(c, capture_output=True, text=True).stdout.rstrip("\n")
        except Exception:
            continue
    return ""


def clip_targets():
    """MIME targets currently on the clipboard — used to tell images from text."""
    try:
        if IS_WAYLAND and _has("wl-paste"):
            out = _run(["wl-paste", "--list-types"], capture_output=True,
                       text=True).stdout
        elif _has("xclip"):
            out = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
                       capture_output=True, text=True).stdout
        else:
            return []
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
    except Exception:
        return []


def clip_read_bytes(mime):
    """Raw clipboard bytes for a MIME type (e.g. 'image/png'), b'' on failure."""
    try:
        if IS_WAYLAND and _has("wl-paste"):
            return _run(["wl-paste", "-t", mime], capture_output=True).stdout
        if _has("xclip"):
            return _run(["xclip", "-selection", "clipboard", "-t", mime, "-o"],
                        capture_output=True).stdout
    except Exception:
        pass
    return b""


def clip_copy_image(path):
    """Put a PNG file's bytes on the clipboard as image/png. True on success."""
    try:
        data = open(path, "rb").read()
    except Exception:
        return False
    if IS_WAYLAND and _has("wl-copy"):
        try:
            _run(["wl-copy", "--type", "image/png"], input=data); return True
        except Exception:
            pass
    if _has("xclip"):
        try:
            _run(["xclip", "-selection", "clipboard", "-t", "image/png", "-i"],
                 input=data); return True
        except Exception:
            pass
    return False


# ============ paste keystroke (Ctrl+V) ============
def send_paste():
    """Simulate Ctrl+V into the currently focused window. True on success."""
    # ydotool talks to /dev/uinput → works under Wayland; xdotool is X11-only.
    if IS_WAYLAND and _has("ydotool"):
        try:
            _run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"], timeout=10)
            return True
        except Exception:
            pass
    if _has("xdotool"):
        try:
            _run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], timeout=10)
            return True
        except Exception:
            pass
    if _has("ydotool"):                       # X11 box without xdotool
        try:
            _run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"], timeout=10)
            return True
        except Exception:
            pass
    return False


# ============ window listing & activation ============
# KWin WindowsRunner (krunner D-Bus) — available under Plasma on X11 *and*
# Wayland. wmctrl is the portable X11 fallback (Cinnamon, GNOME-on-X11, etc.).
_KWIN = ["gdbus", "call", "--session", "--dest", "org.kde.KWin",
         "--object-path", "/WindowsRunner", "--method"]
_KWIN_RE = re.compile(r"\('(0_\{[^}]+\})',\s*'((?:[^'\\]|\\.)*)',\s*'((?:[^'\\]|\\.)*)'")
_SKIP = {"xwaylandvideobridge", "plasmashell", ""}


def window_backend():
    """'kwin', 'wmctrl', or None — whichever can drive windows here."""
    if IS_KDE and _has("gdbus"):              # Plasma: works on X11 + Wayland
        return "kwin"
    if _has("wmctrl"):                         # portable X11 fallback
        return "wmctrl"
    return None


def list_windows():
    """[(key, 'title  ·  app'), ...] of open windows. `key` is an opaque id to
    pass back to activate_target(). Empty list if windows can't be enumerated."""
    be = window_backend()
    if be == "kwin":
        return _kwin_list()
    if be == "wmctrl":
        return _wmctrl_list()
    return []


def activate_target(key):
    """Focus the window identified by `key` (from list_windows). True if an
    activation was issued."""
    be = window_backend()
    if be == "kwin":
        mid = _kwin_resolve(key)
        if mid:
            _kwin_activate(mid)
            return True
        return False
    if be == "wmctrl":
        try:
            return _run(["wmctrl", "-x", "-a", key]).returncode == 0
        except Exception:
            return False
    return False


# ---- KWin (Plasma) ----
def _kwin_list():
    try:
        out = _run(_KWIN + ["org.kde.krunner1.Match", ""],
                   capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return []
    wins, seen = [], set()
    for _mid, title, appcls in _KWIN_RE.findall(out):
        title = title.replace("\\'", "'")
        if appcls in _SKIP or appcls in seen:
            continue
        seen.add(appcls)
        wins.append((appcls, f"{title}  ·  {appcls}"))
    return wins


def _kwin_resolve(appclass):
    try:
        out = _run(_KWIN + ["org.kde.krunner1.Match", appclass],
                   capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return None
    for mid, _title, appcls in _KWIN_RE.findall(out):
        if appcls == appclass:
            return mid
    m = re.search(r"0_\{[^}]+\}", out)
    return m.group(0) if m else None


def _kwin_activate(match_id):
    _run(_KWIN + ["org.kde.krunner1.Run", match_id, ""],
         capture_output=True, timeout=4)


# ---- wmctrl (X11) ----
def _wmctrl_list():
    try:
        out = _run(["wmctrl", "-lx"], capture_output=True, text=True).stdout
    except Exception:
        return []
    wins, seen = [], set()
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        wmclass = parts[2]                    # "instance.Class"
        title = parts[4] if len(parts) >= 5 else ""
        cls = wmclass.split(".")[-1]
        if cls.lower() in _SKIP or wmclass in seen:
            continue
        seen.add(wmclass)
        wins.append((wmclass, f"{title}  ·  {cls}"))
    return wins
