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
# word<space>count, most frequent first (OpenSubtitles 2018, top 50k). The
# dictionary knows which words exist; only this knows which ones people use.
FREQ_FILES = [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "el_freq.txt"),
              "/usr/lib/voice/data/el_freq.txt"]
HUNSPELL_ENC = "iso-8859-7"
MIN_LEN = 3                 # όλα, όχι, μια — Greek leans on short words
SUGGEST_MIN = 4             # but completing a 3-letter word saves nobody time
MAX_LEN = 24
# one letter counts: "ο" and "η" are words, and they are exactly the context
# that tells άνοιξη from άνοιξε
WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
ALPHABET = "αβγδεζηθικλμνξοπρστυφχψως"          # unaccented: keys are stripped
DICT_SCAN = 400             # rare-word fallback: index rows read per lookup
FREQ_CANDIDATES = 12        # matches pulled off the frequency list per lookup


def _data_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "voice")


DEFAULT_INDEX = os.path.join(_data_dir(), "el_words.v3.idx")


def load_frequencies():
    """{word: count} from the bundled list, {} when it is not installed."""
    for path in FREQ_FILES:
        try:
            freq = {}
            with open(path, encoding="utf-8") as f:
                for line in f:
                    word, _, count = line.partition(" ")
                    if count.strip().isdigit():
                        freq[word] = int(count)
            if freq:
                return freq
        except OSError:
            continue
    return {}


def load_frequencies_ordered():
    """Words from the bundled list, most used first."""
    freq = load_frequencies()
    return sorted(freq, key=freq.get, reverse=True)


def build_index(path):
    """One-off: hunspell's word list -> sorted 'key\tform:count|form:count'.

    All accent variants of a key are kept, ordered by how often the word is
    actually used. Greek has real pairs that differ only in stress
    (καλημέρα/καλήμερα, πότε/ποτέ), and picking by file order meant
    "correcting" a common word into its rare twin."""
    freq = load_frequencies()
    forms = {}
    with open(HUNSPELL, encoding=HUNSPELL_ENC, errors="ignore") as f:
        next(f, None)                      # first line is the word count
        for line in f:
            w = line.split("/", 1)[0].strip()
            if not (MIN_LEN <= len(w) <= MAX_LEN) or not w[:1].islower():
                continue
            variants = forms.setdefault(_key(w), [])
            if w not in variants and len(variants) < 4:
                variants.append(w)
    rows = []
    for k, variants in forms.items():
        ranked = sorted(variants,
                        key=lambda w: (-freq.get(w, 0),) + _variant_rank(w))
        rows.append(k + "\t" + "|".join(f"{w}:{freq.get(w, 0)}"
                                        for w in ranked))
    rows.sort()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    os.replace(tmp, path)
    return len(rows)


def _variant_rank(w):
    """Tie-break accent variants the frequency list has never seen.

    Fewer accent marks wins (the dictionary lists enclitic forms like
    "υπαγόρευσή" beside plain "υπαγόρευση"), then the later stress wins."""
    marks = [i for i, c in enumerate(unicodedata.normalize("NFD", w))
             if unicodedata.combining(c)]
    return (len(marks), -(marks[-1] if marks else 0))


def _strip_accents(s):
    """ό → ο. Greek is typed with accents but often searched without, and
    'μιλ' should still find 'μίλησα'."""
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c))


def _key(w):
    return _strip_accents(w.lower())


def _split_form(entry):
    """'άνοιξη:3475' -> ('άνοιξη', 3475)."""
    word, _, count = entry.rpartition(":")
    return (word, int(count)) if count.isdigit() else (entry, 0)


