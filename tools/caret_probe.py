#!/usr/bin/env python3
"""AT-SPI caret probe — does VOICE know where you are typing, and what?

Run it, then type in a few apps (KWrite, Firefox, LibreOffice, a terminal,
Claude Desktop…). For every keystroke it prints the app, the caret's screen
rectangle and the half-finished word. What it prints here is exactly what a
word-prediction bar could use; what it stays silent about is an app that
would never work.

    python3 tools/caret_probe.py
"""
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

WORD = " \t\n.,;:!·«»\"'()[]{}"


def report(ev):
    try:
        acc = ev.source
        app = acc.get_application().get_name()
        text = acc.get_text_iface()
        if text is None:
            return
        off = text.get_caret_offset()
        r = text.get_character_extents(max(off - 1, 0), Atspi.CoordType.SCREEN)
        n = text.get_character_count()
        before = text.get_text(max(0, off - 40), off)
        word = ""
        for ch in reversed(before):
            if ch in WORD:
                break
            word = ch + word
        print(f"{app:<22} caret x={r.x:<5} y={r.y:<5} h={r.height:<3} "
              f"chars={n:<5} word={word!r}")
    except Exception as e:                       # app exposes no usable text
        print("  (no text interface —", type(e).__name__, e, ")")


def main():
    Atspi.init()
    for kind in ("object:text-caret-moved", "object:text-changed:insert"):
        Atspi.EventListener.new(report).register(kind)
    print("Τύπωσε κάπου… (Ctrl+C για τερματισμό)\n")
    try:
        Atspi.event_main()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
