"""Context-aware correction with a small local LLM (llama.cpp), offline.

The hunspell path in predict.py fixes spelling and accents, but it can only
ever see one word: "τη"/"τι" are both real words, so it must leave them alone.
That is exactly the mistake whisper makes. A model that reads the sentence
can fix it — measured on this machine, gemma-3-4b Q4 turns

    "συζητησαμε τη θα κανουμε με το νεο εργο"   →   "συζητήσαμε τι θα κάνουμε
    με το νέο έργο"

in about 12 s a paragraph. That is a button you press, never something that
runs while you type — and it is gated on RAM like the whisper models, since
2.4 GB of weights on a busy 8 GB machine is how you get a swap storm.
"""
import atexit
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import voice_keyboard as vk
from read_aloud import asset_dir, voice_data_dir

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = asset_dir("models")

# key -> (file, download GB, RAM GB, label, note, url)
LLM_MODELS = {
    "gemma-3-4b-q4": (
        "gemma-3-4b-it-Q4_K_M.gguf", 2.4, 3.9,
        "Gemma 3 4B (q4)",
        "Διορθώνει σύνταξη και νόημα · ~12 δευτ. ανά παράγραφο",
        "https://huggingface.co/bartowski/google_gemma-3-4b-it-GGUF/"
        "resolve/main/google_gemma-3-4b-it-Q4_K_M.gguf"),
}
DEFAULT_MODEL = "gemma-3-4b-q4"

PROMPT = ("Διόρθωσε τα ορθογραφικά και τα συντακτικά λάθη στο ελληνικό "
          "κείμενο. Μην αλλάξεις το νόημα, μην προσθέσεις σχόλια. "
          "Γράψε μόνο το διορθωμένο κείμενο.")
MAX_CHUNK = 700          # characters per call — keeps each round trip short


def binary(name="llama-completion"):
    """Path to a llama.cpp tool, in the checkout or the installed tree."""
    for base in (HERE, "/usr/lib/voice"):
        path = os.path.join(base, "llama.cpp", "build", "bin", name)
        if os.access(path, os.X_OK):
            return path
    return None


def model_path(key=DEFAULT_MODEL):
    return os.path.join(MODELS_DIR, LLM_MODELS[key][0])


def installed(key=DEFAULT_MODEL):
    return os.path.exists(model_path(key))


def available(key=DEFAULT_MODEL):
    return bool(binary()) and installed(key)


def ram_check(key=DEFAULT_MODEL):
    _, dl_gb, need, label, _, _ = LLM_MODELS[key]
    return vk.ram_check(label, need, dl_gb)


