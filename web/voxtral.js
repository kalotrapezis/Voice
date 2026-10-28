// DOM-free core shared by every shell: audio math, WAV, the Voxtral API, VAD, TTS chunks.
// Ported from voice_keyboard.py / voice_gtk.py (MeetingPage) so both apps behave alike.
// Loaded as a plain <script>; node's test (test.js) loads it through module.exports.

const SR = 16000;                 // Hz, mono Int16 everywhere
const FRAME = 480;                // 30 ms
const CHUNK = 25 * SR;            // meeting/dictation chunk, in samples
const CUT_WINDOW = 5 * SR;        // cut on the quietest frame of the chunk's last 5 s
// both speak the same REST shape; each needs its own key from its own console
const PROVIDERS = {
  // on this device: whisper.cpp on the desktop (models listed by main.js), Android's own recognizer on a tablet
  local: { name: 'Τοπικά', models: [['medium', 'Whisper Medium'], ['small', 'Whisper Small'], ['large', 'Whisper Large v3']] },
  mistral: { name: 'Mistral', api: 'https://api.mistral.ai/v1', chat: 'mistral-small-latest', console: 'console.mistral.ai → API Keys',
    models: [['voxtral-mini-transcribe-3', 'Voxtral Mini'], ['voxtral-small-transcribe-3', 'Voxtral Small']] },
  openai: { name: 'OpenAI', api: 'https://api.openai.com/v1', chat: 'gpt-4o-mini', console: 'platform.openai.com → API keys',
    models: [['gpt-4o-mini-transcribe', 'GPT-4o mini'], ['gpt-4o-transcribe', 'GPT-4o']] },
};
const DEFAULT_TERMS = 'jamovi, SPSS, t-test, ANOVA, Likert, Levene, Shapiro-Wilk, p-value, Cronbach\'s alpha';

function rms(pcm, from, to) {
  let s = 0;
  for (let i = from; i < to; i++) s += pcm[i] * pcm[i];
  return to > from ? Math.sqrt(s / (to - from)) : 0;
}

/** Sample index to cut a full chunk at: the quietest 30 ms frame in its last `win` samples. */
function quietestCut(pcm, chunk = CHUNK, win = CUT_WINDOW) {
  let best = chunk, bestR = Infinity;
  for (let o = chunk - win; o + FRAME <= chunk; o += FRAME) {
    const r = rms(pcm, o, o + FRAME);
    if (r < bestR) { bestR = r; best = o; }
  }
  return best;
}

/** Digital silence (an idle speaker monitor) only invites invented text from the model. */
function isSilent(pcm) {
  for (let o = 0; o + FRAME <= pcm.length; o += FRAME) if (rms(pcm, o, o + FRAME) >= 150) return false;
  return true;
}

function concat(parts) {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Int16Array(n);
  n = 0;
  for (const p of parts) { out.set(p, n); n += p.length; }
  return out;
}

