// VOICE UI, shared by the Electron (desktop) and Android shells. Each shell supplies a small bridge:
//   Electron: window.voiceElectron (electron/preload.js)   Android: window.VoiceAndroid (MainActivity.Bridge)
// Everything else — capture, chunking, Voxtral calls — runs here, so both apps behave the same.
'use strict';

const $ = s => document.querySelector(s);
const prefs = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage blocked: settings just don't stick */ } },
};

// the Electron mini window loads this same page with ?mini=1
const MINI = new URLSearchParams(location.search).has('mini');
if (MINI) document.body.classList.add('mini');

const host = (() => {
  const E = window.voiceElectron, A = window.VoiceAndroid;
  if (E) return {
    system: E.platform === 'win32' ? 'loopback' : E.platform === 'linux' ? 'pulse' : null,
    getKey: () => E.getKey(), setKey: k => E.setKey(k),
    save: (text, name) => E.save(text, name), keepAwake: on => E.keepAwake(on),
    startSystem: onPcm => E.startSystem(onPcm), stopSystem: () => E.stopSystem(),
    wifiStart: (code, onPcm) => E.wifiStart(code, onPcm),
    piper: () => E.piper(), synth: (t, rate) => E.synth(t, rate),
    whisperReady: () => E.whisperReady(), whisper: (wav, tr) => E.whisper(wav, tr),
    mms: t => E.mms(t), models: E.models,
    openMini: () => E.openMini(), closeMini: () => E.closeMini(), type: t => E.type(t),
  };
  if (A) return {
    system: null,
    getKey: async () => A.getKey(),
    setKey: async k => { if (!A.setKey(k)) throw new Error('Το κλειδί δεν αποθηκεύτηκε.'); },
    save: async text => A.share(text), keepAwake: on => A.keepAwake(on), fullscreen: on => A.fullscreen(on),
    // on-device captions (SpeechRecognizer): events come back through window.onRecog / onRecogInfo
    listen: on => A.listen(on), setLang: l => A.setLang(l), checkRecognizer: () => A.checkRecognizer(), downloadGreek: () => A.downloadGreek(),
    speak: (text, rate) => A.speak(text, rate), stopSpeak: () => A.stopSpeak(), ttsInfo: () => A.ttsInfo(),
    // «Μικρόφωνο PC»: MicService sends this device's sound to a computer, also with the screen off
    micStart: o => A.micStart(JSON.stringify(o)), micStop: () => A.micStop(), micState: () => JSON.parse(A.micState()),
    micPermissions: () => new Promise(ok => { window.onPerms = ok; A.micPermissions(); }),
    deviceName: () => A.deviceName(),
  };
  // plain browser, for working on the UI only: the key sits in localStorage
  return {
    system: null, getKey: async () => prefs.get('key', ''), setKey: async k => prefs.set('key', k),
    save: async text => copyText(text), keepAwake() {},
  };
})();

// ---------- small UI helpers ----------

for (const el of document.querySelectorAll('[data-icon]')) el.innerHTML = icon(el.dataset.icon);

let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('shown');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('shown'), 3000);
}

async function copyText(text) {
  if (!text.trim()) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // WebView on file:// may refuse the async clipboard; the old path still works
    const ta = Object.assign(document.createElement('textarea'), { value: text });
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  toast('Αντιγράφηκε.');
}

function segmented(el, items, value, onPick) {
  el.replaceChildren(...items.map(([v, label]) => {
    const b = document.createElement('button');
    b.dataset.v = v;
    b.textContent = label;
    return b;
  }));
  const mark = v => el.querySelectorAll('button').forEach(b => {
    b.classList.toggle('on', b.dataset.v === v);
    b.setAttribute('aria-checked', b.dataset.v === v);
    b.setAttribute('role', 'radio');
  });
  mark(value);
  el.onclick = e => {
    const b = e.target.closest('button');
    if (b) { mark(b.dataset.v); onPick(b.dataset.v); }
  };
}

function show(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('shown', p.id === page));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === page));
  if (!MINI) prefs.set('page', page);
  if (page === 'read') ttsInfo();
  if (page === 'pcmic') pcmic?.enter(); else pcmic?.leave();
}
document.querySelectorAll('.nav-item').forEach(n => n.onclick = () => show(n.dataset.page));

// 'local' (whisper on the desktop, the device recognizer on Android), 'mistral' or 'openai'
const provider = () => prefs.get('provider', host.listen ? 'local' : 'mistral');
let whisperModels = [];
// offline translation (local.js): Greek ↔ English only; the Android APK ships the models, the desktop says where they are
let localMT = !!host.listen;
const MT_CODE = { English: 'en', Greek: 'el' }; // desktop: installed whisper models, best first, once main.js reports them
const models = (p = provider()) => p !== 'local' ? PROVIDERS[p].models
  : PROVIDERS.local.models.filter(([id]) => whisperModels.includes(id)); // none on Android: the device decides
const model = (p = provider()) => {
  const m = prefs.get('model-' + p, ''), list = models(p);
  return list.some(x => x[0] === m) ? m : list.length ? list[0][0] : '';
};
const lang = () => prefs.get('lang', 'el');
// one secure slot holds every provider's key as JSON; an older plain value is the Mistral key
async function keys() {
  const raw = (await host.getKey()) || '';
  if (raw.startsWith('{')) try { return JSON.parse(raw); } catch { /* corrupt: start over */ }
  return raw ? { mistral: raw } : {};
}
const apiKey = async (p = provider()) => p === 'local' ? '' : (await keys())[p] || '';
// translation needs a cloud even when speech runs locally: the chosen one, else whichever has a key
async function cloud() {
  const all = await keys(), p = provider() !== 'local' ? provider() : ['mistral', 'openai'].find(x => all[x]);
  return p && all[p] ? { p, key: all[p] } : null;
}
async function setApiKey(k) {
  const all = await keys();
  all[keyProvider()] = k;
  await host.setKey(JSON.stringify(all));
}

// ---------- capture: every source becomes 16 kHz mono Int16 ----------

function streamPcm(stream, onPcm) {
  const ctx = new AudioContext({ sampleRate: SR });
  const src = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  proc.onaudioprocess = e => {
    const f = e.inputBuffer.getChannelData(0), out = new Int16Array(f.length);
    for (let i = 0; i < f.length; i++) out[i] = Math.max(-1, Math.min(1, f[i])) * 32767;
    onPcm(out);
  };
  src.connect(proc);
  proc.connect(ctx.destination); // the processor only runs when connected; it outputs silence
  return () => { proc.disconnect(); src.disconnect(); stream.getTracks().forEach(t => t.stop()); ctx.close(); };
}

async function openMic(onPcm) {
  const s = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true },
  });
  return streamPcm(s, onPcm);
}

async function openSystem(onPcm) {
  if (host.system === 'pulse') {          // Linux: parec on the default sink's monitor (main process)
    await host.startSystem(onPcm);
    return () => host.stopSystem();
  }
  // Windows: Chromium loopback through getDisplayMedia (main.js answers with audio: 'loopback')
  const s = await navigator.mediaDevices.getDisplayMedia({ audio: true, video: true });
  s.getVideoTracks().forEach(t => t.stop());
  if (!s.getAudioTracks().length) throw new Error('Δεν δόθηκε ο ήχος του υπολογιστή.');
  return streamPcm(s, onPcm);
}

// this computer's mic, mixed with the phones once any have joined (Teleprompt → + Μικρόφωνο)
async function openRoomMic(onPcm) {
  if (!mics?.running) return openMic(onPcm);
  const close = await openMic(p => mics.push('local', p, 'Αυτός ο υπολογιστής'));
  mics.out = onPcm;
  return () => { close(); mics.out = null; mics.drop('local'); };
}

function micError(e) {
  if (e.name === 'NotAllowedError') return host.system === 'loopback'
    ? 'Τα Windows δεν δίνουν το μικρόφωνο: Ρυθμίσεις → Απόρρητο και ασφάλεια → Μικρόφωνο → άνοιξε «Πρόσβαση στο μικρόφωνο» και «Να επιτρέπεται σε εφαρμογές υπολογιστή».'
    : 'Δεν δόθηκε άδεια για το μικρόφωνο. Στο tablet: Ρυθμίσεις → Εφαρμογές → VOICE → Άδειες → Μικρόφωνο.';
  if (e.name === 'NotFoundError') return 'Δεν βρέθηκε μικρόφωνο.';
  return 'Ο ήχος δεν ξεκίνησε: ' + e.message;
}

const KEEP_MAX = 60 * 60 * SR; // diarize at most the first hour (≈115 MB of WAV)