def chunks(text, limit=MAX_CHUNK):
    """Split on blank lines, then on sentence ends, so no call is too long."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        while len(para) > limit:
            cut = max(para.rfind(". ", 0, limit), para.rfind("; ", 0, limit),
                      para.rfind("\n", 0, limit))
            cut = cut + 1 if cut > limit // 3 else limit
            out.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            out.append(para)
    return out


def _run(chunk, timeout):
    exe = binary()
    prompt = (f"<start_of_turn>user\n{PROMPT}\n\n{chunk}<end_of_turn>\n"
              "<start_of_turn>model\n")
    res = subprocess.run(
        [exe, "-m", model_path(), "-t", str(vk.THREADS), "-c", "2048",
         "-n", str(min(len(chunk), 1200)), "--temp", "0",
         # -st or it drops into an interactive prompt and never returns
         "-st", "--no-display-prompt", "-p", prompt],
        capture_output=True, text=True, timeout=timeout)
    return res.stdout.replace("[end of text]", "").strip()


def _run_server(chunk, timeout):
    """Same prompt through the warm server — no 3 s model load per chunk."""
    prompt = (f"<start_of_turn>user\n{PROMPT}\n\n{chunk}<end_of_turn>\n"
              "<start_of_turn>model\n")
    body = json.dumps({"prompt": prompt, "n_predict": min(len(chunk), 1200),
                       "temperature": 0}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{_PORT}/completion", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r).get("content", "").strip()


def correct(text, timeout=180, on_progress=lambda done, total: None):
    """Corrected text (the original on any failure — never lose the note)."""
    if not text.strip() or not available():
        return text
    parts = chunks(text)
    # One paragraph is cheaper to run through the CLI than to boot a server
    # for; several are not, since the CLI reloads 2.4 GB every time.
    if len(parts) > 1 and not server_running():
        start_server()
    out = []
    for i, part in enumerate(parts, 1):
        try:
            fixed = (_run_server(part, timeout) if server_running()
                     else _run(part, timeout))
        except (OSError, subprocess.SubprocessError, urllib.error.URLError,
                ValueError):
            fixed = ""
        out.append(fixed or part)
        on_progress(i, len(parts))
    return "\n\n".join(out)


# ---------------------------------------------------------------- server
# Next-word suggestions need the model warm: a fresh llama-completion spends
# ~3 s loading 2.4 GB before it says anything, while a server that already
# holds the weights answers in under 1.5 s.
_SERVER = None
_PORT = None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server_running():
    return _SERVER is not None and _SERVER.poll() is None


def start_server(timeout=120):
    """Boot llama-server on a private port. True once it answers /health."""
    global _SERVER, _PORT
    if server_running():
        return True
    exe = binary("llama-server")
    if not exe or not installed():
        return False
    _PORT = _free_port()
    _SERVER = subprocess.Popen(
        [exe, "-m", model_path(), "-t", str(vk.THREADS),
         "-c", str(SERVER_CTX),
         # three slots so three continuations are generated at once instead
         # of one after the other
         "--parallel", "3",
         "--host", "127.0.0.1", "--port", str(_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atexit.register(stop_server)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _SERVER.poll() is not None:            # died on startup
            return False
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{_PORT}/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    stop_server()
    return False


def stop_server():
    global _SERVER
    if server_running():
        _SERVER.terminate()
        try:
            _SERVER.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _SERVER.kill()
    _SERVER = None


# Gemma 3 itself takes 128k tokens; the limit here is RAM. 16384 across three
# slots leaves ~5400 tokens each, and Greek runs 2-3 characters per token, so
# the verbatim tail is capped well inside that and the rest of the note is
# carried as the outline below.
SERVER_CTX = 16384
CONTEXT_CHARS = 6000
SUMMARY_WORDS = 3


def _tail(text):
    """The last CONTEXT_CHARS, cut back to a sentence start where possible —
    handed half a sentence, the model spends its budget finishing that."""
    tail = text[-CONTEXT_CHARS:]
    if len(text) > CONTEXT_CHARS:
        for mark in (". ", "\n", "; ", "· "):
            cut = tail.find(mark)
            if 0 <= cut < 600:
                return tail[cut + len(mark):]
    return tail


def _end_at_sentence(text):
    """Trim a continuation to its last finished sentence.

    The token budget runs out mid-thought otherwise, and half a sentence is
    worse than a short one: you cannot tell whether to keep typing."""
    text = text.rstrip()
    best = max(text.rfind(m) for m in (".", ";", "!", "·", "?", "…"))
    return text[:best + 1] if best > 0 else text


# ---- outline memory: each paragraph in two or three words, cached by text
_SUMMARIES = {}


def summarize(paragraph, timeout=20):
    """Two or three words for one paragraph, remembered between calls."""
    key = paragraph.strip()[:400]
    if key in _SUMMARIES:
        return _SUMMARIES[key]
    if not server_running() or len(key) < 40:
        return ""
    prompt = (f"<start_of_turn>user\nΠερίγραψε σε {SUMMARY_WORDS} λέξεις το "
              f"θέμα της παραγράφου. Μόνο τις λέξεις.\n\n{key}"
              "<end_of_turn>\n<start_of_turn>model\n")
    # 12 tokens cut Greek words in half ("ειλικ"): ask for more and keep
    # whole words only
    body = json.dumps({"prompt": prompt, "n_predict": 28, "temperature": 0,
                       "stop": ["\n"]}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{_PORT}/completion", body,
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            words = json.load(r).get("content", "").strip().split()
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    summary = " ".join(w for w in words[:SUMMARY_WORDS]
                       if len(w) > 1).strip(" .,·")
    _SUMMARIES[key] = summary
    return summary


def outline(text):
    """'1) θέμα · 2) θέμα' for every paragraph but the one being written."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    notes = []
    for i, para in enumerate(paras[:-1], 1):           # the last one is context
        summary = summarize(para)
        if summary:
            notes.append(f"{i}) {summary}")
    return " · ".join(notes)