function wav(pcm) {
  const b = new ArrayBuffer(44 + pcm.length * 2), v = new DataView(b);
  const s = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
  s(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); s(8, 'WAVE'); s(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, SR, true); v.setUint32(28, SR * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  s(36, 'data'); v.setUint32(40, pcm.length * 2, true);
  new Int16Array(b, 44).set(pcm);
  return b;
}

function stamp(sec) {
  sec = Math.max(0, Math.floor(sec));
  const p = n => String(n).padStart(2, '0');
  return `${p(Math.floor(sec / 3600))}:${p(Math.floor(sec / 60) % 60)}:${p(sec % 60)}`;
}

function terms(text) {
  return (text || '').split(',').map(t => t.trim()).filter(Boolean).slice(0, 100);
}

/**
 * One request to Voxtral. Dictation: language=el. Diarize: speaker segments; the API rejects
 * `language` together with timestamps, so diarized requests auto-detect the language.
 */
// Offline models the desktop downloads once, from the project's own models repository so the links
// never change under us (a file's path becomes its asset name, '/' → '__'). Translation is not here:
// its 42 MB ship inside every package.
const MODEL_URL = 'https://github.com/kalotrapezis/voice-models/releases/download/v1/';
const MODEL_PACKS = [
  { id: 'whisper', name: 'Αναγνώριση ομιλίας: Whisper Medium', note: 'Γράφει ό,τι λέτε, χωρίς internet. Με κάρτα γραφικών ~1–2 s ανά φράση.',
    files: [{ path: 'ggml-medium-q8_0.bin', size: 823369779, sha256: '42a1ffcbe4167d224232443396968db4d02d4e8e87e213d3ee2e03095dea6502' }] },
  { id: 'voice', name: 'Ελληνική φωνή: Meta MMS', note: 'Διαβάζει κείμενα φωναχτά, χωρίς internet. Άδεια μόνο για μη εμπορική χρήση.',
    files: [{ path: 'mms-tts-ell/model.onnx', size: 114230384, sha256: '38e3c9824ee483e548e921aba2647378940659e528194ccc59a1e1cb44fb8dc1' },
            { path: 'mms-tts-ell/tokens.json', size: 668, sha256: '765bdf002eb913905dd9cb015755aa7aec24196458d177f06a5cb6b10cfa8de2' }] },
];

// list prices in $, as published when written (shortcut: hand-kept, update when the providers change them)
const PRICES = {
  'voxtral-mini-transcribe-3': { min: 0.0035 }, 'voxtral-small-transcribe-3': { min: 0.004 },
  'voxtral-small-latest': { min: 0.004, in: 0.1, out: 0.3 }, 'mistral-small-latest': { in: 0.1, out: 0.3 },
  // reading aloud, per character (Mistral's from aggregators, OpenAI's ≈ $0.015/min of speech)
  'voxtral-mini-tts-3': { char: 16e-6 }, 'gpt-4o-mini-tts': { char: 15e-6 },
  'gpt-4o-mini-transcribe': { min: 0.003 }, 'gpt-4o-transcribe': { min: 0.006 }, 'gpt-4o-mini': { in: 0.15, out: 0.6 },
};
const cost = (model, sec, tin = 0, tout = 0, chars = 0) => {
  const p = PRICES[model] || {};
  return (p.min || 0) * sec / 60 + ((p.in || 0) * tin + (p.out || 0) * tout) / 1e6 + (p.char || 0) * chars;
};
// every paid call reports here; the page keeps the monthly total
const meter = { on: null };
const used = (provider, model, sec, tin = 0, tout = 0) => { if (meter.on) meter.on(provider, sec, tin + tout, cost(model, sec, tin, tout)); };

async function transcribe(key, model, pcm, { diarize = false, bias = [], provider = 'mistral', lang = 'el' } = {}) {
  const f = new FormData();
  f.append('model', model);
  // OpenAI takes the spelling hints as a free-text prompt; it has no diarize flag on these models
  if (provider === 'openai') { if (bias.length) f.append('prompt', bias.join(', ')); diarize = false; }
  else for (const t of bias) f.append('context_bias', t);
  if (diarize) { f.append('diarize', 'true'); f.append('timestamp_granularities', 'segment'); }
  else if (lang !== 'auto') f.append('language', lang);
  f.append('file', new Blob([wav(pcm)], { type: 'audio/wav' }), 'audio.wav');
  let r;
  try {
    r = await fetch(PROVIDERS[provider].api + '/audio/transcriptions', { method: 'POST', headers: { Authorization: 'Bearer ' + key }, body: f });
  } catch {
    throw new Error('Δεν υπάρχει σύνδεση στο διαδίκτυο.');
  }
  if (r.status === 401) throw Object.assign(new Error('Λάθος κλειδί API — έλεγξέ το στις Ρυθμίσεις.'), { fatal: true });
  if (!r.ok) throw new Error(`Σφάλμα διακομιστή ${r.status}: ${(await r.text()).slice(0, 160)}`);
  used(provider, model, pcm.length / SR);
  return r.json();
}

/** Mistral only: speech straight into `lang` in one call (Voxtral chat with audio), no Greek text in between. */
async function translateAudio(key, pcm, lang) {
  const model = 'voxtral-small-latest';
  const b64 = btoa(Array.from(new Uint8Array(wav(pcm)), c => String.fromCharCode(c)).join(''));
  let r;
  try {
    r = await fetch(PROVIDERS.mistral.api + '/chat/completions', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, temperature: 0, messages: [{ role: 'user', content: [
        { type: 'input_audio', input_audio: b64 },
        { type: 'text', text: `Translate this speech into ${lang}. Reply with the translation only; reply with nothing if there is no speech.` }] }] }),
    });
  } catch {
    throw new Error('Δεν υπάρχει σύνδεση στο διαδίκτυο.');
  }
  if (r.status === 401) throw Object.assign(new Error('Λάθος κλειδί API — έλεγξέ το στις Ρυθμίσεις.'), { fatal: true });
  if (!r.ok) throw new Error(`Σφάλμα διακομιστή ${r.status}: ${(await r.text()).slice(0, 160)}`);
  const j = await r.json(), u = j.usage || {};
  used('mistral', model, pcm.length / SR, u.prompt_tokens || 0, u.completion_tokens || 0);
  return (j.choices[0].message.content || '').trim();
}

