// VOICE desktop shell. The UI is ../web (copied into ./web for packaged builds);
// this process only keeps the API key safe, reads aloud with Piper, captures system audio and saves files.
const { app, BrowserWindow, ipcMain, safeStorage, session, desktopCapturer, dialog, powerSaveBlocker, nativeTheme, clipboard, screen } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawn, execFileSync } = require('child_process');

// in the repo use ../web directly (a stale ./web copy from `npm run copy-web` must not win); packaged builds only have ./web
const WEB = fs.existsSync(path.join(__dirname, '..', 'web', 'app.js')) ? path.join(__dirname, '..', 'web') : path.join(__dirname, 'web');
const keyFile = () => path.join(app.getPath('userData'), 'mistral.key');

// ---- API key: encrypted with the OS keyring (libsecret/KWallet on Linux, DPAPI on Windows) ----
ipcMain.handle('key:get', () => {
  try { return safeStorage.decryptString(fs.readFileSync(keyFile())); } catch { return ''; }
});
ipcMain.handle('key:set', (_e, key) => {
  if (!safeStorage.isEncryptionAvailable()) throw new Error('δεν υπάρχει ασφαλής αποθήκη κλειδιών στο σύστημα');
  fs.writeFileSync(keyFile(), safeStorage.encryptString(String(key).trim()), { mode: 0o600 });
});

// ---- read aloud: Piper with the repo's Greek voice when present; otherwise the page falls back to speechSynthesis ----
const ROOT = path.join(__dirname, '..');
const exe = process.platform === 'win32' ? 'piper.exe' : 'piper';
const PIPER = process.env.VOICE_PIPER || [path.join(ROOT, 'piper_tts', 'piper', exe), path.join(process.resourcesPath || '', 'piper', exe)].find(p => fs.existsSync(p));
const VOICE = process.env.VOICE_PIPER_MODEL || ['medium', 'low']
  .flatMap(q => [path.join(ROOT, 'voices', `el_GR-rapunzelina-${q}.onnx`), path.join(process.resourcesPath || '', 'voices', `el_GR-rapunzelina-${q}.onnx`)])
  .find(p => fs.existsSync(p));
let synthN = 0;
ipcMain.handle('tts:piper', () => !!(PIPER && VOICE));
ipcMain.handle('tts:synth', (_e, text, rate) => new Promise((resolve, reject) => {
  const out = path.join(os.tmpdir(), `voice-tts-${process.pid}-${synthN++}.wav`);
  const p = spawn(PIPER, ['-m', VOICE, '--length_scale', String(1 / (rate || 1)), '--sentence_silence', '0.3', '--output_file', out]);
  p.on('error', reject);
  p.on('close', code => {
    try {
      if (code) throw new Error('piper exited ' + code);
      const buf = fs.readFileSync(out);
      fs.unlinkSync(out);
      resolve(buf);
    } catch (err) { reject(err); }
  });
  p.stdin.end(text);
}));

// ---- offline speech-to-text: the repo's whisper.cpp with large-v3-turbo (medium, then small, if it is missing) ----
// build-vulkan (the AMD/Intel GPU through Vulkan) first: the encoder runs ~2.7× faster there
// packaged: the portable build (tools/build-whisper-portable.sh) in resources/whisper
const WBIN = app.isPackaged ? [path.join(process.resourcesPath, 'whisper')]
  : ['build-vulkan', 'build'].map(b => path.join(ROOT, 'whisper.cpp', b, 'bin')).concat(path.join(__dirname, 'whisper-bin'));
// Windows: whisper.cpp's own release build (CPU), see tools/fetch-whisper-windows.sh
const EXE = process.platform === 'win32' ? '.exe' : '';
const WHISPER = process.env.VOICE_WHISPER || WBIN.map(d => path.join(d, 'whisper-cli' + EXE)).find(p => fs.existsSync(p));
const WSERVER = WBIN.map(d => path.join(d, 'whisper-server' + EXE)).find(p => fs.existsSync(p));
// the model files the download manager below fetches
// q8_0 first: the same text as the full model, ~20% faster on this CPU (whisper-quantize makes it)
const WFILES = { turbo: ['ggml-large-v3-turbo-q5_0.bin'], small: ['ggml-small-q8_0.bin', 'ggml-small.bin'],
  medium: ['ggml-medium-q8_0.bin', 'ggml-medium.bin'], large: ['ggml-large-v3-q5_0.bin'] };
const wmodel = key => (WFILES[key] || []).map(modelFile).find(Boolean);
// turbo was never taught whisper's translate task (it writes the Greek again), so English output stays with medium
const pickModel = (model, translate) => process.env.WHISPER_MODEL
  || (translate && model === 'turbo' && (wmodel('medium') || wmodel('small'))) || wmodel(model) || ['turbo', 'medium', 'small'].map(wmodel).find(Boolean);