/** One source's audio → ~25 s chunks cut at a quiet frame, or VAD phrases (the mic beside the speakers). */
class Track {
  constructor(who, phrases, keepAll, submit, chunk = CHUNK) {
    Object.assign(this, { who, submit, keepAll, chunk, parts: [], len: 0, offset: 0, rest: new Int16Array(0), all: [], allLen: 0 });
    // alongside the speakers the mic only carries your own voice; 25 s of room noise made Voxtral invent sentences
    if (phrases) this.vad = new Vad((pcm, start) => submit(pcm, start / SR, who));
  }
  push(pcm) {
    if (this.keepAll) {
      if (this.allLen < KEEP_MAX) { this.all.push(pcm); this.allLen += pcm.length; } else this.overflow = true;
    }
    if (this.vad) {
      const buf = concat([this.rest, pcm]);
      let o = 0;
      for (; o + FRAME <= buf.length; o += FRAME) this.vad.push(buf.subarray(o, o + FRAME));
      this.rest = buf.slice(o);
      return;
    }
    this.parts.push(pcm);
    this.len += pcm.length;
    if (this.len >= this.chunk) this.cut(false);
  }
  cut(final) {
    if (this.vad) { if (final) this.vad.flush(); return; }
    const buf = concat(this.parts), n = final ? buf.length : quietestCut(buf, this.chunk, Math.min(CUT_WINDOW, this.chunk / 3));
    this.parts = [buf.slice(n)];
    this.len = buf.length - n;
    const start = this.offset;
    this.offset += n;
    const pcm = buf.slice(0, n);
    if (pcm.length > SR / 4 && !isSilent(pcm)) this.submit(pcm, start / SR, this.who);
  }
}

async function retry(fn) {
  for (let i = 0; ; i++) {
    try { return await fn(); } catch (e) {
      if (e.fatal || i >= 2) throw e;
      await new Promise(r => setTimeout(r, 3000 * (i + 1)));
    }
  }
}

// ---------- recording session (one at a time) ----------

const rec = { mode: null, busy: false, tracks: [], closers: [], queue: Promise.resolve(), pending: 0 };
let noMic = false; // no microphone anywhere (see checkMic): the record buttons are greyed out
const panel = mode => document.getElementById(mode);
const recBtn = mode => panel(mode).querySelector('.rec');

function status(mode, text, error = false) {
  const s = panel(mode).querySelector('.status');
  s.textContent = text;
  s.classList.toggle('error', error);
}

function paintRec() {
  for (const mode of ['dictate', 'meeting', 'prompt']) {
    const b = recBtn(mode), on = rec.mode === mode;
    b.innerHTML = icon(on ? 'stop' : 'mic', b.dataset.label ? 24 : 32) + (b.dataset.label ? `<span>${on ? 'Διακοπή' : b.dataset.label}</span>` : '');
    b.classList.toggle('on', on);
    b.setAttribute('aria-label', on ? 'Τέλος ηχογράφησης' : 'Έναρξη ηχογράφησης');
    b.disabled = rec.busy || (noMic && !rec.mode);
  }
}

function level(pcm) {
  const r = rms(pcm, 0, pcm.length);
  panel(rec.mode).querySelector('.level i').style.width = Math.min(100, Math.sqrt(r / 6000) * 100) + '%';
}

async function toggleRec(mode) {
  if (rec.busy) return;
  if (rec.mode) {
    if (rec.mode === mode) return stopRec();
    return toast('Ήδη ηχογραφείς στην άλλη σελίδα.');
  }
  if (host.micState?.().on) return toast('Η συσκευή είναι μικρόφωνο του υπολογιστή: σταμάτησέ το πρώτα στο «Μικρόφωνο PC».');
  if (useLocal()) return startLocal(mode);
  const key = await apiKey();
  // desktop without a key or a network (e.g. a classroom with no internet) falls back to whisper on this computer
  const offline = whisperModels.length > 0 && (provider() === 'local' || !key || !navigator.onLine);
  if (!key && !offline) {
    toast(provider() === 'local' ? 'Δεν βρέθηκε τοπικό μοντέλο whisper σε αυτόν τον υπολογιστή.' : `Βάλε πρώτα το κλειδί της ${PROVIDERS[provider()].name} στις Ρυθμίσεις.`);
    return show('settings');
  }
  const src = host.system ? prefs.get('source', 'mic') : 'mic';
  const both = src === 'both';
  Object.assign(rec, {
    mode, tracks: [], closers: [], failed: false, paused: false,
    diarize: mode === 'meeting' && !both && !offline && provider() === 'mistral' && prefs.get('diarize', '1') === '1',
    opts: { key, offline, model: model(), wmodel: model('local'), provider: provider(), lang: lang(), bias: terms(prefs.get('terms', DEFAULT_TERMS)) },
  });
  if (mode === 'meeting') setLines([]);
  const submit = (pcm, sec, who) => enqueue(mode, pcm, sec, who);
  try {
    if (src !== 'system') {
      // the teleprompt wants words fast (short VAD phrases, not 25 s chunks); so does the mini bar, which types each one
      const t = new Track(both ? 'Εγώ' : '', both || mode === 'prompt' || MINI, rec.diarize, submit);
      rec.tracks.push(t);
      // while the pupil's message is read aloud the mic would caption the tablet's own voice
      rec.closers.push(await openRoomMic(p => { if (!speech.on && !rec.paused) t.push(p); level(p); }));
    }
    if (src === 'system' || both) {
      const t = new Track(both ? 'Ηχεία' : '', mode === 'prompt', rec.diarize, submit);
      rec.tracks.push(t);
      rec.closers.push(await openSystem(p => { if (!rec.paused) t.push(p); if (!both) level(p); }));
    }
  } catch (e) {
    rec.closers.forEach(c => c());
    rec.mode = null;
    status(mode, micError(e), true);
    return paintRec();
  }
  startClock(mode);
  status(mode, (offline ? 'Τοπικά, χωρίς internet. ' : '') + 'Ηχογράφηση… ' + ({ meeting: 'Η μεταγραφή γράφεται ανά 25 δευτερόλεπτα.', prompt: 'Κάθε φράση εμφανίζεται μόλις τελειώσει.' }[mode] || 'Ξαναπάτησε όταν τελειώσεις.'));
  paintRec();
}

function startClock(mode) {
  rec.t0 = performance.now();
  rec.timer = setInterval(() => {
    panel(mode).querySelector('.timer').textContent = stamp((performance.now() - rec.t0) / 1000);
  }, 250);
  host.keepAwake(true);
}

// ---------- on-device recognition (Android SpeechRecognizer, Greek, restarted after every utterance) ----------

const useLocal = () => !!host.listen && provider() === 'local';

function startLocal(mode) {
  Object.assign(rec, { mode, local: true, failed: false, utterStart: null });
  if (mode === 'meeting') setLines([]);
  window.onRecog = (kind, text) => handleRecog(mode, kind, text);
  host.setLang(lang() === 'en' ? 'en-US' : 'el-GR'); // the device recognizer has no "auto"
  host.listen(true);
  startClock(mode);
  status(mode, 'Ακούω…');
  paintRec();
}

function handleRecog(mode, kind, text) {
  const ready = 'Ακούω… ' + (rec.engine === 'device' ? '(στη συσκευή, χωρίς internet)' : '(υπηρεσία αναγνώρισης της συσκευής)');
  if (kind === 'level') {
    panel(mode).querySelector('.level i').style.width = Math.max(0, Math.min(100, (Number(text) + 2) * 8)) + '%';
  } else if (kind === 'engine') {
    rec.engine = text;
    status(mode, ready);
  } else if (kind === 'partial') {
    if (rec.utterStart == null) rec.utterStart = (performance.now() - rec.t0) / 1000;
    if (mode === 'prompt') prompt.partial(text); else status(mode, '… ' + text);
  } else if (kind === 'final') {
    const t = rec.utterStart ?? (performance.now() - rec.t0) / 1000;
    rec.utterStart = null;
    text = text.trim();
    if (mode === 'prompt') prompt.final(text);
    else {
      if (text && mode === 'dictate') insertDictation(text);
      if (text && mode === 'meeting') addLine({ t, who: '', text });
      status(mode, ready);
    }
  } else if (kind === 'error') {
    status(mode, text, true);
  } else if (kind === 'fatal') {
    status(mode, text, true);
    stopLocal(false);
  }
}

function stopLocal(done = true) {
  const mode = rec.mode;
  host.listen(false);
  window.onRecog = null;
  clearInterval(rec.timer);
  host.keepAwake(false);
  Object.assign(rec, { mode: null, local: false });
  panel(mode).querySelector('.level i').style.width = '0';
  if (mode === 'prompt') prompt.partial('');
  if (done) status(mode, 'Σταμάτησε.');
  paintRec();
}

