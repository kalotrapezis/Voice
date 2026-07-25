"""Greek word completion — no model, no download.

Two sources, in this order:

1. What you actually write. Every dictated phrase and every text you copy is
   counted, so your own vocabulary (names, jargon, that one word you type
   forty times a day) floats to the top and the list gets better with use.
2. The system's hunspell dictionary — ~828k fully inflected Greek forms,
   already on the machine. Never as good a guess as your own words, but it
   means the very first completion works on a fresh install.

Matching is prefix-based: the dictionary is converted once into a sorted
"key\tform" index that we mmap and binary-search, so a lookup costs a handful
of page reads instead of the 380 MB and 3.6 s it took to hold the word list in
Python. Keys are accent-stripped, so "μιλ" finds "μίλησα".
"""
import json
import mmap
import os
import re
import unicodedata

HUNSPELL = "/usr/share/hunspell/el_GR.dic"
HUNSPELL_ENC = "iso-8859-7"
MIN_LEN = 4                 # shorter words are faster to type than to pick
MAX_LEN = 24
WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def _data_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "voice")


DEFAULT_INDEX = os.path.join(_data_dir(), "el_words.idx")


def build_index(path):
    """One-off: hunspell's ISO-8859-7 word list -> sorted 'key\tform' lines."""
    rows = []
    with open(HUNSPELL, encoding=HUNSPELL_ENC, errors="ignore") as f:
        next(f, None)                      # first line is the word count
        seen = set()
        for line in f:
            w = line.split("/", 1)[0].strip()
            if not (MIN_LEN <= len(w) <= MAX_LEN) or not w[:1].islower():
                continue
            k = _key(w)
            if k not in seen:
                seen.add(k)
                rows.append(k + "\t" + w)
    rows.sort()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    os.replace(tmp, path)
    return len(rows)


def _strip_accents(s):
    """ό → ο. Greek is typed with accents but often searched without, and
    'μιλ' should still find 'μίλησα'."""
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c))


def _key(w):
    return _strip_accents(w.lower())


class Predictor:
    """Completions for a half-typed word. Load once, keep it around."""

    def __init__(self, learned_path=None, index_path=None):
        self.learned_path = learned_path
        self.index_path = index_path or DEFAULT_INDEX
        self.counts = {}                  # key -> {surface form: count}
        self._mm = None                   # mmap of the sorted index
        self._loaded = False
        if learned_path and os.path.exists(learned_path):
            try:
                with open(learned_path, encoding="utf-8") as f:
                    self.counts = json.load(f)
            except (OSError, ValueError):
                self.counts = {}

    # ---- dictionary
    def _load(self):
        """Map the prebuilt index. Holding the 828k words as Python strings
        cost 380 MB and 3.6 s per start — as a sorted 'key\\tform' file that
        we mmap and binary-search, it is a few pages of RAM and instant."""
        if self._loaded:
            return
        self._loaded = True                # never retry a missing dictionary
        try:
            if not os.path.exists(self.index_path):
                build_index(self.index_path)
            f = open(self.index_path, "rb")
            self._mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            f.close()
        except OSError:
            self._mm = None

    def _line_start(self, pos):
        """Byte offset of the start of the line containing `pos`."""
        return self._mm.rfind(b"\n", 0, pos) + 1

    def _dict_matches(self, key, limit):
        """Surface forms whose key starts with `key`, in index order."""
        self._load()
        if not self._mm:
            return []
        target = key.encode("utf-8")
        # bisect over line starts: probe the middle byte, snap back to the
        # start of the line it lands in, and move the bound past that whole
        # line. lo is always a line start, which is where the answer begins.
        lo, hi = 0, len(self._mm)
        while lo < hi:
            start = self._line_start((lo + hi) // 2)
            end = self._mm.find(b"\n", start)
            if end < 0:
                break
            if self._mm[start:end].split(b"\t", 1)[0] < target:
                lo = end + 1
            else:
                hi = start
        out = []
        pos = lo
        while pos < len(self._mm) and len(out) < limit:
            end = self._mm.find(b"\n", pos)
            if end < 0:
                break
            k, _, form = self._mm[pos:end].partition(b"\t")
            if not k.startswith(target):
                break
            out.append((k.decode("utf-8"), form.decode("utf-8")))
            pos = end + 1
        return out

    # ---- learning
    def learn(self, text):
        """Count the words of something the user wrote or dictated."""
        changed = False
        for w in WORD_RE.findall(text or ""):
            if len(w) < MIN_LEN:
                continue
            slot = self.counts.setdefault(_key(w), {})
            slot[w] = slot.get(w, 0) + 1
            changed = True
        if changed and self.learned_path:
            self.save()
        return changed

    def save(self):
        os.makedirs(os.path.dirname(self.learned_path), exist_ok=True)
        tmp = self.learned_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.counts, f, ensure_ascii=False)
        os.replace(tmp, self.learned_path)

    # ---- the actual prediction
    def suggest(self, prefix, n=3):
        """Up to n completions for `prefix`, best first, prefix excluded from
        the count of what makes a suggestion useful (a completion equal to
        what is already typed is dropped)."""
        if not prefix or len(prefix) < 2:
            return []
        key = _key(prefix)

        # own vocabulary first, most-used first
        hits = []
        for k, forms in self.counts.items():
            if k.startswith(key) and k != key:
                form, count = max(forms.items(), key=lambda kv: kv[1])
                hits.append((-count, len(form), form))
        hits.sort()
        out = [f for _, _, f in hits[:n]]

        if len(out) < n:                   # top up from the system dictionary
            seen = {_key(w) for w in out}
            for k, form in self._dict_matches(key, n * 8):
                if k == key or k in seen:
                    continue
                seen.add(k)
                out.append(form)
                if len(out) == n:
                    break
        return out

    def completion(self, prefix, choice):
        """The part still missing — what to type to turn prefix into choice.
        Empty when the pick does not actually extend what is typed (different
        accents, say), in which case the caller should replace the word."""
        return choice[len(prefix):] if choice.startswith(prefix) else ""


def demo():
    p = Predictor()
    # learning beats the dictionary, and repetition beats a single sighting
    p.learn("μίλησα με τον Παπαδόπουλο για τη μίλησα μίλησα δουλειά")
    assert p.suggest("μίλ")[0] == "μίλησα", p.suggest("μίλ")
    assert "μίλησα" not in p.suggest("μίλησα")      # already fully typed
    assert p.suggest("μ", 3) == []                  # one letter is too little
    assert p.completion("μίλ", "μίλησα") == "ησα"

    # accents are ignored while matching but kept in what gets inserted
    assert "μίλησα" in p.suggest("μιλ")

    # the system dictionary fills in when nothing has been learned yet
    fresh = Predictor()
    if os.path.exists(HUNSPELL):
        got = fresh.suggest("υπολογ", 3)
        assert got and all(_key(w).startswith("υπολογ") for w in got), got

        # the hand-rolled binary search must agree with a plain scan — an
        # off-by-one in _line_start would silently skip whole prefixes
        fresh._load()
        lines = bytes(fresh._mm).decode("utf-8").splitlines()
        for probe in ("α", "ζω", "καλημ", "υπολογιστ", "ωφελ", "ξζξζ"):
            want = [ln.split("\t")[1] for ln in lines
                    if ln.split("\t")[0].startswith(probe)][:5]
            have = [f for _, f in fresh._dict_matches(probe, 5)]
            assert have == want, (probe, have[:3], want[:3])
    print("ok", p.suggest("μιλ"), fresh.suggest("υπολογ"))


if __name__ == "__main__":
    demo()
