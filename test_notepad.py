"""Self-check for the notepad's editing logic (python3 test_notepad.py).

Drives the real Gtk.TextBuffer — the parts worth breaking are the word under
the cursor, what Space replaces, and the counts.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk

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

    # Tab walks the suggestions without touching the text, Enter takes one
    p.buf.set_text("θα γράψ")
    p._on_changed()
    assert p.picked == -1                                  # nothing preselected
    assert p._on_key(None, Gdk.KEY_Tab, 0, 0) is True
    assert p.picked == 0
    assert p.buf.get_text(*p.buf.get_bounds(), False) == "θα γράψ"  # untouched
    assert p._on_key(None, Gdk.KEY_Tab, 0, 0) is True
    assert p.picked == 1
    second = p.suggestions[1]
    assert p._on_key(None, Gdk.KEY_Return, 0, 0) is True
    assert p.buf.get_text(*p.buf.get_bounds(), False) == f"θα {second} "
    assert p.suggestions == [] and p.picked == -1

    # Shift+Tab walks back, and wraps around the end of the list
    p.buf.set_text("θα γράψ")
    p._on_changed()
    assert p._on_key(None, Gdk.KEY_ISO_Left_Tab, 0, 0) is True
    assert p.picked == len(p.suggestions) - 1

    # Enter with nothing picked stays a newline, space stays a space
    p.buf.set_text("θα γράψ")
    p._on_changed()
    assert p._on_key(None, Gdk.KEY_Return, 0, 0) is False
    assert p._on_key(None, Gdk.KEY_space, 0, 0) is False

    # Esc drops the suggestions and leaves the half-typed word alone
    assert p._on_key(None, Gdk.KEY_Escape, 0, 0) is True
    assert p.suggestions == []
    assert p.buf.get_text(*p.buf.get_bounds(), False) == "θα γράψ"
    assert p._on_key(None, Gdk.KEY_Tab, 0, 0) is False     # nothing to walk

    # the word before the cursor is passed as context, and learned pairs win
    g.predictor().learn("η άνοιξη ήρθε. η άνοιξη μου αρέσει. να ανοίξω πόρτα.")
    p.buf.set_text("η ανοιξ")
    p._on_changed()
    assert p.suggestions[0] == "άνοιξη", p.suggestions
    p.buf.set_text("να ανοιξ")
    p._on_changed()
    assert p.suggestions[0] == "ανοίξω", p.suggestions

    # the AI continuation sits among the chips and is inserted verbatim,
    # while a dictionary chip replaces the half-typed word
    p.buf.set_text("θέλει δουλειά πολ")
    p._on_changed()
    p.ai_text = "λή"
    p.picked = len(p.suggestions)                      # the ✨ chip
    labels = [lbl for lbl, _ in p._chip_items()]
    assert labels[-1].startswith("✨"), labels
    assert p._on_key(None, Gdk.KEY_Return, 0, 0) is True
    assert p.buf.get_text(*p.buf.get_bounds(), False) == "θέλει δουλειά πολλή"
    assert p.ai_text == "" and p.suggestions == []

    # with nothing typed, the chips become next-word guesses from pairs
    g.predictor().learn("θέλει δουλειά πολλή. θέλει δουλειά ακόμα.")
    p.buf.set_text("θέλει ")
    p._on_changed()
    assert p.suggestions and p.suggestions[0] == "δουλειά", p.suggestions

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

    popover_follows_caret()
    print("ok")


def popover_follows_caret():
    """The suggestion strip must sit ON the caret and below its line.

    Needs a mapped window and one main-loop turn: before that the text view
    has no allocation and every coordinate reads back zero.
    """
    win = Gtk.Window(default_width=600, default_height=400)
    p = g.NotepadPage(win)
    win.set_child(p)
    win.present()
    loop = GLib.MainLoop()
    state = {}

    def once():
        p.buf.set_text("θα γράψ")
        p._on_changed()
        caret = p.view.get_iter_location(
            p.buf.get_iter_at_mark(p.buf.get_insert()))
        state.update(pos=p.overlay_pos, caret=caret,
                     shown=p.chips.get_visible())
        loop.quit()
        return False

    GLib.timeout_add(600, once)
    GLib.timeout_add_seconds(20, loop.quit)
    loop.run()
    win.destroy()
    if not state:                          # no display: nothing to check
        return
    (x, y), caret = state["pos"], state["caret"]
    assert state["shown"], "the strip should be visible with suggestions up"
    assert x == caret.x, (x, caret.x)                    # follows the cursor
    assert y == caret.y + caret.height + g.NotepadPage.CARET_GAP, \
        (y, caret.y, caret.height)                       # and clears the line


if __name__ == "__main__":
    main()