function enqueue(mode, pcm, sec, who) {
  rec.pending++;
  const o = rec.opts;
  const line = mode === 'meeting' ? addLine({ t: sec, who, text: 'Μεταγραφή…', pending: true }) : null;
  rec.queue = rec.queue.then(async () => {
    try {
      // translating with Mistral: one call straight into the target language, no Greek step to wait for
      const direct = mode === 'prompt' && !o.offline && o.provider === 'mistral' && prompt.cloudTarget();
      const r = await retry(() => o.offline
        ? host.whisper(wav(pcm), { translate: mode === 'prompt' && prompt.whisperEnglish(), lang: o.lang, model: o.wmodel }).then(text => ({ text }))
        : direct ? translateAudio(o.key, pcm, direct).then(text => ({ text, translated: true }))
        : transcribe(o.key, o.model, pcm, { bias: o.bias, provider: o.provider, lang: o.lang }));
      const text = (r.text || '').trim();
      if (line) {
        if (text) Object.assign(line, { text, pending: false }); else meeting.lines.splice(meeting.lines.indexOf(line), 1);
        renderLines();
      } else if (text && mode === 'prompt') prompt.final(text, who, r.translated);
      else if (text) await insertDictation(text); // one paste at a time: the next phrase overwrites the clipboard
    } catch (e) {
      rec.failed = true;
      if (line) { Object.assign(line, { text: '[δεν μεταγράφηκε: ' + e.message + ']', pending: false }); renderLines(); }
      status(mode, e.message, true);
    }
    rec.pending--;
  });
}

async function stopRec() {
  if (rec.local) return stopLocal();
  const mode = rec.mode;
  rec.mode = null;
  rec.busy = true;
  clearInterval(rec.timer);
  rec.closers.forEach(c => c());
  host.keepAwake(false);
  panel(mode).querySelector('.level i').style.width = '0';
  paintRec();
  rec.tracks.forEach(t => t.cut(true));
  if (rec.pending) status(mode, 'Ολοκλήρωση μεταγραφής…');
  await rec.queue; // never drop queued chunks
  const t = rec.tracks[0];
  if (rec.diarize && t && t.allLen > SR && meeting.lines.length) {
    status(mode, 'Αναγνώριση ομιλητών…');
    try {
      const j = await retry(() => transcribe(rec.opts.key, rec.opts.model, concat(t.all), { diarize: true, bias: rec.opts.bias, provider: rec.opts.provider, lang: rec.opts.lang }));
      const lines = speakerLines(j);
      if (lines.length) setLines(lines);
      status(mode, t.overflow ? 'Έτοιμο. Οι ομιλητές χωρίστηκαν μόνο στην πρώτη ώρα.' : 'Έτοιμο.');
    } catch (e) {
      status(mode, 'Οι ομιλητές δεν χωρίστηκαν (' + e.message + '). Κρατήθηκε η μεταγραφή με τις ώρες.', true);
    }
  } else if (!rec.failed) {
    status(mode, mode === 'dictate' && !$('#dictText').value.trim() ? 'Δεν αναγνωρίστηκε ομιλία.' : 'Έτοιμο.');
  }
  rec.tracks = [];
  rec.busy = false;
  paintRec();
}

document.querySelectorAll('.rec').forEach(b => b.onclick = () => toggleRec(b.dataset.mode));

// ---------- dictation ----------

const dict = $('#dictText');
let caret = null; // where the next dictated text goes; the record button steals focus, so remember it
dict.value = prefs.get('dictation', '');
dict.addEventListener('blur', () => { caret = dict.selectionEnd; });
dict.addEventListener('input', () => prefs.set('dictation', dict.value));

async function insertDictation(text) {
  if (MINI) {
    // into the app that has focus; if VOICE itself has it, the text waits on the clipboard
    const typed = await host.type(text + ' ');
    return status('dictate', (typed ? '✓ ' : 'Στο πρόχειρο (Ctrl+V): ') + text);
  }
  const at = Math.min(caret ?? dict.value.length, dict.value.length);
  const before = dict.value.slice(0, at);
  const ins = (before && !/\s$/.test(before) ? ' ' : '') + text;
  dict.value = before + ins + dict.value.slice(at);
  caret = at + ins.length;
  prefs.set('dictation', dict.value);
}

$('#dictCopy').onclick = () => copyText(dict.value);
$('#dictRead').onclick = () => { $('#readText').value = dict.value; show('read'); speech.start(dict.value, rate()); };
$('#dictClear').onclick = () => { dict.value = ''; caret = null; prefs.set('dictation', ''); };

// ---------- mini window ----------

if (host.openMini && !MINI) {
  $('#miniBtn').hidden = $('#miniHint').hidden = false;
  $('#miniBtn').onclick = () => { if (rec.mode) return toast('Σταμάτησε πρώτα την ηχογράφηση.'); host.openMini(); };
}
$('#expandBtn').onclick = async () => { if (rec.mode) await stopRec(); host.closeMini(); };
$('#pauseBtn').onclick = () => {
  if (!rec.mode) return;
  rec.paused = !rec.paused;
  if (rec.paused) rec.tracks.forEach(t => t.cut(true)); // what was said before the pause still gets typed
  $('#pauseBtn').classList.toggle('on', rec.paused);
  $('#pauseBtn').innerHTML = icon(rec.paused ? 'mic' : 'pause');
  status('dictate', rec.paused ? 'Σε παύση' : 'Ακούω…');
};

// ---------- meeting transcript ----------

const meeting = { lines: [] };

function setLines(lines) { meeting.lines = lines; renderLines(); }
function addLine(l) { meeting.lines.push(l); renderLines(); return l; }

function renderLines() {
  const box = $('#transcript');
  // two sources finish out of order; keep the transcript in time order
  meeting.lines.sort((a, b) => a.t - b.t);
  if (!meeting.lines.length) {
    box.innerHTML = '<p class="empty-hint">Η μεταγραφή εμφανίζεται εδώ, με ώρα και ομιλητή.</p>';
  } else {
    box.replaceChildren(...meeting.lines.map(l => {
      const row = document.createElement('div');
      row.className = 'line' + (l.pending ? ' pending' : '');
      const time = document.createElement('time');
      time.textContent = stamp(l.t);
      const p = document.createElement('span');
      if (l.who) { const b = document.createElement('b'); b.textContent = l.who + ':'; p.append(b); }
      p.append(l.text);
      row.append(time, p);
      return row;
    }));
    box.scrollTop = box.scrollHeight;
  }
  prefs.set('meeting', JSON.stringify(meeting.lines.filter(l => !l.pending)));
}

const meetingText = () => meeting.lines.filter(l => !l.pending)
  .map(l => `[${stamp(l.t)}] ${l.who ? l.who + ': ' : ''}${l.text}`).join('\n');

try { meeting.lines = JSON.parse(prefs.get('meeting', '[]')); } catch { meeting.lines = []; }
renderLines();

$('#meetCopy').onclick = () => copyText(meetingText());
$('#meetSave').onclick = async () => {
  const text = meetingText();
  if (!text) return;
  const d = new Date(), p = n => String(n).padStart(2, '0');
  const name = `Σύσκεψη_${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}_${p(d.getHours())}-${p(d.getMinutes())}.md`;
  try {
    if (await host.save('# Μεταγραφή σύσκεψης\n\n' + text + '\n', name)) toast('Αποθηκεύτηκε.');
  } catch (e) { toast('Δεν αποθηκεύτηκε: ' + e.message); }
};
$('#meetClear').onclick = () => { if (!rec.mode && !rec.busy) setLines([]); };

// ---------- read aloud ----------

const rate = () => Number(prefs.get('rate', '1'));

const speech = {
  token: 0,
  async start(text, r) {
    if (!text.trim()) return;
    this.stop();
    const my = ++this.token;
    this.paint(true);
    try {
      const eng = ttsEngine(), key = eng !== 'local' && await apiKey(eng);
      if (key) await this.play(text, t => synthesize(key, eng, t, ttsVoice(eng)), 'audio/mpeg', r, my);
      else if (ttsVoice('local') === 'mms' && host.mms) await this.play(text, t => host.mms(t), 'audio/wav', r, my);
      else if (host.speak) { host.speak(text, r); return; }       // Android: window.onSpeakEnd ends it
      else if (host.synth && await host.piper()) await this.play(text, t => host.synth(t, r), 'audio/wav', 1, my);
      else await this.web(text, r, my);
    } catch (e) {
      toast('Η ανάγνωση απέτυχε: ' + e.message);
    }
    if (my === this.token) this.paint(false);
  },
  // Piper (rate built into the audio) or a cloud voice (rate by playback speed)
  async play(text, synth, type, rate, my) {
    const parts = ttsChunks(text);
    let next = synth(parts[0]);
    for (let i = 0; i < parts.length; i++) {
      const buf = await next;
      if (my !== this.token) return;
      next = i + 1 < parts.length ? synth(parts[i + 1]) : null; // synthesize ahead while this plays
      await new Promise(done => {
        const a = this.audio = new Audio(URL.createObjectURL(new Blob([buf], { type })));
        a.playbackRate = rate;
        a.onended = a.onerror = a.onpause = () => { URL.revokeObjectURL(a.src); done(); };
        a.play().catch(done);
      });
      if (my !== this.token) return;
    }
  },
  async web(text, r, my) {
    const voice = await greekVoice();
    if (!voice) throw new Error('δεν βρέθηκε ελληνική φωνή στο σύστημα.');
    for (const part of ttsChunks(text)) {
      if (my !== this.token) return;
      await new Promise(done => {
        const u = Object.assign(new SpeechSynthesisUtterance(part), { voice, lang: voice.lang, rate: r });
        u.onend = u.onerror = done;
        speechSynthesis.speak(u);
      });
    }
  },
  stop() {
    this.token++;
    if (this.audio) { this.audio.pause(); this.audio = null; }
    if (window.speechSynthesis) speechSynthesis.cancel();
    if (host.stopSpeak) host.stopSpeak();
    this.paint(false);
  },
  paint(on) {
    this.on = on;
    if (!on && this.done) { const d = this.done; this.done = null; d(); }
    $('#speakBtn').innerHTML = icon(on ? 'stop' : 'play') + `<span>${on ? 'Διακοπή' : 'Ανάγνωση'}</span>`;
  },
};
window.onSpeakEnd = () => speech.paint(false);

