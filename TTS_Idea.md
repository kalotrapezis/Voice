Good news — everything is confirmed. Here is the full picture.

* * *

**The right engine for you: Piper TTS**

Piper is a fast, local neural TTS system that runs entirely offline with no cloud services, supporting over 50 languages through downloadable voice models. And critically for your case — there is already a Greek voice model for Piper, called `el_GR-rapunzelina-low`, available for free download. For English there are many voices to choose from, including high-quality ones.

Kokoro (the other strong candidate) does not support Greek at all — Kokoro supports English, French, Hindi, Spanish, Japanese, Chinese, Italian, and Portuguese — so Piper is the clear choice for your bilingual use case.

* * *

**How the whole thing works:**

Think of Piper as a small engine that takes text and outputs an audio file. On its own it's command-line only, which is not useful for a teacher. What you need on top of it is a simple Python window — a text box, a language selector (Greek / English), a speed slider, and a Play button. When you press Play, Python feeds your pasted text to Piper, Piper generates the audio, and your speakers play it. Everything stays on your machine, no internet needed after the initial setup.

The voice models come as two files — a `.onnx` file and a `.onnx.json` config file — which you download once from HuggingFace and place in a folder. After that, the app runs completely offline.

* * *

**What I can build for you:**

A single Python file with a clean GUI that:

* Has a large text paste area
* Lets you switch between Greek and English voice
* Has a speed control slider
* Has Play and Stop buttons
* Works on Windows 7–11 (which matters for your school PCs)

The only setup required on your end would be installing Python, Piper, and downloading two small model files — I can write a step-by-step guide alongside the code.

Want me to go ahead and build it?