const THREADS = Math.max(4, Math.min(os.cpus().length - 2, 12));
// installed model keys, best first; [] when whisper.cpp itself is missing
ipcMain.handle('stt:ready', () => WHISPER ? ['turbo', 'medium', 'small', 'large'].filter(wmodel) : []);
// drop non-speech markers like [BLANK_AUDIO] or (μουσική), as clean_transcript does
const cleanText = out => {
  const text = out.replace(/\[[^\]]*\]|\([^)]*\)|\*[^*]*\*/g, ' ').replace(/\s+/g, ' ').trim();
  return /\p{L}|\d/u.test(text) ? text : '';
};
// the encoder always runs on a 30 s window; shrinking it to the clip (never under ~15 s, shorter invents words) is faster.
// Multiples of 256 only: whisper.cpp pads the window to one, and before its fix of 29 Oct 2026 (#4127, in none of the
// builds we ship) attention also read that padding (FLEURS Greek, turbo: 28% → 15% wrong words; medium 26% → 22%)
const audioCtx = bytes => {
  const n = Math.round((bytes - 44) / 32000 * 50), c = Math.ceil(Math.max(768, n + 100) / 256) * 256; // 50 frames a second
  return c <= 1280 ? c : n <= 1280 ? 1280 : 1500; // a 25 s chunk fits 1280 (25.6 s); only longer audio takes the full 1500
};

// whisper-server keeps the model loaded: no ~1 s load per phrase, so phrases come back in ~1–1.5 s on the GPU
let wsrv = null; // { model, port, ready: Promise, proc }
// whisper takes the first GPU Vulkan lists, on a laptop often the built-in one beside an NVIDIA/AMD card:
// the server's log names every GPU (uma: 1 = built-in), so the first start picks the dedicated one and restarts once
let gpuDev = null;
function server(m) {
  if (wsrv && wsrv.model === m && !wsrv.exited) return wsrv;
  if (wsrv) wsrv.proc.kill();
  const me = wsrv = { model: m, port: 18790 + Math.floor(Math.random() * 1000), proc: null, exited: false, gpu: false };
  const start = () => {
    const args = [WSERVER, '-m', m, '-t', String(THREADS), '--host', '127.0.0.1', '--port', String(me.port),
      ...(gpuDev ? ['-dev', String(gpuDev)] : [])];
    // stderr read, not left to fill its pipe (a full pipe stalls the server); it also says whether a GPU took the model
    const opts = { stdio: ['ignore', 'ignore', 'pipe'] };
    const proc = me.proc = process.platform === 'win32' ? spawn(args[0], args.slice(1), opts) : spawn('nice', ['-n', '10', ...args], opts);
    proc.on('error', () => {});
    let log = '';
    proc.stderr.on('data', d => {
      if (/using (?!CPU)\S+ backend/.test(d)) me.gpu = true;
      if (gpuDev !== null) return;
      log += d;
      if (!/whisper_backend_init_gpu: (using|no GPU)/.test(log)) return;
      const gpus = [...log.matchAll(/ggml_vulkan: (\d+) = .*\| uma: (\d)/g)];
      gpuDev = Number((gpus.find(g => g[2] === '0') || [0, 0])[1]);
      if (gpuDev) { proc.removeAllListeners('exit'); proc.kill(); start(); } // same port: the ready loop below keeps waiting
    });
    proc.on('exit', () => { me.exited = true; });
  };
  start();
  me.ready = (async () => {
    for (let i = 0; i < 160 && !me.exited; i++) {
      try { await fetch(`http://127.0.0.1:${me.port}/`); return; } catch { await new Promise(r => setTimeout(r, 250)); }
    }
    throw new Error('whisper-server did not start');
  })();
  return me;
}
app.on('will-quit', () => { if (wsrv) wsrv.proc.kill(); });

const infer = (s, wavBytes, fields) => {
  const f = new FormData();
  f.append('file', new Blob([wavBytes], { type: 'audio/wav' }), 'a.wav');
  for (const [k, v] of Object.entries({ ...fields, temperature: 0, response_format: 'json' })) f.append(k, String(v));
  return fetch(`http://127.0.0.1:${s.port}/inference`, { method: 'POST', body: f });
};
let sttWaiting = 0; // phrases on their way to the server: warm-up never queues ahead of one

// translate: whisper's own task, speech in any language → English text only
ipcMain.handle('stt:run', async (_e, wavBytes, { translate = false, lang = 'el', model = 'medium', terms = '' } = {}) => {
  const m = pickModel(model, translate);
  if (WSERVER) {
    sttWaiting++;
    try {
      const s = server(m);
      await s.ready;
      // no_timestamps as -nt below: with timestamps the server cut its text every 60 characters, mid-word ("ουραν ό"),
      // and cost accuracy (FLEURS Greek, medium: 33% → 26% wrong words)
      // the words to spell right as whisper's prompt (Settings: terms, today's lesson); cut to stay well inside its 224 tokens
      const prompt = terms && !translate ? { prompt: terms.slice(0, 300) } : {};
      const r = await infer(s, wavBytes, { language: lang, translate, no_timestamps: true, audio_ctx: audioCtx(wavBytes.byteLength), ...prompt });
      if (r.ok) return cleanText((await r.json()).text || '');
    } catch { /* fall back to one whisper-cli run below */ } finally { sttWaiting--; }
  }
  return runCli(m, wavBytes, translate, lang);
});