/** One system + user turn through the provider's small chat model. */
async function chat(key, provider, system, user, temperature = 0) {
  const p = PROVIDERS[provider];
  const r = await fetch(p.api + '/chat/completions', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: p.chat, temperature, messages: [{ role: 'system', content: system }, { role: 'user', content: user }] }),
  });
  if (!r.ok) throw new Error(`${p.name} ${r.status}`);
  const j = await r.json(), u = j.usage || {};
  used(provider, p.chat, 0, u.prompt_tokens || 0, u.completion_tokens || 0);
  return (j.choices[0].message.content || '').trim();
}

/** One caption phrase → `lang`. */
const translate = (key, provider, text, lang) =>
  chat(key, provider, `Translate the user's words, spoken in a classroom, into ${lang}. Reply with the translation only.`, text);

/** The last minutes of the lesson → up to 3 short questions a pupil could tap instead of typing. */
async function suggestQuestions(key, provider, lesson) {
  const out = await chat(key, provider,
    'Είσαι βοηθός μαθητή που δεν ακούει και γράφει αργά. Από τους υπότιτλους του μαθήματος, γράψε 3 πολύ σύντομες ερωτήσεις ' +
    '(έως 8 λέξεις η καθεμία) που θα μπορούσε να κάνει ο μαθητής στον δάσκαλο για να καταλάβει ή να συμμετάσχει: ' +
    'μία για κάτι που ίσως δεν κατάλαβε, μία για το τι πρέπει να κάνει, μία για το ίδιο το θέμα. ' +
    'Ελληνικά, απλή γλώσσα, μία ερώτηση ανά γραμμή, χωρίς αρίθμηση ή άλλο κείμενο.', lesson, 0.4);
  return out.split('\n').map(l => l.replace(/^[-*\d.)\s]+/, '').trim()).filter(Boolean).slice(0, 3);
}

// reading aloud in the cloud; Mistral has no Greek voice, but its English ones read Greek well
const TTS = { mistral: 'voxtral-mini-tts-3', openai: 'gpt-4o-mini-tts' };
const OPENAI_VOICES = ['marin', 'cedar', 'alloy', 'ash', 'ballad', 'coral', 'echo', 'fable', 'nova', 'onyx', 'sage', 'shimmer', 'verse'];

/** Mistral's preset voices as [id, "Name (lang)"], all pages. */
async function mistralVoices(key) {
  const out = [];
  for (let off = 0; ; off += 100) {
    const r = await fetch(PROVIDERS.mistral.api + `/audio/voices?limit=100&offset=${off}`, { headers: { Authorization: 'Bearer ' + key } });
    if (!r.ok) throw new Error('φωνές ' + r.status);
    const items = (await r.json()).items || [];
    out.push(...items.map(v => [v.id, `${v.name} (${(v.languages || []).join(', ')})`]));
    if (items.length < 100) return out;
  }
}

