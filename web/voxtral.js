// DOM-free core shared by every shell: audio math, WAV, the Voxtral API, VAD, TTS chunks.
// Loaded as a plain <script>; node's test (test.js) loads it through module.exports.

const SR = 16000;                 // Hz, mono Int16 everywhere
const FRAME = 480;                // 30 ms
const CHUNK = 25 * SR;            // meeting/dictation chunk, in samples
const CUT_WINDOW = 5 * SR;        // cut on the quietest frame of the chunk's last 5 s
// Mistral and OpenAI speak the same REST shape, ElevenLabs its own; each needs its own key from its own console
const PROVIDERS = {
  // on this device: whisper.cpp on the desktop (models listed by main.js), Android's own recognizer on a tablet
  local: { name: 'Τοπικά', models: [['turbo', 'Whisper Large v3 Turbo'], ['medium', 'Whisper Medium'], ['small', 'Whisper Small'], ['large', 'Whisper Large v3']] },
  mistral: { name: 'Mistral', api: 'https://api.mistral.ai/v1', chat: 'mistral-small-latest', console: 'console.mistral.ai → API Keys',
    models: [['voxtral-mini-transcribe-3', 'Voxtral Mini'], ['voxtral-small-transcribe-3', 'Voxtral Small']] },
  openai: { name: 'OpenAI', api: 'https://api.openai.com/v1', chat: 'gpt-4o-mini', console: 'platform.openai.com → API keys',
    models: [['gpt-4o-mini-transcribe', 'GPT-4o mini'], ['gpt-4o-transcribe', 'GPT-4o']] },
  // no chat model: translation, summaries and questions go to Mistral/OpenAI when one has a key
  elevenlabs: { name: 'ElevenLabs', api: 'https://api.elevenlabs.io/v1', console: 'elevenlabs.io → Developers → API Keys',
    models: [['scribe_v2', 'Scribe v2'], ['scribe_v3', 'Scribe v3']] },
};
// ElevenLabs takes its key in its own header; the others take a Bearer token
const auth = (provider, key) => provider === 'elevenlabs' ? { 'xi-api-key': key } : { Authorization: 'Bearer ' + key };
// a wrong key: 401, or ElevenLabs' 400 "authentication_error"
const badKey = async r => r.status === 401 || (r.status === 400 && (await r.clone().text()).includes('authentication_error'));
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
  // turbo: FLEURS Greek 14% wrong words against medium's 20%, at the same speed and 250 MB smaller (scratch benchmark, Nov 2026)
  { id: 'turbo', name: 'Αναγνώριση ομιλίας: Whisper Large v3 Turbo', note: 'Γράφει ό,τι λέτε, χωρίς internet. Με κάρτα γραφικών ~1–2 s ανά φράση.',
    // the same file whisper.cpp publishes, pinned to a commit: a second source while ours is missing or down
    files: [{ path: 'ggml-large-v3-turbo-q5_0.bin', size: 574041195, sha256: '394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2',
      mirror: 'https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3-turbo-q5_0.bin' }] },
  // extra: not in "download all"; only whisper's own Greek → English needs it, and the Firefox models do that better
  { id: 'whisper', extra: true, name: 'Αναγνώριση ομιλίας: Whisper Medium (προαιρετικό)', note: 'Το προηγούμενο μοντέλο: περισσότερα λάθη, αλλά μεταφράζει και μόνο του στα Αγγλικά.',
    files: [{ path: 'ggml-medium-q8_0.bin', size: 823369779, sha256: '42a1ffcbe4167d224232443396968db4d02d4e8e87e213d3ee2e03095dea6502',
      mirror: 'https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-medium-q8_0.bin' }] },
  { id: 'voice', name: 'Ελληνική φωνή: Meta MMS', note: 'Διαβάζει κείμενα φωναχτά, χωρίς internet. Άδεια μόνο για μη εμπορική χρήση.',
    files: [{ path: 'mms-tts-ell/model.onnx', size: 114230384, sha256: '38e3c9824ee483e548e921aba2647378940659e528194ccc59a1e1cb44fb8dc1' },
            { path: 'mms-tts-ell/tokens.json', size: 668, sha256: '765bdf002eb913905dd9cb015755aa7aec24196458d177f06a5cb6b10cfa8de2' }] },
];