// this AMD iGPU (Renoir) boosts its clock only after ~4 s of steady work, so a lone phrase ran at a third of it
// (2.4 s instead of 1.4 s): while someone speaks, the page sends the last second of their voice to keep the GPU busy.
// Language detection only: the encoder (the GPU's work) and one decoder step, ~1 s every time. Transcribing the
// snippet instead sometimes looped on the same words for 4–5 s, and the next phrase waited behind it.
ipcMain.handle('stt:warm', async (_e, wavBytes, { model = 'medium', translate = false } = {}) => {
  const m = pickModel(model, translate);
  if (!WSERVER || !m || sttWaiting) return;
  const s = server(m); // the first word of a lesson also starts the server and loads the model
  try {
    await s.ready;
    if (s.gpu && !sttWaiting) await (await infer(s, wavBytes, { detect_language: true })).text();
  } catch { /* warm-up only */ }
});

const runCli = (m, wavBytes, translate, lang) => new Promise((resolve, reject) => {
  const f = path.join(os.tmpdir(), `voice-stt-${process.pid}-${synthN++}.wav`);
  fs.writeFileSync(f, Buffer.from(wavBytes));
  const ctx = audioCtx(wavBytes.byteLength);
  const args = ['-n', '10', WHISPER, '-m', m, '-f', f, '-l', lang, '-t', String(THREADS), '-ac', String(ctx), '-nt', '-np',
    ...(gpuDev ? ['-dev', String(gpuDev)] : [])];
  if (translate) args.push('-tr');
  const p = spawn(process.platform === 'win32' ? WHISPER : 'nice', process.platform === 'win32' ? args.slice(2) : args);
  let out = '';
  p.stdout.on('data', d => { out += d; });
  p.on('error', reject);
  p.on('close', code => {
    fs.rm(f, () => {});
    if (code) return reject(new Error('whisper exited ' + code));
    resolve(cleanText(out));
  });
});

// ---- offline models: downloaded into userData/models (the download manager below); a source checkout's
// ../models and the old GTK app's ~/.local/share/voice count too, so nothing is fetched twice ----
const { MODEL_URL, MODEL_PACKS } = require(path.join(WEB, 'voxtral.js'));
const { fetchResumable } = require('./download.js');
const MODELS_HOME = path.join(app.getPath('userData'), 'models');
const MODEL_DIRS = [MODELS_HOME, path.join(process.resourcesPath || '', 'models'), path.join(ROOT, 'models'),
  path.join(os.homedir(), '.local', 'share', 'voice', 'models')];
const modelFile = rel => MODEL_DIRS.map(d => path.join(d, rel)).find(p => fs.existsSync(p));
const packReady = pack => pack.files.every(f => { const p = modelFile(f.path); return p && fs.statSync(p).size === f.size; });

ipcMain.handle('models:status', () => MODEL_PACKS.map(p => ({ id: p.id, ready: packReady(p) })));
// translation's files ship in the package (resources/models) or sit in ../models in a checkout
ipcMain.handle('models:base', () => {
  const dir = MODEL_DIRS.find(d => fs.existsSync(path.join(d, 'bergamot', 'elen')));
  return dir ? require('url').pathToFileURL(dir).href + '/' : '';
});

const downloads = new Map(); // pack id → AbortController
ipcMain.handle('models:download', async (e, id) => {
  const pack = MODEL_PACKS.find(p => p.id === id);
  if (!pack || downloads.has(id)) return;
  const ctl = new AbortController();
  downloads.set(id, ctl);
  const total = pack.files.reduce((n, f) => n + f.size, 0);
  let done = 0;
  const report = state => { if (!e.sender.isDestroyed()) e.sender.send('models:progress', { id, done, total, state }); };
  try {
    for (const f of pack.files) {
      const dest = path.join(MODELS_HOME, f.path);
      if (packReady({ files: [f] })) { done += f.size; continue; }
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      const urls = [MODEL_URL + f.path.replaceAll('/', '__'), f.mirror].filter(Boolean);
      for (let i = 0; ; i++) {
        try { await fetchResumable(urls[i], dest, f, ctl.signal, n => { report('down'); done += n; }); break; }
        catch (err) { if (ctl.signal.aborted || i + 1 >= urls.length || !/HTTP 404/.test(err.message)) throw err; } // not on our repo: the mirror
      }
    }
    report('ready');
  } catch (err) {
    report(ctl.signal.aborted ? 'stopped' : 'error: ' + err.message);
  } finally {
    downloads.delete(id);
  }
});
ipcMain.handle('models:cancel', (_e, id) => downloads.get(id)?.abort());
ipcMain.handle('models:remove', (_e, id) => {
  const pack = MODEL_PACKS.find(p => p.id === id);
  for (const f of pack ? pack.files : []) fs.rmSync(path.join(MODELS_HOME, f.path), { force: true });
  if (id === 'voice') mmsSession = null;
});

