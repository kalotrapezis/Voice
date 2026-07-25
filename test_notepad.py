"""Self-check for the notepad's editing logic (python3 test_notepad.py).

Drives the real Gtk.TextBuffer — the parts worth breaking are the word under
the cursor, what Space replaces, and the counts.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk

import voice_gtk as g


def page():
    win = Gtk.Window()                      # only used as a parent handle
    p = g.NotepadPage(win)
    win.set_child(p)
    return p


def main():
    p = page()

    # the word under the cursor stops at the last non-letter
    p.buf.set_text("γεια σου, γράψ")
    assert p._word_before_cursor()[0] == "γράψ", p._word_before_cursor()[0]

    # …and there is no word right after a space
    p.buf.set_text("γεια ")
    assert p._word_before_cursor()[0] == ""

    # accepting replaces the half-typed word and adds the space itself
    p.buf.set_text("θα γράψ")
    p._accept("γράψω")
    assert p.buf.get_text(*p.buf.get_bounds(), False) == "θα γράψω ", \
        repr(p.buf.get_text(*p.buf.get_bounds(), False))

    # suggestions appear for a real prefix, never for one letter
    p.buf.set_text("υπολογ")
    p._on_changed()
    assert p.suggestions and all(s.startswith("υπολογ") or "υπόλογ" in s
                                 for s in p.suggestions), p.suggestions
    p.buf.set_text("υ")
    p._on_changed()
    assert p.suggestions == []

    # Tab inserts the first suggestion; space stays a plain space
    p.buf.set_text("θα γράψ")
    p._on_changed()
    first = p.suggestions[0]
    assert p._on_key(None, Gdk.KEY_Tab, 0, 0) is True
    assert p.buf.get_text(*p.buf.get_bounds(), False) == f"θα {first} "
    p.buf.set_text("θα γράψ")
    p._on_changed()
    assert p._on_key(None, Gdk.KEY_space, 0, 0) is False   # not swallowed

    # counts, with the selection in parentheses
    p.buf.set_text("ένα δύο τρία")
    p._update_counts()
    assert p.counts.get_label() == "λέξεις 3 · χαρακτήρες 12", \
        p.counts.get_label()
    b = p.buf
    b.select_range(b.get_start_iter(), b.get_iter_at_offset(7))
    p._update_counts()
    assert p.counts.get_label() == "λέξεις 3 (2) · χαρακτήρες 12 (7)", \
        p.counts.get_label()

    # correction fixes the whole note when nothing is selected
    b.set_text("καλημερα ολα καλα")
    b.select_range(b.get_start_iter(), b.get_start_iter())
    p._on_correct(None)
    assert p.buf.get_text(*p.buf.get_bounds(), False) == "καλημέρα όλα καλά", \
        p.buf.get_text(*p.buf.get_bounds(), False)

    print("ok")


if __name__ == "__main__":
    main()