// list prices in $, as published when written (shortcut: hand-kept, update when the providers change them)
const PRICES = {
  'voxtral-mini-transcribe-3': { min: 0.0035 }, 'voxtral-mini-transcribe-realtime-3': { min: 0.008 },'voxtral-small-transcribe-3': { min: 0.004 },
  'voxtral-small-latest': { min: 0.004, in: 0.1, out: 0.3 }, 'mistral-small-latest': { in: 0.1, out: 0.3 }, 'mistral-medium-latest': { in: 0.4, out: 2 },
  // reading aloud, per character (Mistral's from aggregators, OpenAI's ≈ $0.015/min of speech)
  'voxtral-mini-tts-3': { char: 16e-6 }, 'gpt-4o-mini-tts': { char: 15e-6 },
  'gpt-4o-mini-transcribe': { min: 0.003 }, 'gpt-4o-transcribe': { min: 0.006 }, 'gpt-4o-mini': { in: 0.15, out: 0.6 },
  // ElevenLabs pay-as-you-go (Nov 2026): Scribe $0.20/h batch, $0.22/h live, + keyterms $0.05/h and $0.08/h (we always send them)
  'scribe_v2': { min: 0.25 / 60 }, 'scribe_v3': { min: 0.25 / 60 }, 'scribe_v3_live': { min: 0.30 / 60 }, 'eleven_multilingual_v2': { char: 80e-6 },
};
const cost = (model, sec, tin = 0, tout = 0, chars = 0) => {
  const p = PRICES[model] || {};
  return (p.min || 0) * sec / 60 + ((p.in || 0) * tin + (p.out || 0) * tout) / 1e6 + (p.char || 0) * chars;
};
// every paid call reports here as { p (provider), f (feature), m (model), s, i, o, c, usd }; the page keeps the ledger
const meter = { on: null };
const used = (p, f, m, s, i = 0, o = 0, c = 0) => { if (meter.on) meter.on({ p, f, m, s, i, o, c, usd: cost(m, s, i, o, c) }); };

/*
 * The ledger of paid calls, Tetra's usage page in VOICE (8 Nov 2026: Tetra's is "fuller and correct"). Tetra writes a line a
 * call to files; here it lives in localStorage (~5 MB) on desktop and Android alike, so it sums a day per provider|feature|model:
 * { v: 1, days: { 'YYYY-MM-DD': { 'mistral|captions|voxtral-mini-transcribe-3': { n, usd, s, i, o, c } } } }. Nothing is dropped.
 */