async function greekVoice() {
  if (!window.speechSynthesis) return null;
  let v = speechSynthesis.getVoices();
  if (!v.length) {
    await new Promise(r => { speechSynthesis.onvoiceschanged = r; setTimeout(r, 1500); });
    v = speechSynthesis.getVoices();
  }
  return v.find(x => x.lang.toLowerCase().startsWith('el')) || null;
}

async function ttsInfo() {
  let msg;
  const eng = ttsEngine();
  if (eng !== 'local') msg = (await apiKey(eng)) ? `Φωνή: ${PROVIDERS[eng].name}, ${$('#ttsVoice').selectedOptions[0]?.textContent || ttsVoice(eng)} (με internet).`
    : `Η φωνή ${PROVIDERS[eng].name} θέλει κλειδί· διαβάζει η τοπική φωνή.`;
  else if (ttsVoice('local') === 'mms' && mmsOk) msg = 'Φωνή: Meta MMS, ελληνική, τοπικά στον υπολογιστή.';
  else if (host.ttsInfo) msg = host.ttsInfo();
  else if (host.piper && await host.piper()) msg = 'Φωνή: Piper, ελληνική, τοπικά στον υπολογιστή.';
  else {
    const v = await greekVoice();
    msg = v ? 'Φωνή: ' + v.name : 'Δεν βρέθηκε ελληνική φωνή. Στα Windows: Ρυθμίσεις → Ώρα και γλώσσα → Ομιλία → Προσθήκη φωνών → Ελληνικά.';
  }
  $('#ttsInfo').textContent = msg;
}

// open a PDF or a text file to read aloud; pdf.js loads only when first needed
const loadScript = src => new Promise((ok, fail) => document.head.append(Object.assign(document.createElement('script'), { src, onload: ok, onerror: () => fail(new Error('λείπει το ' + src)) })));
async function fileText(file) {
  if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') return file.text();
  if (!window.pdfjsLib) {
    // the worker script on the page makes pdf.js run in this thread: a file:// page cannot start a Worker everywhere
    await loadScript('vendor/pdfjs/pdf.worker.min.js');
    await loadScript('vendor/pdfjs/pdf.min.js');
  }
  const pdf = await pdfjsLib.getDocument({ data: await file.arrayBuffer() }).promise;
  const pages = [];
  for (let i = 1; i <= pdf.numPages; i++) {
    const c = await (await pdf.getPage(i)).getTextContent();
    pages.push(c.items.map(it => it.str + (it.hasEOL ? '\n' : '')).join(''));
  }
  return pages.join('\n\n');
}
$('#openFile').onclick = () => $('#fileInput').click();
$('#fileInput').onchange = async e => {
  const f = e.target.files[0];
  e.target.value = '';
  if (!f) return;
  toast('Άνοιγμα: ' + f.name);
  try {
    const text = (await fileText(f)).replace(/[ \t]+\n/g, '\n').trim();
    if (!text) return toast('Το αρχείο δεν έχει κείμενο (σκαναρισμένο PDF;).');
    $('#readText').value = text;
  } catch (err) { toast('Δεν άνοιξε: ' + err.message); }
};

$('#speakBtn').onclick = () => speech.on ? speech.stop() : speech.start($('#readText').value, rate());
segmented($('#ratePick'), [['0.8', 'Αργά'], ['1', 'Κανονικά'], ['1.25', 'Γρήγορα']], prefs.get('rate', '1'), v => prefs.set('rate', v));
speech.paint(false);

// ---------- settings: reading voice ----------

const ttsEngine = () => prefs.get('ttsEngine', 'local');
const ttsVoice = eng => prefs.get('ttsVoice-' + eng, eng === 'openai' ? 'marin' : eng === 'local' ? (mmsOk ? 'mms' : 'piper') : ''); // MMS sounds better than Piper
let mmsOk = false; // desktop: the Meta MMS Greek voice is downloaded (Settings → Τοπικά μοντέλα)
async function paintTts() {
  const eng = ttsEngine(), sel = $('#ttsVoice');
  segmented($('#ttsPick'), [['local', 'Τοπικά'], ['mistral', 'Mistral'], ['openai', 'OpenAI']], eng, v => { prefs.set('ttsEngine', v); paintTts(); });
  $('#ttsVoiceRow').hidden = eng === 'local' && !mmsOk;
  $('#ttsHint').textContent = eng === 'local' ? (host.speak ? 'Η φωνή της συσκευής: δωρεάν, χωρίς internet.'
    : 'Δωρεάν, χωρίς internet. Piper: πιο γρήγορη, πιο ρομποτική. Meta MMS: πιο φυσική (άδεια μόνο για μη εμπορική χρήση).')
    : eng === 'mistral' ? 'Voxtral TTS: δεν έχει ελληνική φωνή, αλλά οι αγγλικές (π.χ. James) διαβάζουν καλά ελληνικά. Θέλει internet και κλειδί, ≈ $16 ανά εκατομμύριο χαρακτήρες.'
    : 'GPT-4o mini TTS: μιλά ελληνικά, θέλει internet και κλειδί, ≈ $0.015 το λεπτό. Η OpenAI το αποσύρει στις 6/1/2027.';
  if (eng === 'local' && !mmsOk) return;
  let list = eng === 'openai' ? OPENAI_VOICES.map(v => [v, v]) : eng === 'local' ? [['piper', 'Piper (rapunzelina)'], ['mms', 'Meta MMS']] : [];
  if (eng === 'mistral') {
    try { list = JSON.parse(prefs.get('mistralVoices', '[]')); } catch { list = []; }
    const key = await apiKey('mistral');
    if (!list.length && key) {
      try { list = await mistralVoices(key); prefs.set('mistralVoices', JSON.stringify(list)); } catch (e) { $('#ttsHint').textContent = 'Οι φωνές δεν φορτώθηκαν: ' + e.message; }
    }
    if (!ttsVoice('mistral') && list.length) prefs.set('ttsVoice-mistral', (list.find(([, n]) => n.startsWith('James ')) || list[0])[0]);
  }
  sel.replaceChildren(...list.map(([id, name]) => new Option(name, id)));
  sel.value = ttsVoice(eng);
  sel.onchange = () => prefs.set('ttsVoice-' + eng, sel.value);
}
paintTts();
// ---------- settings: offline models (desktop) — download once, then no internet needed ----------

const fmtMB = n => n >= 1e9 ? (n / 1e9).toFixed(2).replace('.', ',') + ' GB' : Math.round(n / 1e6) + ' MB';
const packSize = p => p.files.reduce((n, f) => n + f.size, 0);
const modelState = {}; // id → { ready, done, total, state }
async function refreshModels() {
  for (const s of await host.models.status()) modelState[s.id] = { ...modelState[s.id], ready: s.ready };
  mmsOk = !!modelState.voice?.ready;
  whisperModels = await host.whisperReady();
  paintModelList(); paintTts(); paintEngine();
}
function paintModelList() {
  $('#modelList').replaceChildren(...MODEL_PACKS.map(p => {
    const st = modelState[p.id] || {}, busy = st.state === 'down';
    const row = document.createElement('div');
    row.className = 'model-row';
    const pct = st.total ? Math.round(st.done / st.total * 100) : 0;
    row.innerHTML = `<div class="model-info"><b></b><span class="hint"></span>
      <div class="level"${busy ? '' : ' hidden'}><i style="width:${pct}%"></i></div></div><button class="pill-button"></button>`;
    row.querySelector('b').textContent = p.name;
    row.querySelector('.hint').textContent = st.ready ? '✓ Εγκατεστημένο · ' + fmtMB(packSize(p))
      : busy ? `Λήψη… ${fmtMB(st.done)} από ${fmtMB(st.total)} (${pct}%)`
      : (st.state && st.state.startsWith('error') ? '⚠ ' + st.state.slice(7) + ' · ' : '') + p.note + ' · ' + fmtMB(packSize(p));
    const b = row.querySelector('button');
    b.textContent = st.ready ? 'Διαγραφή' : busy ? 'Ακύρωση' : st.state === 'stopped' ? 'Συνέχεια λήψης' : 'Λήψη';
    if (!st.ready && !busy) b.classList.add('primary');
    b.onclick = async () => {
      if (st.ready) { await host.models.remove(p.id); return refreshModels(); }
      if (busy) return host.models.cancel(p.id);
      modelState[p.id] = { ...st, state: 'down', done: 0, total: packSize(p) };
      paintModelList();
      host.models.download(p.id);
    };
    return row;
  }));
}
if (host.models) {
  host.models.onProgress(p => {
    modelState[p.id] = { ...modelState[p.id], ...p };
    if (p.state !== 'down') refreshModels(); else paintModelList();
  });
  $('#downloadAll').onclick = () => MODEL_PACKS.forEach(p => { if (!modelState[p.id]?.ready && modelState[p.id]?.state !== 'down') host.models.download(p.id); });
  refreshModels();
} else {
  $('#modelsGroup').remove(); $('#modelsCard').remove();
}