// ---- Greek voice: Meta MMS (VITS → ONNX), native onnxruntime, ~0.4 s a sentence ----
let mmsSession = null;
ipcMain.handle('mms:synth', async (_e, text) => {
  if (!mmsSession) mmsSession = (async () => {
    const ort = require('onnxruntime-node');
    const { mmsIds } = require(path.join(WEB, 'local.js'));
    const tok = JSON.parse(fs.readFileSync(modelFile('mms-tts-ell/tokens.json'), 'utf8'));
    const session = await ort.InferenceSession.create(modelFile('mms-tts-ell/model.onnx'), { intraOpNumThreads: Math.max(2, Math.min(os.cpus().length >> 1, 8)) });
    return { ort, mmsIds, tok, session };
  })().catch(err => { mmsSession = null; throw err; });
  const { ort, mmsIds, tok, session } = await mmsSession;
  const ids = mmsIds(text, tok.vocab);
  const out = await session.run({ input_ids: new ort.Tensor('int64', BigInt64Array.from(ids, BigInt), [1, ids.length]) });
  return wav16(out.waveform.data, tok.sr);
});
function wav16(f, sr) {
  const b = Buffer.alloc(44 + f.length * 2);
  b.write('RIFF', 0); b.writeUInt32LE(36 + f.length * 2, 4); b.write('WAVEfmt ', 8); b.writeUInt32LE(16, 16);
  b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(sr, 24); b.writeUInt32LE(sr * 2, 28);
  b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write('data', 36); b.writeUInt32LE(f.length * 2, 40);
  for (let i = 0; i < f.length; i++) b.writeInt16LE(Math.round(Math.max(-1, Math.min(1, f[i])) * 32767), 44 + i * 2);
  return b;
}

// ---- system audio on Linux: parec on the default sink's monitor, 16 kHz mono s16le, streamed to the page ----
let parec = null;
ipcMain.handle('sys:start', e => {
  const sink = execFileSync('pactl', ['get-default-sink']).toString().trim();
  parec = spawn('parec', ['-d', sink + '.monitor', '--format=s16le', '--rate=16000', '--channels=1', '--latency-msec=100']);
  let odd = null; // keep chunks on whole samples
  parec.stdout.on('data', d => {
    if (odd) { d = Buffer.concat([odd, d]); odd = null; }
    if (d.length % 2) { odd = d.subarray(d.length - 1); d = d.subarray(0, d.length - 1); }
    if (!e.sender.isDestroyed()) e.sender.send('sys:pcm', d);
  });
  return new Promise((resolve, reject) => {
    parec.once('error', reject);
    parec.once('spawn', resolve);
  });
});
ipcMain.handle('sys:stop', () => { if (parec) parec.kill(); parec = null; });

// ---- live captions: Mistral's realtime transcription; words arrive while the teacher speaks, not after each phrase.
// Here, not in the page, because a browser WebSocket cannot send the Authorization header.
const RT_MODEL = 'voxtral-mini-transcribe-realtime-3';
let rt = null;
ipcMain.handle('rt:start', (e, { key, lang = 'el', bias = [], provider = 'mistral', diarize = false } = {}) => new Promise((resolve, reject) => {
  if (rt) { const old = rt; rt = null; old.close(); }
  if (!key) return reject(new Error('χωρίς κλειδί'));
  if (provider === 'elevenlabs') return rtEleven(e, { key, lang, bias, diarize }, resolve, reject);
  const ws = rt = new WebSocket(`wss://api.mistral.ai/v1/audio/transcriptions/realtime?model=${RT_MODEL}`, { headers: { Authorization: 'Bearer ' + key } });
  const send = ev => { if (!e.sender.isDestroyed()) e.sender.send('rt:event', ev); };
  let ready = false;
  ws.onmessage = m => {
    const j = JSON.parse(m.data);
    if (j.type === 'session.created') {
      const session = { audio_format: { encoding: 'pcm_s16le', sample_rate: 16000 }, target_streaming_delay_ms: 480 };
      if (lang !== 'auto') session.language = lang;
      if (bias.length) session.context_bias = bias;
      ws.send(JSON.stringify({ type: 'session.update', session }));
    } else if (j.type === 'session.updated') { ready = true; resolve(RT_MODEL); }
    else if (j.type === 'transcription.text.delta') send({ type: 'delta', text: j.text });
    else if (j.type === 'transcription.done') send({ type: 'done', usage: j.usage });
    else if (j.type === 'error') { const msg = j.error?.message || 'σφάλμα'; if (ready) send({ type: 'error', text: msg }); else reject(new Error(msg)); }
  };
  ws.onerror = () => { if (!ready) reject(new Error('η ζωντανή μεταγραφή δεν συνδέθηκε')); };
  // 'closed' only when the server or the network ended it: a stop or a restart from the page sets rt aside first
  ws.onclose = () => { if (rt !== ws) return reject(new Error('stopped')); rt = null; if (ready) send({ type: 'closed' }); else reject(new Error('η ζωντανή μεταγραφή δεν συνδέθηκε')); };
}));