/** One piece of text → mp3 bytes. */
async function synthesize(key, provider, text, voice) {
  const p = PROVIDERS[provider], model = TTS[provider];
  const body = provider === 'mistral' ? { model, input: text, voice_id: voice, response_format: 'mp3' }
    : { model, input: text, voice, response_format: 'mp3' };
  let r;
  try {
    r = await fetch(p.api + '/audio/speech', { method: 'POST', headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  } catch {
    throw new Error('Δεν υπάρχει σύνδεση στο διαδίκτυο.');
  }
  if (!r.ok) throw new Error(`${p.name} ${r.status}: ${(await r.text()).slice(0, 120)}`);
  if (meter.on) meter.on(provider, 0, 0, cost(model, 0, 0, 0, text.length));
  if (provider === 'openai') return r.arrayBuffer();
  return Uint8Array.from(atob((await r.json()).audio_data), c => c.charCodeAt(0)).buffer;
}

/** 'ok' | 'bad' | 'offline' | 'error' */
async function checkKey(key, provider = 'mistral') {
  try {
    const r = await fetch(PROVIDERS[provider].api + '/models', { headers: { Authorization: 'Bearer ' + key } });
    return r.ok ? 'ok' : r.status === 401 ? 'bad' : 'error';
  } catch {
    return 'offline';
  }
}

/** Diarized JSON → [{t, who, text}]; consecutive segments of one speaker merge; speakers numbered by first appearance. */
function speakerLines(json, offset = 0) {
  const segs = json.segments || [];
  if (!segs.length) return json.text && json.text.trim() ? [{ t: offset, who: '', text: json.text.trim() }] : [];
  const ids = new Map(), out = [];
  let last;
  for (const s of segs) {
    const text = (s.text || '').trim();
    if (!text) continue;
    const id = s.speaker_id == null ? '' : String(s.speaker_id);
    if (out.length && id === last) { out[out.length - 1].text += ' ' + text; continue; }
    if (id && !ids.has(id)) ids.set(id, ids.size + 1);
    out.push({ t: offset + (s.start || 0), who: id ? 'Ομιλητής ' + ids.get(id) : '', text });
    last = id;
  }
  return out;
}

/** Energy VAD (run_vad_capture): feeds 30 ms frames, calls onPhrase(pcm, startSample) per phrase. */
class Vad {
  constructor(onPhrase) {
    Object.assign(this, { onPhrase, noise: 200, calib: [], inSpeech: false, silence: 0, speech: 0, seg: [], pre: [], seen: 0 });
  }
  push(frame) {
    const r = rms(frame, 0, frame.length);
    this.seen++;
    if (this.calib.length < 13) {
      this.calib.push(r);
      this.noise = this.calib.reduce((a, b) => a + b) / this.calib.length;
      return;
    }
    const th = Math.max(this.noise * 2.5 + 120, 280);
    if (!this.inSpeech) {
      this.pre.push(frame);
      if (this.pre.length > 6) this.pre.shift();
      if (r > th) Object.assign(this, { inSpeech: true, silence: 0, speech: 1, seg: [...this.pre] });
      else this.noise = 0.95 * this.noise + 0.05 * r;
      return;
    }
    this.seg.push(frame);
    if (r > th) { this.silence = 0; this.speech++; } else this.silence++;
    // end on a ~0.55 s pause, or force-flush at 12 s so a noisy floor cannot trap us
    if (this.silence >= 18 || this.seg.length * FRAME >= 12 * SR) this.flush();
  }
  flush() {
    if (this.inSpeech && this.speech >= 7) this.onPhrase(concat(this.seg), (this.seen - this.seg.length) * FRAME);
    Object.assign(this, { inSpeech: false, seg: [], pre: [] });
  }
}

/** Text → pieces a TTS engine starts quickly on (read_aloud.chunks). */
function ttsChunks(text) {
  const out = [];
  for (let part of text.split(/(?<=[.!;?·\n])\s+/)) {
    part = part.trim();
    while (part.length > 280) {
      let cut = part.lastIndexOf(',', 280);
      if (cut <= 80) cut = 280;
      out.push(part.slice(0, cut + 1).trim());
      part = part.slice(cut + 1).trim();
    }
    if (part) out.push(part);
  }
  return out;
}

if (typeof module !== 'undefined') {
  module.exports = { MODEL_URL, MODEL_PACKS, PROVIDERS, PRICES, cost, SR, FRAME, CHUNK, CUT_WINDOW, rms, quietestCut, isSilent, concat, wav, stamp, terms, speakerLines, Vad, ttsChunks };
}