const pad = n => String(n).padStart(2, '0');
const dayKey = ms => { const d = new Date(ms); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
/** The stored ledger, or (first run) a new one holding the old month total ({ month, mistral: { sec, tokens, usd } }) on its 1st. */
function usageLoad(text, oldText) {
  try { const l = JSON.parse(text); if (l && l.days) return l; } catch { /* none yet */ }
  const log = { v: 1, days: {} };
  try {
    const old = JSON.parse(oldText);
    for (const [p, x] of Object.entries(old)) if (p !== 'month' && x && x.usd > 0)
      (log.days[old.month + '-01'] ||= {})[`${p}|before|`] = { n: 0, usd: x.usd, s: x.sec || 0, i: x.tokens || 0, o: 0, c: 0 };
  } catch { /* no old total */ }
  return log;
}
function usageAdd(log, e, t = Date.now()) {
  const d = (log.days[dayKey(t)] ||= {}), k = `${e.p}|${e.f}|${e.m}`, x = d[k] || (d[k] = { n: 0, usd: 0, s: 0, i: 0, o: 0, c: 0 });
  x.n++;
  for (const f of ['usd', 's', 'i', 'o', 'c']) x[f] += +e[f] || 0;
  return log;
}
/** Today, this month, all time; by feature, model and provider (costliest first); every day, month and year (newest first). */
function usageSummary(log, now = Date.now()) {
  const rows = Object.entries(log.days).flatMap(([day, d]) => Object.entries(d).map(([k, x]) => {
    const [p, f, m] = k.split('|');
    return { day, p, f, m, ...x };
  }));
  const sum = xs => xs.reduce((s, x) => s + x.usd, 0);
  const group = (key, xs) => [...xs.reduce((g, x) => {
    const k = key(x);
    if (!k) return g;
    const o = g.get(k) || { key: k, usd: 0, n: 0 };
    o.usd += x.usd; o.n += x.n;
    return g.set(k, o);
  }, new Map()).values()];
  const costly = xs => xs.sort((a, b) => b.usd - a.usd), newest = xs => xs.sort((a, b) => (a.key < b.key ? 1 : -1));
  const today = dayKey(now), month = today.slice(0, 7);
  return {
    today: sum(rows.filter(x => x.day === today)), month: sum(rows.filter(x => x.day.startsWith(month))), all: sum(rows),
    since: rows.reduce((m, x) => (x.day < m ? x.day : m), today),
    byFeature: costly(group(x => x.f, rows)), byModel: costly(group(x => x.m, rows)), byProvider: costly(group(x => x.p, rows)),
    history: { days: newest(group(x => x.day, rows)), months: newest(group(x => x.day.slice(0, 7), rows)), years: newest(group(x => x.day.slice(0, 4), rows)) },
  };
}

/** Scribe's keyterms: at most 50 characters and 5 words each, none of < > { } [ ] \ (shortcut: the first 100, as Mistral's). */
const keyterms = bias => bias.filter(t => t.length <= 50 && t.split(/\s+/).length <= 5 && !/[<>{}[\]\\]/.test(t));

async function transcribe(key, model, pcm, { diarize = false, bias = [], provider = 'mistral', lang = 'el' } = {}) {
  const f = new FormData(), el = provider === 'elevenlabs';
  if (el) {
    // Scribe keeps the language with diarization, and takes the raw 16 kHz PCM as it is
    f.append('model_id', model);
    if (lang !== 'auto') f.append('language_code', lang);
    for (const t of keyterms(bias)) f.append('keyterms', t);
    if (diarize) f.append('diarize', 'true');
    f.append('file_format', 'pcm_s16le_16');
    f.append('file', new Blob([pcm]), 'audio.pcm');
  } else {
    f.append('model', model);
    // OpenAI takes the spelling hints as a free-text prompt; it has no diarize flag on these models
    if (provider === 'openai') { if (bias.length) f.append('prompt', bias.join(', ')); diarize = false; }
    else for (const t of bias) f.append('context_bias', t);
    if (diarize) { f.append('diarize', 'true'); f.append('timestamp_granularities', 'segment'); }
    else if (lang !== 'auto') f.append('language', lang);
    f.append('file', new Blob([wav(pcm)], { type: 'audio/wav' }), 'audio.wav');
  }
  let r;
  try {
    r = await fetch(PROVIDERS[provider].api + (el ? '/speech-to-text' : '/audio/transcriptions'), { method: 'POST', headers: auth(provider, key), body: f });
  } catch {
    throw new Error('Δεν υπάρχει σύνδεση στο διαδίκτυο.');
  }
  if (await badKey(r)) throw Object.assign(new Error('Λάθος κλειδί API — έλεγξέ το στις Ρυθμίσεις.'), { fatal: true });
  if (!r.ok) throw new Error(`Σφάλμα διακομιστή ${r.status}: ${(await r.text()).slice(0, 160)}`);
  used(provider, diarize ? 'meeting' : 'captions', model, pcm.length / SR);
  return el ? scribeSegments(await r.json()) : r.json();
}

/**
 * Scribe's words (spaces and "(laughter)"-style sound tags are words too) → Mistral's { text, segments: [{ start, text, speaker_id }] },
 * a segment per run of one speaker; no segments without diarization.
 */
function scribeSegments(j) {
  const segs = [];
  for (const w of j.words || []) {
    const last = segs[segs.length - 1];
    if (w.type !== 'spacing' && (!last || w.speaker_id !== last.speaker_id)) segs.push({ start: w.start || 0, speaker_id: w.speaker_id ?? null, text: w.text });
    else if (last) last.text += w.text;
  }
  return { text: j.text || '', segments: segs.some(x => x.speaker_id != null) ? segs : [] };
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
  used('mistral', 'translate', model, pcm.length / SR, u.prompt_tokens || 0, u.completion_tokens || 0);
  return (j.choices[0].message.content || '').trim();
}

/** One system + user turn through the provider's small chat model. */
async function chat(key, provider, system, user, temperature = 0, feature = 'chat', model = '') {
  const p = PROVIDERS[provider], m = model || p.chat;
  const r = await fetch(p.api + '/chat/completions', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: m, temperature, messages: [{ role: 'system', content: system }, { role: 'user', content: user }] }),
  });
  if (!r.ok) throw new Error(`${p.name} ${r.status}`);
  const j = await r.json(), u = j.usage || {};
  used(provider, feature, m, 0, u.prompt_tokens || 0, u.completion_tokens || 0);
  return (j.choices[0].message.content || '').trim();
}