// ElevenLabs Scribe live: it closes a line itself on a pause (commit_strategy vad); with diarize a second message
// gives the line's words with who said them, so the line waits up to 1.5 s for it and goes out unlabelled without it
const EL_RT_MODEL = 'scribe_v3_live';
const elChunk = (audio, commit = false) => JSON.stringify({ message_type: 'input_audio_chunk', audio_base_64: audio, commit, sample_rate: 16000 });
function rtEleven(e, { key, lang, bias, diarize }, resolve, reject) {
  const q = new URLSearchParams({ model_id: EL_RT_MODEL, audio_format: 'pcm_16000', commit_strategy: 'vad' });
  if (lang !== 'auto') q.set('language_code', lang);
  if (diarize) q.set('diarize', 'true');
  for (const t of bias.filter(t => t.length <= 20).slice(0, 50)) q.append('keyterms', t); // live takes 50 terms of ≤ 20 characters
  const ws = rt = new WebSocket('wss://api.elevenlabs.io/v1/speech-to-text/realtime?' + q, { headers: { 'xi-api-key': key } });
  ws.chunk = elChunk;
  const send = ev => { if (!e.sender.isDestroyed()) e.sender.send('rt:event', ev); };
  let ready = false, held = null;
  const flush = () => { if (held) { clearTimeout(held.timer); send({ type: 'final', text: held.text }); held = null; } };
  ws.onmessage = m => {
    const j = JSON.parse(m.data), t = j.message_type;
    if (t === 'session_started') { ready = true; resolve(EL_RT_MODEL); }
    else if (t === 'partial_transcript') send({ type: 'partial', text: j.text || '' });
    else if (t === 'committed_transcript') { flush(); if (diarize) held = { text: j.text || '', timer: setTimeout(flush, 1500) }; else send({ type: 'final', text: j.text || '' }); }
    else if (t === 'diarized_transcript' && held) { clearTimeout(held.timer); send({ type: 'final', text: held.text, words: j.words || [] }); held = null; }
    else if (/error|exceeded|limited/.test(t)) { const msg = `ElevenLabs: ${j.error || j.message || t}`; if (ready) send({ type: 'error', text: msg }); else reject(new Error(msg)); }
  };
  ws.onerror = () => { if (!ready) reject(new Error('η ζωντανή μεταγραφή δεν συνδέθηκε')); };
  ws.onclose = () => { flush(); if (rt !== ws) return reject(new Error('stopped')); rt = null; if (ready) send({ type: 'closed' }); else reject(new Error('η ζωντανή μεταγραφή δεν συνδέθηκε')); };
}

ipcMain.on('rt:audio', (_e, buf) => {
  if (rt?.readyState !== WebSocket.OPEN) return;
  const audio = Buffer.from(buf).toString('base64');
  rt.send(rt.chunk ? rt.chunk(audio) : JSON.stringify({ type: 'input_audio.append', audio }));
});
ipcMain.handle('rt:stop', () => {
  const ws = rt;
  rt = null;
  if (ws?.readyState !== WebSocket.OPEN) return;
  ws.send(ws.chunk ? ws.chunk('', true) : JSON.stringify({ type: 'input_audio.end' }));
  setTimeout(() => ws.close(), 3000); // the last words arrive after the end
});

// ---- Wi-Fi mics: phones/tablets running VOICE POST 16 kHz mono s16le here; the page mixes them (AutoMixer) ----
const WIFI_PORT = 8790;
let wifi = null, wifiTo = null, wifiCode = '';
// the Wi-Fi/Ethernet addresses a phone can reach; Docker/VM bridges would only confuse the teacher
const wifiAddrs = () => Object.entries(os.networkInterfaces())
  .filter(([n]) => !/^(docker|br-|veth|virbr|vmnet|vboxnet|lxc|lxdbr|tailscale|zt|wg)/i.test(n))
  .sort(([a], [b]) => /^(wl|wi-?fi)/i.test(b) - /^(wl|wi-?fi)/i.test(a)) // Wi-Fi first: the phone tries them in order
  .flatMap(([, a]) => a).filter(a => a.family === 'IPv4' && !a.internal).map(a => a.address);