// ---------- first run: free models on this computer, or a school's API key ----------

async function welcome() {
  if (MINI || prefs.get('welcomed', '') || !host.models) return;
  if (await apiKey('mistral') || await apiKey('openai')) return prefs.set('welcomed', '1');
  const status = await host.models.status();
  if (status.every(s => s.ready)) return prefs.set('welcomed', '1');
  const total = MODEL_PACKS.reduce((n, p) => n + packSize(p), 0);
  $('#welcomeSize').textContent = fmtMB(total);
  $('#welcome').hidden = false;
  const done = () => { prefs.set('welcomed', '1'); $('#welcome').hidden = true; show('settings'); };
  $('#welcomeLocal').onclick = () => {
    prefs.set('provider', 'local'); prefs.set('ttsEngine', 'local'); paintEngine(); paintTts();
    done(); $('#downloadAll').click(); $('#modelsCard').scrollIntoView();
  };
  $('#welcomeKey').onclick = () => { done(); $('#keyCard').scrollIntoView(); };
}
welcome();

// ---------- settings ----------

const keyField = $('#keyField');
async function paintKey() {
  keyField.value = '';
  const p = PROVIDERS[keyProvider()];
  $('#keyWhere').dataset.local = provider() === 'local';
  keyField.placeholder = (await apiKey(keyProvider())) ? '•••••••• αποθηκευμένο — γράψε νέο για αλλαγή' : `Επικόλλησε εδώ το κλειδί της ${p.name}`;
  $('#keyTitle').textContent = `Κλειδί API της ${p.name}`;
  $('#keyWhere').textContent = (provider() === 'local' ? 'Χρειάζεται μόνο για τη μετάφραση με internet. ' : '') +
    `Το παίρνεις από το ${p.console}. Κάθε υπηρεσία έχει δικό της κλειδί· αποθηκεύεται κρυπτογραφημένο μόνο σε αυτή τη συσκευή.`;
  keyStatus('');
}
function keyStatus(text, cls = '') { const k = $('#keyStatus'); k.textContent = text; k.className = 'hint ' + cls; }

$('#keySave').onclick = async () => {
  const k = keyField.value.trim();
  if (!k) return;
  try {
    await setApiKey(k);
    await paintKey();
    toast('Το κλειδί αποθηκεύτηκε.');
    $('#keyCheck').click();
  } catch (e) { keyStatus('Δεν αποθηκεύτηκε: ' + e.message, 'bad'); }
};
$('#keyCheck').onclick = async () => {
  const k = keyField.value.trim() || await apiKey(keyProvider());
  if (!k) return keyStatus('Δεν υπάρχει κλειδί.', 'bad');
  keyStatus('Έλεγχος…');
  const r = await checkKey(k, keyProvider());
  keyStatus({ ok: '✓ Το κλειδί λειτουργεί.', bad: '✗ Το κλειδί δεν είναι σωστό.',
    offline: 'Δεν υπάρχει σύνδεση στο διαδίκτυο.', error: 'Η υπηρεσία δεν απάντησε σωστά· δοκίμασε ξανά.' }[r], r === 'ok' ? 'ok' : 'bad');
};

// the key card serves the chosen cloud; with "Τοπικά" it stays on the cloud used for translation
const keyProvider = () => provider() !== 'local' ? provider() : prefs.get('keyProvider', 'mistral');
// what this device spent this month, per provider (the providers' own consoles have the exact bill)
const month = () => new Date().toISOString().slice(0, 7);
function usage() {
  try { const u = JSON.parse(prefs.get('usage', '{}')); return u.month === month() ? u : { month: month() }; } catch { return { month: month() }; }
}
meter.on = (p, sec, tokens, usd) => {
  const u = usage(), x = u[p] || (u[p] = { sec: 0, tokens: 0, usd: 0 });
  x.sec += sec; x.tokens += tokens; x.usd += usd;
  prefs.set('usage', JSON.stringify(u));
  paintUsage();
};
function paintUsage() {
  const x = usage()[keyProvider()] || { sec: 0, tokens: 0, usd: 0 };
  const m = ['Ιανουάριος', 'Φεβρουάριος', 'Μάρτιος', 'Απρίλιος', 'Μάιος', 'Ιούνιος', 'Ιούλιος', 'Αύγουστος',
    'Σεπτέμβριος', 'Οκτώβριος', 'Νοέμβριος', 'Δεκέμβριος'][new Date().getMonth()]; // Intl gives the genitive
  $('#usage').textContent = `${m}: ${(x.sec / 60).toFixed(1)} λεπτά · ${Math.round(x.tokens).toLocaleString('el')} tokens · ≈ $${x.usd.toFixed(3)}`;
  $('#usage').title = 'Μετρημένο σε αυτή τη συσκευή, με τις τιμές καταλόγου. Ο ακριβής λογαριασμός είναι στην κονσόλα της υπηρεσίας.';
}
const perHour = id => PRICES[id] && PRICES[id].min ? ` · $${(PRICES[id].min * 60).toFixed(2)}/ώρα` : '';

function paintEngine() {
  const p = provider(), list = models(p).map(([id, label]) => [id, label + perHour(id)]);
  segmented($('#providerPick'), Object.entries(PROVIDERS).map(([id, x]) => [id, x.name]), p, v => {
    prefs.set('provider', v);
    if (v !== 'local') prefs.set('keyProvider', v);
    paintEngine();
  });
  $('#modelPick').hidden = !list.length;
  segmented($('#modelPick'), list, model(p), v => prefs.set('model-' + p, v));
  $('#engineHint').textContent = p !== 'local'
    ? `${PROVIDERS[p].name}: πιο ακριβές, θέλει internet και κλειδί, χρεώνεται ανά λεπτό. Τα «mini» είναι φθηνότερα και εξίσου καλά στα ελληνικά.`
    : host.listen ? 'Στη συσκευή: δωρεάν, ο ήχος δεν φεύγει από το tablet.'
    : 'Whisper σε αυτόν τον υπολογιστή, με την κάρτα γραφικών: δωρεάν, χωρίς internet, ~1–2 δευτερόλεπτα ανά φράση. Medium: η καλή ισορροπία· Large: ακριβέστερο αλλά πολύ αργό εδώ. Χωρίς internet ή κλειδί, η εφαρμογή περνά μόνη της στο τοπικό.';
  paintKey();
  paintUsage();
}
const termsField = $('#termsField');
termsField.value = prefs.get('terms', DEFAULT_TERMS);
termsField.addEventListener('change', () => prefs.set('terms', termsField.value));
const diarizeBox = $('#diarizeBox');
diarizeBox.checked = prefs.get('diarize', '1') === '1';
diarizeBox.onchange = () => prefs.set('diarize', diarizeBox.checked ? '1' : '0');

// one audio input for every page; Settings, Σύσκεψη and the Teleprompt bar show the same choice
const SOURCES = [['mic', 'Μικρόφωνο'], ['system', 'Ήχος υπολογιστή'], ['both', 'Και τα δύο']];
function paintSource() {
  const v = prefs.get('source', 'mic'), pick = x => { prefs.set('source', x); paintSource(); };
  segmented($('#sourcePick'), SOURCES, v, pick);
  segmented($('#inputPick'), SOURCES, v, pick);
  $('#promptSource').value = v;
  checkMic();
}

// ---------- no microphone: the app has nothing to caption, so it says so and greys out recording ----------
// a phone that joined (Teleprompt → + Μικρόφωνο) counts as a microphone; «Ήχος υπολογιστή» needs none
async function checkMic() {
  let has = true;
  try { has = (await navigator.mediaDevices.enumerateDevices()).some(d => d.kind === 'audioinput'); } catch { /* can't tell: don't block */ }
  noMic = !has && !mics?.phones() && (!host.system || prefs.get('source', 'mic') !== 'system');
  $('#noMic').hidden = !noMic;
  paintRec();
}
navigator.mediaDevices?.addEventListener?.('devicechange', checkMic); // plugged in or out

// ---------- extra mics (desktop): phones/tablets around the room send their sound; whoever hears best leads ----------

