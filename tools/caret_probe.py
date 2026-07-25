#!/usr/bin/env python3
"""AT-SPI caret probe — does VOICE know where you are typing, and what?

Run it, then type in a few apps (KWrite, Firefox, LibreOffice, a terminal,
Claude Desktop…). For every keystroke it prints the app, the caret's screen
rectangle and the half-finished word. What it prints here is exactly what a
word-prediction bar could use; what it stays silent about is an app that
would never work.

    python3 tools/caret_probe.py
"""
import os
import signal
import subprocess

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

WORD = " \t\n.,;:!·«»\"'()[]{}"


def report(ev):
    try:
        acc = ev.source
        app = acc.get_application().get_name()
        text = acc.get_text_iface()
        if text is None:
            return
        # call through Atspi.Text explicitly: text.get_text(...) resolves to
        # the deprecated one-argument Atspi.Accessible.get_text instead
        off = Atspi.Text.get_caret_offset(text)
        r = Atspi.Text.get_character_extents(text, max(off - 1, 0),
                                             Atspi.CoordType.SCREEN)
        n = Atspi.Text.get_character_count(text)
        before = Atspi.Text.get_text(text, max(0, off - 40), off)
        word = ""
        for ch in reversed(before):
            if ch in WORD:
                break
            word = ch + word
        print(f"{app:<22} caret x={r.x:<5} y={r.y:<5} h={r.height:<3} "
              f"chars={n:<5} word={word!r}")
    except Exception as e:                       # app exposes no usable text
        seen = getattr(report, "_quiet", set())
        key = (type(e).__name__, str(e)[:60])
        if key not in seen:                      # once per failure, not per key
            seen.add(key)
            report._quiet = seen
            print("  (χωρίς κείμενο —", *key, ")")


def main():
    # Atspi.event_main() runs a GLib loop that swallows Ctrl+C: Python's
    # handler only runs between bytecodes and the loop never returns to the
    # interpreter. Catch SIGINT in GLib itself instead.
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT,
                         lambda *_: (Atspi.event_quit(), False)[1])

    if not os.environ.get("GTK_A11Y") and not accessibility_on():
        print("⚠  Η προσβασιμότητα (AT-SPI) είναι κλειστή — χωρίς αυτήν καμία\n"
              "   εφαρμογή δεν λέει τι γράφεις. Άνοιξέ την και ξανατρέξε:\n\n"
              "   gsettings set org.gnome.desktop.interface "
              "toolkit-accessibility true\n")

    Atspi.init()
    for kind in ("object:text-caret-moved", "object:text-changed:insert"):
        Atspi.EventListener.new(report).register(kind)
    print("Τύπωσε κάπου… (Ctrl+C για τερματισμό)\n")
    Atspi.event_main()


def accessibility_on():
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface",
             "toolkit-accessibility"],
            capture_output=True, text=True, timeout=5).stdout
        return "true" in out
    except (OSError, subprocess.SubprocessError):
        return False


if __name__ == "__main__":
    main()