// the network the phones must join, shown above the QR code; '' when unknown (cable, no NetworkManager)
// async: the first nmcli call can take seconds while NetworkManager wakes up, and must not freeze the window
const output = (cmd, args, env) => new Promise(ok => require('child_process').execFile(cmd, args, { encoding: 'utf8', timeout: 5000, env }, (err, out) => ok(err ? '' : out)));
async function wifiName() {
  if (process.platform === 'win32') return (/^\s*SSID\s*:\s*(.+)$/m.exec(await output('netsh', ['wlan', 'show', 'interfaces'])) || [])[1]?.trim() || '';
  // LC_ALL=C: in a Greek session the column says «ναι», not yes
  const line = (await output('nmcli', ['-t', '-f', 'active,ssid', 'dev', 'wifi', 'list', '--rescan', 'no'], { ...process.env, LC_ALL: 'C' })).split('\n').find(l => l.startsWith('yes:'));
  return line ? line.slice(4).replace(/\\:/g, ':') : '';
}
const wifiInfo = async () => ({ addrs: wifiAddrs(), port: WIFI_PORT, ssid: await wifiName(), pc: os.hostname() });
ipcMain.handle('wifi:start', (e, code) => {
  wifiTo = e.sender;
  wifiCode = String(code);
  if (wifi) return wifiInfo();
  wifi = require('http').createServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    if (req.method !== 'POST' || u.pathname !== '/mic') return res.end('VOICE');
    // anyone on the school network can reach this port: only devices that know the code shown on screen get in
    if (req.headers['x-voice-code'] !== wifiCode) { res.statusCode = 403; return res.end(); }
    const parts = [];
    let size = 0;
    req.on('data', d => { size += d.length; if (size > 1 << 20) req.destroy(); else parts.push(d); });
    req.on('end', () => {
      let d = Buffer.concat(parts);
      if (d.length % 2) d = d.subarray(0, d.length - 1);
      const id = (u.searchParams.get('id') || '').slice(0, 40), name = (u.searchParams.get('name') || '').slice(0, 40);
      if (id && d.length && wifiTo && !wifiTo.isDestroyed()) wifiTo.send('wifi:pcm', id, name, d);
      res.end('OK');
    });
  });
  return new Promise((resolve, reject) => {
    wifi.once('error', err => { wifi = null; reject(err.code === 'EADDRINUSE' ? new Error(`η θύρα ${WIFI_PORT} είναι πιασμένη`) : err); });
    wifi.listen(WIFI_PORT, () => resolve(wifiInfo()));
  });
});

// ---- mini window: a small always-on-top bar that types dictation into whatever app has focus ----
let mainWin = null, mini = null, miniOpening = false;
ipcMain.handle('mini:open', async (_e, onTop = true) => {
  if (!mini && !miniOpening) {
    miniOpening = true; // a double click must not open two bars while KWin is being asked
    await kwinKeepAbove('VOICE mini', onTop).catch(() => {}); // before the window exists, so KWin sees it arrive
    miniOpening = false;
    const { workArea: a } = screen.getPrimaryDisplay();
    mini = new BrowserWindow({
      width: 320, height: 76, x: a.x + a.width - 340, y: a.y + a.height - 100,
      frame: false, resizable: false, alwaysOnTop: onTop, skipTaskbar: false, title: 'VOICE mini',
      // never take keyboard focus: the paste must land in the app the user is typing in
      focusable: false,
      backgroundColor: nativeTheme.shouldUseDarkColors ? '#121416' : '#eef0f1',
      webPreferences: { preload: path.join(__dirname, 'preload.js') },
    });
    if (onTop) mini.setAlwaysOnTop(true, 'floating');
    mini.on('page-title-updated', e => e.preventDefault()); // keepAbove below finds the bar by this title
    mini.loadFile(path.join(WEB, 'index.html'), { query: { mini: '1' } });
    mini.on('closed', () => { mini = null; if (mainWin && !mainWin.isDestroyed()) mainWin.show(); });
  }
  if (mainWin) mainWin.hide();
});
// KDE on Wayland ignores alwaysOnTop (Wayland has no such request), so ask KWin itself through its scripting API.
// The script waits for the window to appear (running it after load raced the mapping), then stops listening.
// Each load gets a new name: reloading under the same name returns the old, still-unloading script, which never runs.
// a spot { left | right (+ width), top | bottom, height } inside area A → a rectangle; also written into KWin scripts
const rect = (p, A) => {
  const width = p.width ?? A.width - (p.left || 0) - (p.right || 0);
  return { x: A.x + (p.left ?? A.width - p.right - width), y: A.y + (p.top ?? A.height - p.bottom - p.height), width, height: p.height };
};
const kwinOn = () => process.platform === 'linux' && !!process.env.WAYLAND_DISPLAY && /KDE/i.test(process.env.XDG_CURRENT_DESKTOP || '');
const kwinCall = (obj, method, ...args) => output('gdbus', ['call', '--session', '--dest', 'org.kde.KWin', '--object-path', obj, '--method', 'org.kde.kwin.' + method, ...args]);
/** Load and run one KWin script under a fresh name (a reused name returns the old, still-unloading script). */
async function kwinRun(code, prefix) {
  const js = path.join(app.getPath('userData'), prefix + '.js');
  fs.writeFileSync(js, code);
  const name = prefix + '-' + Date.now();
  const id = /\d+/.exec(await kwinCall('/Scripting', 'Scripting.loadScript', js, name))?.[0];
  if (id) await kwinCall('/Scripting/Script' + id, 'Script.run');
  return name;
}
let kwinScript = null;
async function kwinKeepAbove(title, on, place = {}) {
  if (!kwinOn()) return;
  if (kwinScript) await kwinCall('/Scripting', 'Scripting.unloadScript', kwinScript);
  kwinScript = null;
  if (!on) return;
  // title: one window's title, or several (the caption overlay has two)
  // place: { title: spot (see rect) } in the usable area (above the panel), since a Wayland window cannot position itself
  kwinScript = await kwinRun(`const T = ${JSON.stringify([].concat(title))}, P = ${JSON.stringify(place)}; let left = T.length;
function f(w) { if (!T.includes(w.caption) || w.keepAbove) return false; w.keepAbove = true; place(w); if (--left === 0) try { workspace.windowAdded.disconnect(added); } catch (e) {} return true; }
function added(w) { if (!f(w)) w.captionChanged.connect(() => f(w)); }
const rect = ${rect};
function place(w) { const p = P[w.caption]; if (p) w.frameGeometry = rect(p, workspace.clientArea(KWin.MaximizeArea, w)); }
workspace.windowList().forEach(f);
if (left > 0) workspace.windowAdded.connect(added);`, 'voice-keep-above');
}
ipcMain.handle('mini:close', () => { if (mini) mini.close(); });

