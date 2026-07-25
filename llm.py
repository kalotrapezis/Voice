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
import os
import re
import subprocess

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


def binary():
    """Path to llama-completion, in the checkout or in the installed tree."""
    for base in (HERE, "/usr/lib/voice"):
        path = os.path.join(base, "llama.cpp", "build", "bin",
                            "llama-completion")
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


def correct(text, timeout=180, on_progress=lambda done, total: None):
    """Corrected text (the original on any failure — never lose the note)."""
    if not text.strip() or not available():
        return text
    parts = chunks(text)
    out = []
    for i, part in enumerate(parts, 1):
        try:
            fixed = _run(part, timeout)
        except (OSError, subprocess.SubprocessError):
            fixed = ""
        out.append(fixed or part)
        on_progress(i, len(parts))
    return "\n\n".join(out)


def demo():
    print("binary:", binary() or "—")
    print("model :", model_path(), "✓" if installed() else "(δεν έχει ληφθεί)")
    print("ram   :", ram_check())

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
    print("ok")


if __name__ == "__main__":
    demo()
