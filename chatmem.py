"""Bob's history and long-term memory, on disk.

Three layers, deliberately different sizes:

* **chat** — the turns of one conversation, kept verbatim while it is open.
* **note** — two words per finished conversation. Written when you leave the
  chat, not while you type, because summarising costs a model call.
* **memory** — all the notes. Once they pass MAX_NOTES they are folded into a
  single sentence, so memory grows sideways instead of forever.

The notes and the folded sentence are what future conversations get: a couple
of hundred characters that say what you have talked about, rather than a
transcript nobody has room for.
"""
import json
import os
import time

import llm
from read_aloud import voice_data_dir

STORE = os.path.join(voice_data_dir(), "chats.json")
MAX_NOTES = 25              # notes kept before they are folded into one line
NOTE_WORDS = 2
TITLE_CHARS = 40


def _now():
    return int(time.time())


class Store:
    """The whole history file. Small enough to load and save as one blob."""

    def __init__(self, path=STORE):
        self.path = path
        self.chats = []                   # newest first
        self.notes = []                   # ["δύο λέξεις", ...]
        self.summary = ""
        self.load()

    # ---- disk
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        self.chats = data.get("chats", [])
        self.notes = data.get("notes", [])
        self.summary = data.get("summary", "")

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"chats": self.chats, "notes": self.notes,
                       "summary": self.summary}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ---- chats
    def new_chat(self):
        """A blank conversation — reusing an existing empty one if there is
        already one waiting, or every app start would leave a stub behind."""
        for chat in self.chats:
            if not chat["turns"]:
                chat["ts"] = _now()
                return chat
        chat = {"id": str(_now()) + str(len(self.chats)), "turns": [],
                "title": "Νέα συνομιλία", "note": "", "ts": _now()}
        self.chats.insert(0, chat)
        return chat

    def get(self, chat_id):
        for chat in self.chats:
            if chat["id"] == chat_id:
                return chat
        return None

    def update(self, chat_id, turns):
        chat = self.get(chat_id)
        if chat is None:
            return
        chat["turns"] = [list(t) for t in turns]
        chat["ts"] = _now()
        if turns and chat["title"] == "Νέα συνομιλία":
            chat["title"] = turns[0][1][:TITLE_CHARS].strip() or chat["title"]

    def drop(self, chat_id):
        self.chats = [c for c in self.chats if c["id"] != chat_id]

    # ---- memory
    def remember(self, chat_id):
        """Two words for this conversation, added to memory. Needs the model,
        so it runs when a chat is closed rather than on every message."""
        chat = self.get(chat_id)
        if not chat or not chat["turns"] or chat.get("note"):
            return ""
        said = "\n".join(f"{'Χρήστης' if r == 'user' else 'Βοηθός'}: {t}"
                         for r, t in chat["turns"][:12])
        note = llm._ask(
            f"Περίγραψε σε {NOTE_WORDS} λέξεις για τι μίλησαν. Μόνο τις "
            f"λέξεις.\n\n{said}", n_predict=24, temperature=0)
        note = " ".join(w for w in note.split()[:NOTE_WORDS]
                        if len(w) > 1).strip(" .,·")
        if not note:
            return ""
        chat["note"] = note
        self.notes.append(note)
        if len(self.notes) > MAX_NOTES:
            self.fold()
        return note

    def fold(self):
        """All the notes become one sentence — memory has to stay bounded."""
        joined = " · ".join(self.notes)
        line = llm._ask(
            "Γράψε ΜΙΑ πρόταση που να συνοψίζει όλα τα θέματα.\n\n"
            + (f"Ως τώρα: {self.summary}\n" if self.summary else "")
            + joined, n_predict=120, temperature=0)
        self.summary = (line or (self.summary + " " + joined)).strip()[:600]
        self.notes = []

    def memory_text(self):
        """What a new conversation is told about the old ones."""
        parts = ([self.summary] if self.summary else []) + self.notes[-12:]
        return " · ".join(p for p in parts if p)

    def forget(self):
        self.notes = []
        self.summary = ""
        for chat in self.chats:
            chat["note"] = ""


def demo():
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "chats.json")
    st = Store(path)

    chat = st.new_chat()
    assert chat["title"] == "Νέα συνομιλία"
    assert st.new_chat() is chat            # no stub per app start
    st.update(chat["id"], [("user", "Πώς φτιάχνω μουσακά με μελιτζάνες;"),
                           ("model", "Μάλλον… δεν είμαι σίγουρος.")])
    assert st.get(chat["id"])["title"].startswith("Πώς φτιάχνω")
    st.save()

    # it survives a reload, which is the whole point of the file
    again = Store(path)
    assert len(again.chats) == 1 and again.chats[0]["turns"], again.chats

    # memory folds instead of growing without limit
    again.notes = [f"θέμα {i}" for i in range(MAX_NOTES + 1)]
    again.summary = ""
    if not llm.server_running():           # no model: fold still bounds it
        again.fold()
        assert again.notes == [] and again.summary
    assert len(again.memory_text()) < 700

    again.forget()
    assert again.notes == [] and again.summary == ""
    assert again.memory_text() == ""

    # a chat with no turns is never summarised (nothing to say about it)
    empty = again.new_chat()
    assert again.remember(empty["id"]) == ""
    print("ok", path)


if __name__ == "__main__":
    demo()