// ---- caption overlay: the teleprompt's words along the bottom of the screen, over any app.
// Where the mouse can pass through a window (Windows, macOS, X11) it does; on Wayland it cannot (Electron's
// setIgnoreMouseEvents and setShape both still take the click, tested on KDE, Nov 2026), so there the words jump to the
// other edge of the screen as soon as the pointer comes onto them. The ✕ is a window of its own beside the words.
let cap = null, capX = null, capTop = false, capMoved = 0;
const CAP = 'VOICE captions', CAPX = 'VOICE captions close', clickThrough = !process.env.WAYLAND_DISPLAY;
const capPage = (name, body) => 'data:text/html;charset=utf-8,' + encodeURIComponent(`<!doctype html><title>${name}</title>${body}`);
const capHeight = () => Math.round(Math.min(260, screen.getPrimaryDisplay().workArea.height * 0.3));
function capSpots(h = capHeight()) {
  const edge = capTop ? { top: 0 } : { bottom: 0 }, xEdge = capTop ? { top: 8 } : { bottom: h - 56 };
  return { [CAP]: { left: 0, right: 64, height: h, ...edge }, [CAPX]: { right: 8, width: 48, height: 48, ...xEdge } };
}
let kwinMove = null;
async function capPlace() {
  const spots = capSpots();
  if (!kwinOn()) {
    const A = screen.getPrimaryDisplay().workArea;
    if (cap) cap.setBounds(rect(spots[CAP], A));
    if (capX) capX.setBounds(rect(spots[CAPX], A));
    return;
  }
  const old = kwinMove;
  kwinMove = await kwinRun(`const P = ${JSON.stringify(spots)}, rect = ${rect};
workspace.windowList().forEach(w => { const p = P[w.caption]; if (p) w.frameGeometry = rect(p, workspace.clientArea(KWin.MaximizeArea, w)); });`, 'voice-move');
  if (old) kwinCall('/Scripting', 'Scripting.unloadScript', old).catch(() => {});
}
ipcMain.handle('cap:open', async () => {
  if (cap) return;
  capTop = false;
  const A = screen.getPrimaryDisplay().workArea, spots = capSpots();
  await kwinKeepAbove([CAP, CAPX], true, spots).catch(() => {});
  const common = { frame: false, resizable: false, movable: false, alwaysOnTop: true, skipTaskbar: true, focusable: false, hasShadow: false,
    transparent: true, backgroundColor: '#00000000', webPreferences: { preload: path.join(__dirname, 'preload.js') } };
  cap = new BrowserWindow({ ...common, ...rect(spots[CAP], A), title: CAP });
  if (clickThrough) cap.setIgnoreMouseEvents(true);
  cap.loadFile(path.join(WEB, 'captions.html'));
  capX = new BrowserWindow({ ...common, ...rect(spots[CAPX], A), title: CAPX });
  capX.loadURL(capPage(CAPX, `<style>html,body{margin:0;background:transparent;overflow:hidden}
button{width:44px;height:44px;margin:2px;border:0;border-radius:50%;background:rgba(0,0,0,.65);color:#fff;font:700 22px sans-serif;cursor:pointer}
button:hover{background:#c62828}</style><button aria-label="Κλείσιμο υποτίτλων" onclick="voiceElectron.capClose()">✕</button>`));
  for (const w of [cap, capX]) {
    w.setAlwaysOnTop(true, 'screen-saver');
    w.on('page-title-updated', e => e.preventDefault()); // KWin finds them by title
    w.on('closed', capClose);
  }
  // the main window keeps recording behind the overlay: don't let Chromium slow its timers
  mainWin?.webContents.setBackgroundThrottling(false);
  mainWin?.minimize();
});
// the pointer came onto the words: move them to the other edge (not again for a moment, so they cannot bounce)
ipcMain.on('cap:dodge', () => {
  if (!cap || clickThrough || Date.now() - capMoved < 600) return;
  capMoved = Date.now();
  capTop = !capTop;
  capPlace().catch(() => {});
});
function capClose() {
  const a = cap, b = capX;
  cap = capX = null;
  for (const w of [a, b]) if (w && !w.isDestroyed()) w.close();
  if (!a && !b) return;
  kwinKeepAbove(null, false).catch(() => {});
  if (kwinMove) { kwinCall('/Scripting', 'Scripting.unloadScript', kwinMove).catch(() => {}); kwinMove = null; }
  if (mainWin && !mainWin.isDestroyed()) {
    mainWin.webContents.setBackgroundThrottling(true);
    mainWin.restore(); mainWin.show();
    mainWin.webContents.send('cap:closed');
  }
}
ipcMain.on('cap:close', capClose);
ipcMain.on('cap:text', (_e, t) => { if (cap && !cap.isDestroyed()) cap.webContents.send('cap:text', t); });