const mics = (() => {
  if (!host.wifiStart || MINI) { $('#micsDlg').remove(); return null; } // the mini window keeps to the plain mic
  const mixer = new AutoMixer(), seen = new Map(), list = $('#micsList'), dlg = $('#micsDlg'), btn = $('#addMicBtn');
  const off = new Set(JSON.parse(prefs.get('micsOff', '[]'))); // the devices whose tick was taken away
  let code = prefs.get('wifiCode', '');
  if (!/^\d{4}$/.test(code)) prefs.set('wifiCode', code = String(1000 + Math.floor(Math.random() * 9000)));
  let timer = null, t0 = 0, made = 0, rows = null, info = null;
  const self = {
    out: null,
    get running() { return !!timer; },
    // runs from the first time a phone is added until VOICE closes, so the phones reconnect on their own
    async start() {
      if (timer) return;
      timer = setInterval(tick, 100);
      t0 = performance.now();
      made = 0;
      try {
        info = await host.wifiStart(code, (id, name, pcm) => {
          if (mixer.inputs.size >= 9 && !mixer.inputs.has(id)) return; // a handful of devices, not the whole school
          self.push(id, pcm, name || 'Συσκευή');
          prefs.set('micsUsed', '1');
        });
      } catch (e) {
        clearInterval(timer);
        timer = null;
        info = null;
        throw e;
      }
    },
    push(id, pcm, name) {
      if (!mixer.inputs.has(id)) { mixer.push(id, pcm, name); mixer.inputs.get(id).muted = off.has(id); } else mixer.push(id, pcm, name);
      seen.set(id, performance.now());
    },
    drop(id) { mixer.inputs.delete(id); seen.delete(id); },
    // connected phones whose tick is on
    phones: () => [...mixer.inputs.keys()].filter(id => id !== 'local' && !off.has(id)).length,
  };
  let phonesBefore = 0;
  function tick() {
    // samples owed by the clock, not one block per tick: timers drift and stall, the audio must not
    const n = Math.round((performance.now() - t0) * SR / 1000) - made;
    if (n <= 0) return;
    made += n;
    for (const [id, t] of seen) if (performance.now() - t > 5000) self.drop(id); // the phone went away
    // never zero microphones: this computer's mic switches off only while a ticked phone is connected
    const phones = self.phones(), local = mixer.inputs.get('local');
    if (local) local.muted = off.has('local') && phones > 0;
    if ((phones > 0) !== (phonesBefore > 0)) checkMic();
    phonesBefore = phones;
    const pcm = mixer.pull(n);
    if (self.out) self.out(pcm);
    paint();
  }
  function paintButton() {
    const n = [...mixer.inputs.keys()].filter(id => id !== 'local').length;
    btn.innerHTML = n ? icon('phone', 20) + `<span>Μικρόφωνα: ${n + 1}</span>` : icon('add', 20) + '<span>Μικρόφωνο</span>';
  }
  function paint() {
    paintButton();
    if (dlg.hidden) return;
    // this computer's mic is always listed, so its tick can be taken away before recording
    const ins = [['local', mixer.inputs.get('local') || { name: 'Αυτός ο υπολογιστής', level: 0, gain: 0 }],
      ...[...mixer.inputs].filter(([id]) => id !== 'local')];
    const key = ins.map(([id]) => id).join('|');
    if (key !== rows) {
      rows = key;
      list.replaceChildren(...ins.map(([id, i]) => {
        const row = document.createElement('label');
        row.className = 'mic-row check';
        row.dataset.id = id;
        row.innerHTML = '<input type="checkbox"><span></span><div class="level"><i></i></div>';
        row.querySelector('span').textContent = i.name;
        const box = row.querySelector('input');
        box.checked = !off.has(id);
        box.onchange = () => {
          if (box.checked) off.delete(id); else off.add(id);
          prefs.set('micsOff', JSON.stringify([...off]));
          const live = mixer.inputs.get(id);
          if (live && id !== 'local') live.muted = !box.checked; // tick() decides for this computer's mic
        };
        return row;
      }));
      if (ins.length === 1) list.insertAdjacentHTML('beforeend', '<p class="hint">Κανένα κινητό ακόμα.</p>');
    }
    let lead = null;
    for (const [, i] of mixer.inputs) if (!lead || i.gain > lead.gain) lead = i;
    const box = list.querySelector('.mic-row[data-id="local"] input'), alone = !self.phones();
    box.disabled = alone;
    box.checked = alone || !off.has('local');
    box.parentElement.title = alone ? 'Μένει ανοιχτό όσο δεν υπάρχει άλλο μικρόφωνο.' : '';
    for (const row of list.querySelectorAll('.mic-row')) {
      const i = mixer.inputs.get(row.dataset.id);
      row.querySelector('.level i').style.width = i ? Math.min(100, Math.sqrt(i.level / 6000) * 100) + '%' : '0';
      row.classList.toggle('lead', !!i && i === lead && i.gain > 0.5 && mixer.inputs.size > 1);
    }
  }
  async function open() {
    dlg.hidden = false;
    rows = null;
    $('#micsQr').textContent = '';
    try { await Promise.all([self.start(), window.qrcode || loadScript('vendor/qr/qrcode.js')]); } catch (e) {
      $('#micsWifi').textContent = 'Τα κινητά δεν μπορούν να συνδεθούν: ' + e.message;
      return;
    }
    $('#micsWifi').innerHTML = info.ssid ? `Σύνδεσε το κινητό ή το tablet στο Wi-Fi <b></b>.` : 'Σύνδεσε το κινητό ή το tablet στο ίδιο Wi-Fi με αυτόν τον υπολογιστή.';
    if (info.ssid) $('#micsWifi b').textContent = info.ssid;
    if (!info.addrs.length) return void ($('#micsAddr').textContent = 'Ο υπολογιστής δεν είναι συνδεδεμένος σε δίκτυο.');
    const qr = qrcode(0, 'M');
    qr.addData(pairText({ ...info, code }));
    qr.make();
    $('#micsQr').innerHTML = qr.createSvgTag({ cellSize: 6, margin: 4, scalable: true });
    $('#micsAddr').textContent = `${info.addrs.join(' ή ')} · κωδικός ${code}`;
    paint();
  }
  btn.hidden = $('#micsOpen').hidden = false;
  btn.onclick = $('#micsOpen').onclick = open;
  $('#micsClose').onclick = () => { dlg.hidden = true; };
  dlg.onclick = e => { if (e.target === dlg) dlg.hidden = true; };
  addEventListener('keydown', e => { if (e.key === 'Escape') dlg.hidden = true; });
  paintButton();
  if (prefs.get('micsUsed', '')) self.start().catch(() => {}); // phones paired before reconnect without opening the page
  return self;
})();

// ---------- Μικρόφωνο PC (phone/tablet): scan the computer's QR code once, then one button ----------

