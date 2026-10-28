# Άδειες τρίτων

Το VOICE χρησιμοποιεί και μοιράζει τα παρακάτω έργα. Τα ευχαριστούμε. Τα κείμενα των αδειών είναι στον φάκελο
[`licenses/`](licenses).

## Μοντέλα

| Μοντέλο | Δημιουργός | Άδεια | Τι αλλάξαμε |
|---|---|---|---|
| **Meta MMS-TTS, ελληνικά** ([facebook/mms-tts-ell](https://huggingface.co/facebook/mms-tts-ell)), η ελληνική φωνή | Meta AI | [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) | Μετατράπηκε σε ONNX χωρίς αλλαγή στα βάρη. Η εφαρμογή προσθέτει κενά γύρω από το κείμενο και γράφει τους αριθμούς ως λέξεις πριν το διαβάσει. |
| **Whisper Medium** (μέσω [whisper.cpp](https://github.com/ggml-org/whisper.cpp)), η αναγνώριση ομιλίας | OpenAI | MIT ([`licenses/whisper-MIT.txt`](licenses/whisper-MIT.txt)) | Κβάντιση q8_0 με το `whisper-quantize`. |
| **Μοντέλα μετάφρασης του Firefox**, ελληνικά → αγγλικά και αγγλικά → ελληνικά ([mozilla/translations](https://github.com/mozilla/translations)) | Mozilla | MPL 2.0 ([`licenses/MPL-2.0.txt`](licenses/MPL-2.0.txt)) | Καμία· μόνο αποσυμπιέστηκαν. Η πηγή τους είναι ελεύθερα διαθέσιμη στον σύνδεσμο. |

**Η φωνή MMS επιτρέπεται μόνο για μη εμπορική χρήση.** Το VOICE μοιράζεται δωρεάν· αν κάποιος θέλει να το
χρησιμοποιήσει εμπορικά, πρέπει να αφαιρέσει τη φωνή MMS (η εφαρμογή δουλεύει και με το Piper ή με τις φωνές της
Mistral / OpenAI).

Η φωνή MMS προέρχεται από τη δημοσίευση: V. Pratap, A. Tjandra, B. Shi κ.ά., *Scaling Speech Technology to 1,000+
Languages*, 2023, [arXiv:2305.13516](https://arxiv.org/abs/2305.13516).

## Λογισμικό που περιέχουν τα πακέτα

| Έργο | Άδεια |
|---|---|
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) και ggml | MIT ([`licenses/whisper.cpp-MIT.txt`](licenses/whisper.cpp-MIT.txt)) |
| [bergamot-translator](https://github.com/browsermt/bergamot-translator) (η μηχανή της μετάφρασης) | MPL 2.0 ([`licenses/MPL-2.0.txt`](licenses/MPL-2.0.txt)) |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) (τρέχει τη φωνή MMS) | MIT ([`licenses/onnxruntime-MIT.txt`](licenses/onnxruntime-MIT.txt)) |
| [pdf.js](https://github.com/mozilla/pdf.js) (ανοίγει τα PDF) | Apache 2.0 ([`licenses/pdf.js-Apache-2.0.txt`](licenses/pdf.js-Apache-2.0.txt)) |
| [Electron](https://www.electronjs.org/) | MIT (μέσα στο πακέτο, `LICENSE.electron.txt` και `LICENSES.chromium.html`) |
| whisper.cpp για Windows, η [επίσημη έκδοση](https://github.com/ggml-org/whisper.cpp/releases) `b5454` | MIT ([`licenses/whisper.cpp-MIT.txt`](licenses/whisper.cpp-MIT.txt)) |
| Microsoft Visual C++ Runtime (`msvcp140*.dll`, `vcruntime140*.dll`, μόνο στο πακέτο Windows) | αρχεία που η Microsoft επιτρέπει να διανέμονται μαζί με εφαρμογές (Visual Studio 2022, «Distributable Code»), από τον επίσημο `vc_redist.x64.exe` |
| Εικονίδια [Material Symbols](https://fonts.google.com/icons) | Apache 2.0 |

Υπηρεσίες cloud (Mistral, OpenAI) χρησιμοποιούνται μόνο αν βάλετε δικό σας κλειδί, με τους όρους της κάθε υπηρεσίας.