class Predictor:
    """Completions for a half-typed word. Load once, keep it around."""

    def __init__(self, learned_path=None, index_path=None):
        self.learned_path = learned_path
        self.index_path = index_path or DEFAULT_INDEX
        self.counts = {}                  # key -> {surface form: count}
        self.pairs = {}                   # "prev next" key -> count
        self._mm = None                   # mmap of the sorted index
        self._rank_list = None            # frequency list, most used first
        self._loaded = False
        if learned_path and os.path.exists(learned_path):
            try:
                with open(learned_path, encoding="utf-8") as f:
                    data = json.load(f)
                self.counts = data.get("words", data)      # v1 files were flat
                self.pairs = data.get("pairs", {})
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
        """[(key, [surface forms])] whose key starts with `key`, in order."""
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
            out.append((k.decode("utf-8"),
                        [_split_form(f)
                         for f in form.decode("utf-8").split("|")]))
            pos = end + 1
        return out

    def _ranked(self):
        """The frequency list as [(key, word)], most used first.

        Walking the dictionary alphabetically could not work: "πολ" matches
        5592 keys and "πολύ" — the 42nd most common Greek word — sits behind
        every πόλεμ-/πολιτ- form. Walking 50k words in frequency order finds
        it in the first handful and stops."""
        if self._rank_list is None:
            self._rank_list = [(_key(w), w)
                               for w in load_frequencies_ordered()]
        return self._rank_list

    # ---- learning
    def learn(self, text):
        """Count the words — and the word pairs — of something written here.

        The pairs are what make "η ανοιξ…" propose the noun: frequency lists
        are built from subtitles, where imperatives ("άνοιξε") outrank nouns
        ("άνοιξη") four to one, and no general list knows how *you* write."""
        changed = False
        words = WORD_RE.findall(text or "")
        for i, w in enumerate(words):
            if i:
                pair = _key(words[i - 1]) + " " + _key(w)
                self.pairs[pair] = self.pairs.get(pair, 0) + 1
                changed = True
            if len(w) < SUGGEST_MIN:
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
            json.dump({"words": self.counts, "pairs": self.pairs}, f,
                      ensure_ascii=False)
        os.replace(tmp, self.learned_path)

    # ---- the actual prediction
    def suggest(self, prefix, n=3, prev=None):
        """Up to n completions for `prefix`, best first. `prev` is the word
        before it, which lets pairs seen here outrank raw frequency."""
        if not prefix or len(prefix) < 2:
            return []
        key = _key(prefix)
        pkey = _key(prev) if prev else None

        def pair_count(word):
            return self.pairs.get(f"{pkey} {_key(word)}", 0) if pkey else 0

        # The word may already be typed in full, just without its accent —
        # which is how most people type Greek. Offer the accented spelling as
        # the first completion instead of going silent and leaving the whole
        # note for the AI pass to fix.
        accented = [w for w in self._lookup(key) if w != prefix][:1]

        # own vocabulary first, most-used first, pairs ahead of bare counts
        hits = []
        for k, forms in self.counts.items():
            if k.startswith(key) and k != key:
                form, count = max(forms.items(), key=lambda kv: kv[1])
                hits.append((-pair_count(form), -count, len(form), form))
        hits.sort()
        out = accented + [h[-1] for h in hits[:n] if h[-1] not in accented]
        out = out[:n]

        seen = {_key(w) for w in out}
        if len(out) < n:                   # the common words, by frequency
            pool = []
            for i, (k, word) in enumerate(self._ranked()):
                if not k.startswith(key) or k == key or k in seen:
                    continue
                seen.add(k)
                pool.append((-pair_count(word), i, word))
                if len(pool) >= FREQ_CANDIDATES:
                    break
            pool.sort()
            out += [p[-1] for p in pool[:n - len(out)]]

        if len(out) < n:                   # rare words the list never saw
            for k, forms in self._dict_matches(key, DICT_SCAN):
                if k == key or k in seen:
                    continue
                seen.add(k)
                out.append(forms[0][0])
                if len(out) == n:
                    break
        return out

    def next_words(self, prev, n=3):
        """What usually follows `prev` here — the next word, not a completion.

        Only from pairs seen in this user's own writing: a general frequency
        list would just propose "να", "το", "δεν" after everything."""
        if not prev:
            return []
        head = _key(prev) + " "
        hits = [(-c, k[len(head):]) for k, c in self.pairs.items()
                if k.startswith(head)]
        hits.sort()
        out = []
        for _, key in hits:
            forms = self.counts.get(key) or {}
            word = (max(forms.items(), key=lambda kv: kv[1])[0] if forms
                    else (self._lookup(key) or [key])[0])
            if word not in out:
                out.append(word)
            if len(out) == n:
                break
        return out

    # ---- spelling
    def _lookup(self, key):
        """The dictionary's surface forms for an exact key, most used first
        ([] when the key is unknown)."""
        for k, forms in self._dict_matches(key, 1):
            if k == key:
                return [w for w, _ in forms]
        return []

    def _known(self, key):
        return key in self.counts or bool(self._lookup(key))

    def is_word(self, word):
        """True when `word` stands on its own — a complete word, not a prefix
        someone is still typing."""
        key = _key(word)
        return key in self.counts or bool(self._lookup(key))

    def _edits1(self, key):
        """Every one-edit neighbour of `key` that the dictionary knows."""
        out = []
        for i in range(len(key) + 1):
            a, b = key[:i], key[i:]
            if b:
                out.append(a + b[1:])                       # delete
                if len(b) > 1:
                    out.append(a + b[1] + b[0] + b[2:])     # transpose
            for c in ALPHABET:
                if b:
                    out.append(a + c + b[1:])               # replace
                out.append(a + c + b)                       # insert
        seen, hits = set(), []
        for cand in out:
            if cand == key or cand in seen or len(cand) < MIN_LEN:
                continue
            seen.add(cand)
            if self._known(cand):
                hits.append(cand)
        return hits

    def correct_word(self, word):
        """The word as it should be spelled, or the word itself when it is
        already fine (or when nothing better is known).

        Accents come free: keys are accent-stripped, so 'καλημερα' finds the
        entry whose surface form is 'καλημέρα'."""
        if len(word) < MIN_LEN or not word[:1].islower():
            return word                    # names and short words: hands off
        key = _key(word)
        own = self.counts.get(key)
        if own and word in own:
            return word                    # the user writes it this way
        forms = self._lookup(key)
        if forms:
            if word in forms:
                return word                # a valid spelling of this word
            if own:                        # the user's own accentuation wins
                return max(own.items(), key=lambda kv: kv[1])[0]
            # ponytail: no corpus frequencies, so among accent variants the
            # first is as good a guess as any — learning fixes it with use
            return forms[0]
        if own:                            # learned, never in the dictionary
            return max(own.items(), key=lambda kv: kv[1])[0]
        best, best_score = word, 0
        for cand in self._edits1(key):
            score = sum((self.counts.get(cand) or {}).values()) + 1
            if score > best_score:
                best_score = score
                best = (max(self.counts[cand].items(),
                            key=lambda kv: kv[1])[0] if cand in self.counts
                        else (self._lookup(cand) or [word])[0])
        return best

    def accent_text(self, text):
        """Only put the accents back — never guess at a misspelling.

        For text on its way to the model, a wrong guess is worse than a typo:
        'αθηνα' became 'φθηνά' and the answer was about cheap things. This
        touches a word only when the dictionary knows its exact letters.
        """
        out, pos = [], 0
        for m in WORD_RE.finditer(text or ""):
            word = m.group()
            forms = self._lookup(_key(word))
            fixed = word
            if forms and word not in forms and len(word) >= MIN_LEN \
                    and word[:1].islower():
                fixed = forms[0]
            out.append(text[pos:m.start()])
            out.append(fixed)
            pos = m.end()
        out.append(text[pos:])
        return "".join(out)

    def correct_text(self, text):
        """(corrected text, number of words changed). Punctuation, spacing and
        anything that is not a word are left exactly as they were."""
        changed = 0
        out = []
        pos = 0
        for m in WORD_RE.finditer(text or ""):
            fixed = self.correct_word(m.group())
            out.append(text[pos:m.start()])
            out.append(fixed)
            changed += fixed != m.group()
            pos = m.end()
        out.append(text[pos:])
        return "".join(out), changed

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

    # a fully typed word with no accent gets its accent offered
    if os.path.exists(HUNSPELL):
        assert Predictor().suggest("δουλεια", 3)[0] == "δουλειά"
        assert "δουλειά" not in Predictor().suggest("δουλειά", 3)  # already right

    # a word pair seen here beats the subtitle frequencies: "άνοιξε" is four
    # times more common than "άνοιξη", but not after "η"
    ctx = Predictor()
    assert ctx.suggest("ανοιξ", 3)[0] == "άνοιξε", ctx.suggest("ανοιξ", 3)
    ctx.learn("η άνοιξη ήρθε νωρίς. η άνοιξη μου αρέσει.")
    ctx.learn("να ανοίξω την πόρτα. να ανοίξω το παράθυρο.")
    assert ctx.suggest("ανοιξ", 3, prev="η")[0] == "άνοιξη", \
        ctx.suggest("ανοιξ", 3, prev="η")
    assert ctx.suggest("ανοιξ", 3, prev="να")[0] == "ανοίξω", \
        ctx.suggest("ανοιξ", 3, prev="να")

    # next-word prediction comes from pairs, and stays quiet without them
    nxt = Predictor()
    assert nxt.next_words("η") == []
    nxt.learn("η άνοιξη είναι εδώ. η άνοιξη ήρθε. η θάλασσα λάμπει.")
    assert nxt.next_words("η")[0] == "άνοιξη", nxt.next_words("η")
    assert nxt.next_words("ανύπαρκτη") == []

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
            want = [[_split_form(f) for f in ln.split("\t")[1].split("|")]
                    for ln in lines if ln.split("\t")[0].startswith(probe)][:5]
            have = [f for _, f in fresh._dict_matches(probe, 5)]
            assert have == want, (probe, have[:3], want[:3])
    # spelling: accents come free from the stripped keys, typos need an edit
    if os.path.exists(HUNSPELL):
        assert fresh.correct_word("ελληνικα") == "ελληνικά"
        assert fresh.correct_word("ολα") == "όλα"      # three letters count
        assert fresh.correct_word("καλημερα") == "καλημέρα"   # not καλήμερα
        assert fresh.correct_word("υπαγόρευση") == "υπαγόρευση"  # not -σή
        # both accentuations exist in the dictionary: neither may be rewritten
        assert fresh.correct_word("καλημέρα") == "καλημέρα"
        assert fresh.correct_word("καλήμερα") == "καλήμερα"
        assert fresh.correct_word("καλημερα") in ("καλημέρα", "καλήμερα")
        assert fresh.correct_word("Παπαδόπουλος") == "Παπαδόπουλος"  # a name
        fixed, n = fresh.correct_text("ελληνικα, ολα καλα;")
        assert n == 3 and fixed.endswith(";") and "," in fixed, (fixed, n)
        assert fresh.correct_text("")[0] == ""

        # a word the user really writes is never "corrected" away
        mine = Predictor()
        mine.learn("Κοτρωνάκης κοτρωνάκης κοτρωνάκης")
        assert mine.correct_word("κοτρωνάκης") == "κοτρωνάκης"
    print("ok", p.suggest("μιλ"), fresh.suggest("υπολογ"),
          fresh.correct_text("ελληνικα ολα καλα"))


if __name__ == "__main__":
    demo()