const pcmic = (() => {
  if (host.system) { $('#pcmic').remove(); document.querySelector('[data-page="pcmic"]').remove(); return null; }
  const video = $('#scanVideo'), wave = $('#micWave'), btn = $('#pcmicBtn'), levels = new Array(60).fill(0);
  let pair = parsePair(prefs.get('pcPair', '')), stream = null, scanTimer = null, poll = null, scanning = false, last = {};
  let id = prefs.get('sendId', '');
  if (!id) prefs.set('sendId', id = Math.random().toString(36).slice(2, 10));
  const scanSay = t => { $('#scanStatus').textContent = t; };
  window.onCamera = (_, msg) => scanSay(msg);

  function view() {
    $('#pairView').hidden = !scanning;
    $('#micView').hidden = scanning;
    $('#pairBack').hidden = !pair;
    if (pair) $('#micPc').textContent = pair.pc;
  }

  async function scan() {
    scanning = true;
    view();
    scanSay('Ανοίγει η κάμερα…');
    try {
      await (window.jsQR || loadScript('vendor/qr/jsQR.js'));
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
    } catch (e) {
      return scanSay(e.name === 'NotAllowedError' ? 'Χρειάζεται η κάμερα για τον κωδικό QR.' : 'Η κάμερα δεν άνοιξε: ' + e.message);
    }
    if (!scanning) return stopCamera(); // left the page while the camera was opening
    video.srcObject = stream;
    await video.play().catch(() => {});
    scanSay('Στόχευσε τον κωδικό QR στην οθόνη του υπολογιστή.');
    const c = document.createElement('canvas'), g = c.getContext('2d', { willReadFrequently: true });
    scanTimer = setInterval(() => {
      if (!video.videoWidth) return;
      const k = Math.min(1, 640 / video.videoWidth);
      c.width = Math.round(video.videoWidth * k);
      c.height = Math.round(video.videoHeight * k);
      g.drawImage(video, 0, 0, c.width, c.height);
      const hit = jsQR(g.getImageData(0, 0, c.width, c.height).data, c.width, c.height, { inversionAttempts: 'dontInvert' });
      if (!hit) return;
      const p = parsePair(hit.data);
      if (!p) return scanSay('Αυτός δεν είναι κωδικός του VOICE.');
      if (navigator.vibrate) navigator.vibrate(30);
      pair = p;
      prefs.set('pcPair', hit.data);
      stopScan();
      if (!host.micState?.().on) return start();
      host.micStop(); // was sending to another computer: let the service end first
      setTimeout(start, 800);
    }, 250);
  }

  function stopCamera() {
    clearInterval(scanTimer);
    scanTimer = null;
    if (stream) stream.getTracks().forEach(t => t.stop());
    stream = null;
    video.srcObject = null;
  }
  function stopScan() { scanning = false; stopCamera(); view(); }

  function start() {
    if (rec.mode) return toast('Σταμάτησε πρώτα την ηχογράφηση.');
    if (!host.micStart) return toast('Μόνο στην εφαρμογή Android.');
    host.micStart({ ...pair, id, name: host.deviceName() });
  }

  function paint() {
    const st = host.micState ? host.micState() : {};
    if (st.on !== last.on || st.ok !== last.ok || st.problem !== last.problem) {
      last = st;
      btn.classList.toggle('on', !!st.on);
      btn.innerHTML = icon('mic', 56);
      btn.setAttribute('aria-label', st.on ? 'Σταμάτημα μικροφώνου' : 'Έναρξη μικροφώνου');
      const state = !st.on ? 'off' : st.problem === 'code' ? 'bad' : st.ok ? 'ok' : 'wait';
      $('#micLight').className = 'light ' + state;
      $('#micStatus').textContent = {
        off: 'Πάτησε το μικρόφωνο για να στέλνει ήχο στον υπολογιστή. Συνεχίζει και με κλειστή οθόνη.',
        ok: 'Συνδεδεμένο: ο υπολογιστής ακούει από εδώ. Μπορείς να κλείσεις την οθόνη.',
        wait: 'Ψάχνει τον υπολογιστή… Ίδιο Wi-Fi; Είναι ανοιχτό το VOICE εκεί;',
        bad: 'Ο υπολογιστής δεν δέχεται αυτή τη συσκευή: σάρωσε ξανά τον κωδικό QR.',
      }[state];
    }
    levels.push(st.on ? Math.min(1, Math.sqrt((st.level || 0) / 6000)) : 0);
    levels.shift();
    const w = wave.width = wave.clientWidth * devicePixelRatio, h = wave.height = 80 * devicePixelRatio, g = wave.getContext('2d');
    g.fillStyle = getComputedStyle(document.body).getPropertyValue(st.on && st.ok ? '--ok' : '--muted');
    const bw = w / levels.length;
    levels.forEach((l, i) => { const bh = Math.max(2 * devicePixelRatio, l * h); g.fillRect(i * bw + bw * 0.2, (h - bh) / 2, bw * 0.6, bh); });
  }

  btn.onclick = () => (host.micState?.().on ? host.micStop() : start());
  $('#repairBtn').onclick = scan;
  $('#pairBack').onclick = stopScan;
  return {
    // the first visit asks for every permission at once, then opens the camera; afterwards straight to the mic
    async enter() {
      clearInterval(poll); poll = setInterval(paint, 100); paint();
      if (pair) return view();
      if (host.micPermissions && !prefs.get('pcmicAsked', '')) {
        prefs.set('pcmicAsked', '1');
        scanning = true; view();
        scanSay('Το VOICE θα ζητήσει κάμερα (για τον κωδικό QR), μικρόφωνο και ειδοποιήσεις.');
        await host.micPermissions();
        if (!scanning) return; // left the tab while Android was asking
      }
      scan();
    },
    leave() { if (scanning) { scanning = false; stopCamera(); } clearInterval(poll); poll = null; },
  };
})();

if (host.system) {
  $('#promptSource').onchange = e => { prefs.set('source', e.target.value); paintSource(); };
  paintSource();
} else {
  $('#sourcePick').remove(); // phones/tablets: the microphone only
  $('#promptSource').remove();
  $('#inputCard').remove();
}
segmented($('#langPick'), host.listen ? [['el', 'Ελληνικά'], ['en', 'English']] : [['el', 'Ελληνικά'], ['en', 'English'], ['auto', 'Αυτόματα']],
  lang(), v => prefs.set('lang', v));

// ---------- teleprompt: huge live captions that scroll gently to the newest words ----------