/** One caption phrase → `lang`. */
const translate = (key, provider, text, lang) =>
  chat(key, provider, `Translate the user's words, spoken in a classroom, into ${lang}. Reply with the translation only.`, text, 0, 'translate');

/** The last minutes of the lesson → up to 3 short questions a pupil could tap instead of typing. */
async function suggestQuestions(key, provider, lesson) {
  const out = await chat(key, provider,
    'Είσαι βοηθός μαθητή που δεν ακούει και γράφει αργά. Από τους υπότιτλους του μαθήματος, γράψε 3 πολύ σύντομες ερωτήσεις ' +
    '(έως 8 λέξεις η καθεμία) που θα μπορούσε να κάνει ο μαθητής στον δάσκαλο για να καταλάβει ή να συμμετάσχει: ' +
    'μία για κάτι που ίσως δεν κατάλαβε, μία για το τι πρέπει να κάνει, μία για το ίδιο το θέμα. ' +
    'Ελληνικά, απλή γλώσσα, μία ερώτηση ανά γραμμή, χωρίς αρίθμηση ή άλλο κείμενο.', lesson, 0.4, 'questions');
  return out.split('\n').map(l => l.replace(/^[-*\d.)\s]+/, '').trim()).filter(Boolean).slice(0, 3);
}

// reading aloud in the cloud; Mistral has no Greek voice, but its English ones read Greek well
const TTS = { mistral: 'voxtral-mini-tts-3', openai: 'gpt-4o-mini-tts', elevenlabs: 'eleven_multilingual_v2' };
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

/** The account's ElevenLabs voices (premade and the school's own) as [id, "Name (accent)"]; all speak Greek with this model. */
async function elevenVoices(key) {
  // shortcut: the first 100 voices, enough for a school account; page with next_page_token if one has more
  const r = await fetch('https://api.elevenlabs.io/v2/voices?page_size=100', { headers: auth('elevenlabs', key) });
  if (!r.ok) throw new Error('φωνές ' + r.status);
  return ((await r.json()).voices || []).map(v => [v.voice_id, v.name + (v.labels?.accent ? ` (${v.labels.accent})` : '')]);
}