const has = cmd => { try { execFileSync('which', [cmd], { stdio: 'ignore' }); return true; } catch { return false; } };
const run = (cmd, args) => new Promise(ok => { const p = spawn(cmd, args, { stdio: 'ignore' }); p.on('error', () => ok(false)); p.on('close', c => ok(c === 0)); });
// Ctrl+V into the focused window, as platform_io.send_paste: ydotool (Wayland), xdotool (X11), SendKeys (Windows)
async function sendPaste() {
  if (process.platform === 'win32')
    return run('powershell', ['-NoProfile', '-Command', "(New-Object -ComObject WScript.Shell).SendKeys('^v')"]);
  const ydo = () => has('ydotool') && run('ydotool', ['key', '29:1', '47:1', '47:0', '29:0']);
  const xdo = () => has('xdotool') && run('xdotool', ['key', '--clearmodifiers', 'ctrl+v']);
  return process.env.WAYLAND_DISPLAY ? (await ydo()) || xdo() : (await xdo()) || ydo();
}
// No clipboard watching here, ever: polling wl-paste broke menus desktop-wide in the old GTK app.
ipcMain.handle('type', async (_e, text) => {
  clipboard.writeText(text);
  // a focused VOICE window would receive the paste and show the text twice
  if (BrowserWindow.getFocusedWindow()) return false;
  await new Promise(r => setTimeout(r, 80));
  return sendPaste();
});

// ---- misc ----
let blocker = null;
ipcMain.handle('awake', (_e, on) => {
  if (on && blocker === null) blocker = powerSaveBlocker.start('prevent-app-suspension');
  if (!on && blocker !== null) { powerSaveBlocker.stop(blocker); blocker = null; }
});
ipcMain.handle('save', async (e, text, name) => {
  const r = await dialog.showSaveDialog(BrowserWindow.fromWebContents(e.sender), {
    defaultPath: path.join(app.getPath('documents'), name),
    filters: [{ name: 'Markdown', extensions: ['md'] }, { name: 'Κείμενο', extensions: ['txt'] }],
  });
  if (r.canceled || !r.filePath) return false;
  fs.writeFileSync(r.filePath, text, 'utf8');
  return true;
});

// a page (the lesson outline: summary, list, map) → an A4 PDF, printed by a hidden window
ipcMain.handle('pdf', async (e, html, name) => {
  const r = await dialog.showSaveDialog(BrowserWindow.fromWebContents(e.sender), {
    defaultPath: path.join(app.getPath('documents'), name), filters: [{ name: 'PDF', extensions: ['pdf'] }],
  });
  if (r.canceled || !r.filePath) return false;
  const w = new BrowserWindow({ show: false, webPreferences: { javascript: false } });
  try {
    await w.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
    fs.writeFileSync(r.filePath, await w.webContents.printToPDF({ pageSize: 'A4', printBackground: true, preferCSSPageSize: true }));
  } finally { w.destroy(); }
  return true;
});

app.whenReady().then(() => {
  // Windows: system audio through getDisplayMedia, answered with Chromium's loopback (no picker shown)
  session.defaultSession.setDisplayMediaRequestHandler((_req, cb) => {
    desktopCapturer.getSources({ types: ['screen'] }).then(s => cb({ video: s[0], audio: 'loopback' })).catch(() => cb({}));
  });
  const win = mainWin = new BrowserWindow({
    width: 1100, height: 780, minWidth: 380, minHeight: 520,
    title: 'VOICE', icon: path.join(WEB, 'icon.png'), autoHideMenuBar: true,
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#121416' : '#eef0f1',
    webPreferences: { preload: path.join(__dirname, 'preload.js') },
  });
  win.loadFile(path.join(WEB, 'index.html'));
  if (process.env.VOICE_SHOT) {
    // screenshot for the README: VOICE_SHOT=out.png [VOICE_PAGE=meeting] electron .
    win.webContents.once('did-finish-load', async () => {
      if (process.env.VOICE_PAGE) await win.webContents.executeJavaScript(`show(${JSON.stringify(process.env.VOICE_PAGE)})`);
      setTimeout(async () => {
        fs.writeFileSync(process.env.VOICE_SHOT, (await win.webContents.capturePage()).toPNG());
        app.quit();
      }, 1200);
    });
  }
});
// one instance only: a second one would run its own recorder and paste everything twice
if (!app.requestSingleInstanceLock()) app.quit();
app.on('second-instance', () => { if (mini) mini.close(); if (mainWin) { mainWin.show(); mainWin.focus(); } });
app.on('window-all-closed', () => { if (parec) parec.kill(); app.quit(); });
