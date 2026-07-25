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
FACTS_VERBATIM = 5          # newest details kept exactly as they were said
MAX_FACTS = 20              # personal details carried into new conversations
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
        self.facts = []                   # newest details, verbatim
        self.facts_general = ""           # older ones, generalised to a line
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
        self.facts = data.get("facts", [])
        self.facts_general = data.get("facts_general", "")
        self.summary = data.get("summary", "")

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"chats": self.chats, "notes": self.notes,
                       "facts": self.facts,
                       "facts_general": self.facts_general,
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
        self.learn_facts(said)
        if len(self.notes) > MAX_NOTES:
            self.fold()
        return note

    def learn_facts(self, said):
        """Personal details the user let slip, one per line.

        Two words about the topic are not enough to make later conversations
        personal — the assistant persona promises to remember that you are
        called Teo and write Greek software, so something has to actually
        hold that."""
        out = llm._ask(
            "Γράψε ΜΟΝΟ τα προσωπικά στοιχεία που ανέφερε ο ΧΡΗΣΤΗΣ για τον "
            "εαυτό του. Ένα ανά γραμμή, στη μορφή «χαρακτηριστικό: τιμή» "
            "(π.χ. «όνομα: Τέο», «δουλειά: προγραμματιστής»). Αν δεν "
            f"ανέφερε κανένα, γράψε ΚΑΝΕΝΑ.\n\n{said}",
            n_predict=120, temperature=0)
        added = []
        known = {f.lower() for f in self.facts}
        # ΚΑΝΕΝΑ arrives as a trailing line *beside* real facts, so it is
        # dropped per line — testing it against the whole answer threw the
        # good lines away with it
        for line in (out or "").splitlines():
            fact = line.strip(" -*·•\t")[:120]
            if len(fact) < 3 or "ΚΑΝΕΝΑ" in fact.upper() \
                    or fact.lower() in known:
                continue
            known.add(fact.lower())
            self.facts.append(fact)
            added.append(fact)
        self.generalise_facts()
        return added

    def generalise_facts(self):
        """Keep the last few details word for word, blur the rest into a line.

        "όνομα: Τέο" matters exactly; that he asked about bread in March is
        better remembered as "μαγειρική, λογισμικό" than as five stale lines."""
        if len(self.facts) <= FACTS_VERBATIM:
            return
        old, self.facts = self.facts[:-FACTS_VERBATIM], \
            self.facts[-FACTS_VERBATIM:]
        line = llm._ask(
            "Γενίκευσε τα παρακάτω σε 1-2 σύντομες γραμμές, κρατώντας μόνο "
            "ό,τι περιγράφει σταθερά τον χρήστη.\n\n"
            + (f"Ήδη γνωστά: {self.facts_general}\n" if self.facts_general
               else "") + "\n".join(old),
            n_predict=90, temperature=0)
        merged = line or " · ".join(
            x for x in [self.facts_general] + old if x)
        self.facts_general = merged.strip()[:300]

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
        topics = " · ".join(p for p in parts if p)
        facts = " · ".join(
            x for x in [self.facts_general] + self.facts[-FACTS_VERBATIM:]
            if x)
        out = []
        if facts:
            out.append(f"Ο χρήστης: {facts}")
        if topics:
            out.append(f"Θέματα: {topics}")
        return " | ".join(out)

    def forget(self):
        self.notes = []
        self.facts = []
        self.facts_general = ""
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

    # personal details: the newest stay verbatim, older ones generalise
    again.facts = [f"στοιχείο {i}" for i in range(FACTS_VERBATIM + 4)]
    again.generalise_facts()
    assert len(again.facts) == FACTS_VERBATIM, again.facts
    assert again.facts_general, "τα παλιά δεν γενικεύτηκαν"
    again.facts = ["όνομα: Τέο", "δουλειά: λογισμικό"]
    again.facts_general = ""
    assert "Ο χρήστης: όνομα: Τέο" in again.memory_text()

    again.forget()
    assert again.notes == [] and again.summary == "" and again.facts == []
    assert again.facts_general == ""
    assert again.memory_text() == ""

    # a chat with no turns is never summarised (nothing to say about it)
    empty = again.new_chat()
    assert again.remember(empty["id"]) == ""
    print("ok", path)


if __name__ == "__main__":
    demo()