def next_words(context, words=8, timeout=30, temperature=0, seed=-1,
               memory=""):
    """What the model would write next — '' when it has nothing or is off.

    A phrase, not a word: the dictionary already completes words, and with a
    warm server eight words cost the same 3-4 s as three. Takes the text
    before the cursor, so it can answer 'στις τρεις το …' with 'απόγευμα'."""
    if not context.strip() or not server_running():
        return ""
    prompt = _tail(context)
    if memory:
        prompt = f"[Το κείμενο ως εδώ: {memory}]\n\n{prompt}"
    body = json.dumps({"prompt": prompt, "n_predict": words * 3,
                       "temperature": temperature, "seed": seed,
                       "stop": ["\n\n", "[Το κείμενο"]}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{_PORT}/completion", body,
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = json.load(r).get("content", "")
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    # Keep whole words only (the model stops mid-word at the token budget)
    # but preserve the leading space, or its absence: asked to continue
    # "δουλειά πολ" the answer is "λή", which must not gain a space.
    lead = text[:len(text) - len(text.lstrip())]
    body = _end_at_sentence(" ".join(text.split()[:words * 2]))
    body = " ".join(body.split()[:words * 2])
    return (lead + body) if body else ""


def next_phrases(context, count=3, words=8, timeout=40):
    """`count` different continuations, generated in parallel.

    Sampled rather than greedy — three greedy runs would return the same
    sentence three times — with the seed varied per slot."""
    if not context.strip() or not server_running():
        return []
    memory = outline(context)              # what the earlier paragraphs said
    out = [None] * count
    threads = []

    def one(i):
        out[i] = next_words(context, words=words, timeout=timeout,
                            temperature=0.8, seed=i + 1, memory=memory)

    for i in range(count):
        t = threading.Thread(target=one, args=(i,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout)
    seen, phrases = set(), []
    for text in out:
        key = (text or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            phrases.append(text)
    return phrases


def demo():
    print("binary:", binary() or "—")
    print("model :", model_path(), "✓" if installed() else "(δεν έχει ληφθεί)")
    print("ram   :", ram_check())

    # half a sentence is worse than a short one
    assert _end_at_sentence("Πρώτη πρόταση. Δεύτερη μισ") == "Πρώτη πρόταση."
    assert _end_at_sentence("χωρίς τελεία") == "χωρίς τελεία"

    # the outline stays quiet with no server, and never summarises the
    # paragraph being written (that one goes in verbatim)
    assert outline("μία παράγραφος μόνο") == ""
    assert summarize("κάτι μικρό") == ""

    # the model reads the tail of the note, cut at a sentence start
    assert _tail("μικρό κείμενο") == "μικρό κείμενο"
    long_note = "Πρώτη πρόταση κομμένη. " + ("Μια πρόταση εδώ. " * 200)
    tail = _tail(long_note)
    assert len(tail) <= CONTEXT_CHARS and not tail.startswith("ταση"), tail[:20]

    # chunking must never lose or reorder text
    long_text = ("Πρώτη πρόταση. " * 40).strip()
    parts = chunks(long_text)
    assert parts and all(len(p) <= MAX_CHUNK for p in parts), [len(p) for p in parts]
    assert "".join(parts).replace(" ", "") == long_text.replace(" ", "")
    assert chunks("μία\n\nδύο") == ["μία", "δύο"]
    assert chunks("   ") == []

    # with no model installed, correct() is a no-op rather than an error
    assert correct("") == ""
    if available():
        fixed = correct("συζητησαμε τη θα κανουμε με το νεο εργο")
        print("δοκιμή:", fixed)
        assert "τι θα" in fixed, fixed

        # a two-paragraph note comes back as two paragraphs, both corrected
        two = correct("καλημερα σας.\n\nολα ειναι ετοιμα.")
        print("δύο παράγραφοι:", repr(two))
        assert two.count("\n\n") == 1, two
        stop_server()
    # the server path: boot once, ask, shut down
    if available() and binary("llama-server"):
        assert next_words("οτιδήποτε") == ""        # nothing without a server
        if start_server():
            got = next_words("Η συνάντηση είναι αύριο στις τρεις το")
            print("συνέχεια:", repr(got))
            assert got and len(got.split()) <= 3, got
            # a half-typed word is continued without gaining a space
            mid = next_words("Για να γυρίσει ο ήλιος θέλει δουλειά πολ")
            print("μισή λέξη:", repr(mid))
            assert not mid.startswith(" "), mid

            # three continuations, generated together and actually different
            t0 = time.time()
            three = next_phrases("Καλημέρα σας, σας γράφω σχετικά με")
            print(f"τρεις προτάσεις σε {time.time() - t0:.1f}s:")
            for ph in three:
                print("   ·", ph.strip())
            assert len(three) >= 2, three
            stop_server()
            assert not server_running()
    print("ok")


if __name__ == "__main__":
    demo()