const prompt = (() => {
  const view = $('#promptView'), box = $('#promptText'), live = $('#liveBtn');
  let para = null, partialEl = null, lastFinal = 0, follow = true, userAt = 0, pos = 0;
  let speed = Number(prefs.get('promptSpeed', '3'));

  const newPara = () => { para = document.createElement('p'); box.append(para); };
  const save = () => prefs.set('prompt', [...box.children].map(p => p.textContent).slice(-200).join('\n'));
  for (const t of prefs.get('prompt', '').split('\n')) if (t.trim()) { newPara(); para.textContent = t; }
  lastFinal = 0;

  function partial(text) {
    if (!text) { if (partialEl) partialEl.remove(); partialEl = null; return; }
    if (!partialEl) {
      partialEl = document.createElement('span');
      partialEl.className = 'partial';
      (para || (newPara(), para)).append(partialEl);
    }
    partialEl.textContent = ' ' + text;
  }

  let lastWho = '';
  function final(text, who = '', translated = false) {
    partial('');
    if (!text) return;
    // a pause of 6 s, or another speaker, starts a new paragraph, so topics stay apart
    if (!para || para.classList.contains('me') || who !== lastWho || (Date.now() - lastFinal > 6000 && para.textContent)) {
      newPara();
      if (who) para.append(Object.assign(document.createElement('span'), { className: 'who', textContent: who }));
    }
    lastWho = who;
    // translating shows only the translation: the reader waits for one text, not two
    if (translating && !translated && !whisperEnglish()) addTranslation(text);
    else para.append((para.textContent ? ' ' : '') + text);
    lastFinal = Date.now();
    while (box.childElementCount > 400) box.firstElementChild.remove();
    save();
    suggestSoon();
  }

  // tap-to-ask: every ~20 s of new lesson, the cloud proposes 3 short questions (≈ $0.0001 each time)
  let sugAt = 0, sugBusy = false, sugTimer = null;
  const sugBox = $('#suggest');
  function suggestSoon() {
    clearTimeout(sugTimer);
    sugTimer = setTimeout(suggest, Math.max(1500, 20000 - (Date.now() - sugAt)));
  }
  const sugBoxPref = $('#suggestBox');
  sugBoxPref.checked = prefs.get('suggest', '1') === '1';
  sugBoxPref.onchange = () => {
    prefs.set('suggest', sugBoxPref.checked ? '1' : '0');
    if (!sugBoxPref.checked) { sugBox.replaceChildren(); sugBox.hidden = true; } else suggestSoon();
  };
  async function suggest() {
    if (!sugBoxPref.checked) return;
    const c = navigator.onLine && await cloud();
    const lesson = [...box.children].filter(p => !p.classList.contains('me')).map(p => p.textContent).join('\n').slice(-1500);
    if (!c || sugBusy || lesson.length < 40) return;
    sugBusy = true; sugAt = Date.now();
    try {
      const qs = await suggestQuestions(c.key, c.p, lesson);
      sugBox.replaceChildren(...qs.map(q => Object.assign(document.createElement('button'), {
        textContent: q, title: q,
        onclick: () => { msg.value = q; prefs.set('msg', q); msg.focus(); },
      })));
      sugBox.hidden = !qs.length;
    } catch { /* offline or no credit: keep the last suggestions */ }
    sugBusy = false;
  }

  // follow the newest line with an eased, slow scroll; speed 1..10 scales the easing
  const frame = () => {
    if (follow && view.offsetParent) {
      const end = view.scrollHeight - view.clientHeight;
      if (Math.abs(view.scrollTop - pos) > 2) pos = view.scrollTop; // layout changed under us
      const d = end - pos;
      if (d > 0.5) {
        pos += Math.min(d, Math.max(0.15 * speed, d * 0.0025 * speed));
        view.scrollTop = pos;
      }
    }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);

  // the pupil scrolling back pauses following; scrolling to the end (or "Ζωντανά") resumes it
  const touched = () => { userAt = Date.now(); };
  ['wheel', 'touchstart', 'touchmove', 'keydown', 'mousedown'].forEach(ev => view.addEventListener(ev, touched, { passive: true }));
  view.addEventListener('scroll', () => {
    if (Date.now() - userAt > 1500) return;
    follow = view.scrollHeight - view.clientHeight - view.scrollTop < 60;
    pos = view.scrollTop;
    live.hidden = follow;
  });
  live.onclick = () => { follow = true; pos = view.scrollTop; live.hidden = true; };

  // font size: sliders here and in Settings, +/- by pinch or ctrl+wheel
  const setSize = px => {
    px = Math.round(Math.max(24, Math.min(160, px)));
    view.style.fontSize = px + 'px';
    prefs.set('promptSize', String(px));
    document.querySelectorAll('.sizeRange').forEach(r => { r.value = px; });
    document.querySelectorAll('.sizeOut').forEach(o => { o.textContent = px + ' px'; });
  };
  const setSpeed = v => {
    speed = Number(v);
    prefs.set('promptSpeed', String(speed));
    document.querySelectorAll('.speedRange').forEach(r => { r.value = speed; });
    document.querySelectorAll('.speedOut').forEach(o => { o.textContent = speed <= 3 ? 'αργή' : speed <= 6 ? 'μέτρια' : 'γρήγορη'; });
  };
  const tick = () => { if (navigator.vibrate) navigator.vibrate(12); };
  document.querySelectorAll('.sizeRange').forEach(r => r.addEventListener('input', () => { tick(); setSize(Number(r.value)); }));
  document.querySelectorAll('.speedRange').forEach(r => r.addEventListener('input', () => { tick(); setSpeed(r.value); }));
  setSize(Number(prefs.get('promptSize', '64')));  // ~1.3 cm capitals: readable from about 1 m on a tablet
  setSpeed(prefs.get('promptSpeed', '3'));

  let pinch = null;
  const dist = t => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  view.addEventListener('touchstart', e => {
    if (e.touches.length === 2) pinch = { d: dist(e.touches), size: parseFloat(view.style.fontSize) };
  }, { passive: true });
  view.addEventListener('touchmove', e => {
    if (!pinch || e.touches.length !== 2) return;
    e.preventDefault();
    setSize(pinch.size * dist(e.touches) / pinch.d);
  }, { passive: false });
  view.addEventListener('touchend', () => { pinch = null; });
  view.addEventListener('wheel', e => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    setSize(parseFloat(view.style.fontSize) - Math.sign(e.deltaY) * 4);
  }, { passive: false });

  const setContrast = c => { view.classList.toggle('dark', c === 'dark'); view.classList.toggle('light', c !== 'dark'); prefs.set('promptContrast', c); };
  setContrast(prefs.get('promptContrast', 'dark'));
  $('#contrastBtn').onclick = () => setContrast(view.classList.contains('dark') ? 'light' : 'dark');

  const setFull = on => {
    document.body.classList.toggle('immersive', on);
    if (host.fullscreen) host.fullscreen(on);
    else if (on && document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => {});
    else if (!on && document.fullscreenElement) document.exitFullscreen().catch(() => {});
  };
  $('#fullBtn').onclick = () => setFull(!document.body.classList.contains('immersive'));
  document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement && !host.fullscreen) document.body.classList.remove('immersive'); });

  $('#promptClear').onclick = () => { box.replaceChildren(); para = partialEl = null; save(); $('#suggest').replaceChildren(); $('#suggest').hidden = true; };

  // translation under each phrase (cloud): for pupils who do not read Greek
  let translating = false;
  const LANGS = ['English', 'Greek', 'Albanian', 'Arabic', 'Ukrainian', 'Russian', 'Georgian', 'Persian', 'Urdu', 'Bulgarian', 'Romanian', 'French'];
  const trLang = $('#trLang');
  trLang.replaceChildren(...LANGS.map(l => new Option(l, l)));
  trLang.value = prefs.get('trLang', 'English');
  trLang.onchange = () => prefs.set('trLang', trLang.value);
  const trBtn = $('#translateBtn');
  // whisper's own translate task: into English only, no internet; anything else goes to the cloud
  const offlineNow = async () => whisperModels.length > 0 && (provider() === 'local' || !await apiKey() || !navigator.onLine);
  // offline: the Firefox models translate whisper's Greek text (better than whisper's own English); whisper's is the fallback
  const offlineRec = () => !!(rec.opts && rec.opts.offline);
  const whisperEnglish = () => translating && trLang.value === 'English' && offlineRec() && !localMT;
  const localTarget = () => localMT && MT_CODE[trLang.value];
  trBtn.onclick = async () => {
    if (!translating && !localTarget() && !(await offlineNow() && trLang.value === 'English') && !(navigator.onLine && await cloud()))
      return toast(trLang.value === 'English' ? 'Η μετάφραση θέλει internet και κλειδί (Mistral ή OpenAI) ή τοπικό whisper.'
        : `Μετάφραση σε ${trLang.value} γίνεται μόνο με internet και κλειδί (Mistral ή OpenAI)· χωρίς internet μόνο στα αγγλικά.`);
    translating = !translating;
    trBtn.setAttribute('aria-pressed', translating);
    trBtn.textContent = translating ? 'Μετάφραση: ' + trLang.value : 'Μετάφραση';
  };
  async function addTranslation(text) {
    const span = Object.assign(document.createElement('span'), { textContent: (para.textContent ? ' ' : '') + '…' });
    para.append(span);
    try {
      let out;
      // free and instant on this device when it can (Greek ↔ English); the cloud for other languages
      const c0 = localTarget() && (offlineRec() || !navigator.onLine || !await cloud());
      if (c0) {
        out = await local.translate(text, lang() === 'en' ? 'en' : 'el', MT_CODE[trLang.value]);
      } else {
        const c = await cloud();
        if (!c) throw new Error('χωρίς κλειδί');
        out = await translate(c.key, c.p, text, trLang.value);
      }
      span.textContent = (span.textContent.startsWith(' ') ? ' ' : '') + out;
      save();
    } catch (e) { span.textContent = '(' + e.message + ')'; }
  }

  // the pupil's answer: written here, read aloud by the tablet, kept in the captions in another colour
  const msg = $('#msgText');
  msg.value = prefs.get('msg', '');
  msg.addEventListener('input', () => prefs.set('msg', msg.value));
  const say = () => {
    const text = msg.value.trim();
    if (!text) return msg.focus();
    newPara();
    para.className = 'me';
    para.textContent = '✍ ' + text;
    save();
    msg.value = '';
    prefs.set('msg', '');
    // Android captions mute media and would caption the voice: pause them while it speaks
    const resume = rec.local && rec.mode === 'prompt';
    if (resume) host.listen(false);
    speech.start(text, rate()); // its first step stops any earlier speech, so set the hook after it
    if (resume) speech.done = () => { if (rec.local && rec.mode === 'prompt') host.listen(true); };
  };
  $('#msgSpeak').onclick = say;
  msg.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); say(); } });

  const page = $('#prompt');
  page.classList.toggle('swapped', prefs.get('promptSwap', '0') === '1');
  $('#swapBtn').onclick = () => prefs.set('promptSwap', page.classList.toggle('swapped') ? '1' : '0');

  // the target language when the cloud does the translating, else ''
  const cloudTarget = () => translating && !whisperEnglish() ? trLang.value : '';
  return { partial, final, whisperEnglish, cloudTarget };
})();

// ---------- settings: which recognizer (Android only) ----------

if (host.listen) {
  window.onRecogInfo = i => {
    const dl = $('#greekDownload');
    dl.hidden = i.greek !== 'downloadable';
    $('#recogInfo').textContent =
      !i.service && !i.onDevice ? 'Δεν βρέθηκε υπηρεσία αναγνώρισης ομιλίας. Εγκατάστησε ή ενημέρωσε την εφαρμογή Google από το Play Store.'
      : i.greek === 'installed' ? '✓ Τα ελληνικά είναι κατεβασμένα στη συσκευή: οι υπότιτλοι δουλεύουν και χωρίς internet.'
      : i.greek === 'pending' ? 'Τα ελληνικά κατεβαίνουν… (θέλει λίγα λεπτά σε Wi-Fi)'
      : i.greek === 'downloadable' ? 'Τα ελληνικά δεν είναι ακόμα κατεβασμένα για χρήση χωρίς internet. Πάτησε το κουμπί (μία φορά, σε Wi-Fi).'
      : i.greek === 'none' ? 'Η αναγνώριση χωρίς internet αυτής της συσκευής δεν έχει ελληνικά· θα χρησιμοποιηθεί η υπηρεσία Google με internet.'
      : 'Η αναγνώριση γίνεται από την υπηρεσία Google της συσκευής. Για χρήση χωρίς internet κατέβασε τα ελληνικά: εφαρμογή Google → Ρυθμίσεις → Φωνή → Αναγνώριση ομιλίας εκτός σύνδεσης → Ελληνικά.';
  };
  $('#greekDownload').onclick = () => {
    host.downloadGreek();
    toast('Ξεκίνησε η λήψη των ελληνικών.');
    setTimeout(() => host.checkRecognizer(), 8000);
  };
  host.checkRecognizer();
}

// ---------- start ----------

if (host.models) host.models.base().then(b => { if (b) { local.base = b; localMT = true; } });
if (host.whisperReady) host.whisperReady().then(m => {
  whisperModels = m;
  $('#recogInfo').textContent = m.length ? '' : 'Δεν βρέθηκε το whisper.cpp ή μοντέλο του σε αυτόν τον υπολογιστή· κατέβασέ τα από τις Ρυθμίσεις → Τοπικά μοντέλα.';
  paintEngine();
});

paintRec();
paintEngine();
checkMic();
apiKey().then(k => show(MINI ? 'dictate' : k || useLocal() ? prefs.get('page', 'dictate') : 'settings'));