/** One piece of text → mp3 bytes. */
async function synthesize(key, provider, text, voice) {
  const p = PROVIDERS[provider], model = TTS[provider], el = provider === 'elevenlabs';
  const body = el ? { model_id: model, text } : provider === 'mistral' ? { model, input: text, voice_id: voice, response_format: 'mp3' }
    : { model, input: text, voice, response_format: 'mp3' };
  const url = el ? `${p.api}/text-to-speech/${encodeURIComponent(voice)}?output_format=mp3_44100_128` : p.api + '/audio/speech';
  let r;
  try {
    r = await fetch(url, { method: 'POST', headers: { ...auth(provider, key), 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  } catch {
    throw new Error('Δεν υπάρχει σύνδεση στο διαδίκτυο.');
  }
  if (!r.ok) throw new Error(`${p.name} ${r.status}: ${(await r.text()).slice(0, 120)}`);
  used(provider, 'read', model, 0, 0, 0, text.length);
  if (provider !== 'mistral') return r.arrayBuffer();
  return Uint8Array.from(atob((await r.json()).audio_data), c => c.charCodeAt(0)).buffer;
}

/** 'ok' | 'bad' | 'offline' | 'error' */
async function checkKey(key, provider = 'mistral') {
  try {
    const r = await fetch(PROVIDERS[provider].api + '/models', { headers: auth(provider, key) });
    return r.ok ? 'ok' : await badKey(r) ? 'bad' : 'error';
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

/** Several mics (phones/tablets over Wi-Fi, the laptop's own) → one stream. Summing them would add up every
 *  device's noise and play the same voice twice, a few ms apart; instead each input's gain follows how far it
 *  stands above its own noise floor, so whoever hears the speaker best leads (an "automixer"). */
const MIX_JITTER = SR / 2, MIX_MAX_LAG = 2 * SR; // buffer 0.5 s per device; one that falls 2 s behind skips ahead
class AutoMixer {
  constructor() { this.inputs = new Map(); }
  push(id, pcm, name = id) {
    let i = this.inputs.get(id);
    if (!i) this.inputs.set(id, i = { name, q: [], len: 0, primed: false, gain: 0, noise: 0, level: 0, muted: false });
    i.q.push(pcm);
    i.len += pcm.length;
  }
  take(i, n) {
    const out = new Int16Array(n);
    let k = 0;
    while (k < n && i.q.length) {
      const head = i.q[0], m = Math.min(n - k, head.length);
      out.set(head.subarray(0, m), k);
      k += m;
      if (m === head.length) i.q.shift(); else i.q[0] = head.subarray(m);
    }
    i.len -= k;
    if (k < n) i.primed = false; // ran dry (Wi-Fi hiccup): wait for a fresh buffer instead of stuttering
    return out;
  }
  pull(n) {
    const live = [];
    for (const i of this.inputs.values()) {
      if (!i.primed && i.len >= MIX_JITTER) i.primed = true;
      if (!i.primed) { i.level = 0; continue; }
      if (i.len > MIX_MAX_LAG) this.take(i, i.len - MIX_JITTER);
      const x = this.take(i, n), r = rms(x, 0, n);
      // the floor drops at once and rises slowly, so a long sentence is not mistaken for noise
      i.noise = !i.noise ? r : r < i.noise ? 0.7 * i.noise + 0.3 * r : i.noise * 1.01;
      i.level = r;
      live.push({ i, x, w: i.muted ? 0 : Math.max(0, r - 2 * i.noise - 50) ** 2 });
    }
    const sum = live.reduce((s, l) => s + l.w, 0);
    let target = live.map(l => sum ? l.w / sum : l.i.muted ? 0 : l.i.gain); // silence: keep the last leader
    if (!sum && target.reduce((s, g) => s + g, 0) < 0.01) {
      const on = live.filter(l => !l.i.muted).length;
      target = live.map(l => l.i.muted ? 0 : 1 / on);
    }
    const out = new Int16Array(n), acc = new Float32Array(n);
    live.forEach((l, k) => {
      const t = target[k], g0 = l.i.gain;
      // rise fast so the first word is not lost, fall slowly so voices do not pump
      const g1 = l.i.gain = g0 + (t - g0) * (t > g0 ? 0.6 : 0.15);
      for (let s = 0; s < n; s++) acc[s] += l.x[s] * (g0 + (g1 - g0) * s / n);
    });
    for (let s = 0; s < n; s++) out[s] = Math.max(-32768, Math.min(32767, acc[s]));
    return out;
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

/** Words → lines of at most `max` characters (a longer single word keeps its own line). */
function wrapWords(text, max = 16) {
  const out = [];
  for (const w of String(text).split(/\s+/).filter(Boolean)) {
    if (out.length && (out[out.length - 1] + ' ' + w).length <= max) out[out.length - 1] += ' ' + w;
    else out.push(w);
  }
  return out.length ? out : [''];
}

/**
 * Concept map { label, children: [{ label, link, children }] } → a radial layout: the main idea in the middle, its
 * branches on a ring, each branch's details fanned out beyond it. Boxes are sized from the text (px at font size `fs`).
 */
function conceptLayout(tree, fs = 22) {
  const nodes = [], edges = [];
  const box = (label, x, y, level) => {
    const lines = wrapWords(label, level ? 16 : 14), size = level ? fs : fs * 1.25;
    const n = { x, y, lines, level, size, w: Math.max(...lines.map(l => l.length)) * size * 0.6 + 28, h: lines.length * size * 1.25 + 18 };
    nodes.push(n);
    return n;
  };
  const root = box(tree.label || '', 0, 0, 0);
  const kids = (tree.children || []).slice(0, 7), n = kids.length;
  // an ellipse, wider than tall like the screen it is drawn on
  const R1 = 120 + n * 14, R2 = R1 + 130, W = 1.6, H = 0.85;
  kids.forEach((k, i) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / n;
    const c = box(k.label || '', W * R1 * Math.cos(a), H * R1 * Math.sin(a), 1);
    edges.push({ from: root, to: c, label: k.link || '' });
    const gs = (k.children || []).slice(0, 4), spread = Math.min(2 * Math.PI / Math.max(n, 1), 1.3) * 0.8;
    gs.forEach((g, j) => {
      const b = a + (gs.length > 1 ? spread * (j / (gs.length - 1) - 0.5) : 0);
      const d = box(g.label || '', W * R2 * Math.cos(b), H * R2 * Math.sin(b), 2);
      edges.push({ from: c, to: d, label: g.link || '' });
    });
  });
  // boxes of neighbouring branches can touch: push each one that does outward along its own ray until it is free
  const hit = (p, q) => Math.abs(p.x - q.x) < (p.w + q.w) / 2 + 8 && Math.abs(p.y - q.y) < (p.h + q.h) / 2 + 8;
  nodes.forEach((m, i) => {
    const r = Math.hypot(m.x, m.y);
    if (!r) return;
    for (let k = 0; k < 60 && nodes.slice(0, i).some(o => hit(m, o)); k++) { m.x += 15 * m.x / r; m.y += 15 * m.y / r; }
  });
  const pad = 20;
  const minX = Math.min(...nodes.map(m => m.x - m.w / 2)) - pad, maxX = Math.max(...nodes.map(m => m.x + m.w / 2)) + pad;
  const minY = Math.min(...nodes.map(m => m.y - m.h / 2)) - pad, maxY = Math.max(...nodes.map(m => m.y + m.h / 2)) + pad;
  return { nodes, edges, view: [minX, minY, maxX - minX, maxY - minY] };
}

/** The model's reply → a concept tree, or null; it sometimes wraps the JSON in ``` fences or adds a sentence. */
function parseConcept(text) {
  const m = /\{[\s\S]*\}/.exec(String(text));
  if (!m) return null;
  try { const t = JSON.parse(m[0]); return t && t.label ? t : null; } catch { return null; }
}

// «Μικρόφωνο PC»: what the computer's QR code carries to the phone — every address it has, its port, code and name
const pairText = ({ addrs, port, code, pc }) => `voicemic:1?a=${addrs.join(',')}&p=${port}&c=${code}&n=${encodeURIComponent(pc)}`;
// anything can be shown to the camera: accept only plain IPv4 addresses, a port and a 4-digit code
function parsePair(text) {
  const m = /^voicemic:1\?(.*)$/.exec(String(text).trim());
  if (!m) return null;
  const q = new URLSearchParams(m[1]), addrs = (q.get('a') || '').split(',').filter(a => /^(\d{1,3}\.){3}\d{1,3}$/.test(a));
  const port = Number(q.get('p')), code = q.get('c') || '';
  if (!addrs.length || !(port > 0 && port < 65536) || !/^\d{4}$/.test(code)) return null;
  return { addrs: addrs.slice(0, 6), port, code, pc: (q.get('n') || '').slice(0, 40) || 'υπολογιστή' };
}

if (typeof module !== 'undefined') {
  module.exports = { MODEL_URL, MODEL_PACKS, PROVIDERS, PRICES, cost, dayKey, usageLoad, usageAdd, usageSummary, SR, FRAME, CHUNK, CUT_WINDOW, rms, quietestCut, isSilent, concat, wav, stamp, terms, speakerLines, scribeSegments, keyterms, Vad, AutoMixer, pairText, parsePair, ttsChunks, wrapWords, conceptLayout, parseConcept };
}
