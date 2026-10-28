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

// ---- offline speech-to-text: the repo's whisper.cpp with the medium model (small if medium is missing) ----
// build-vulkan (the AMD/Intel GPU through Vulkan) first: the encoder runs ~2.7× faster there
// packaged: the portable build (tools/build-whisper-portable.sh) in resources/whisper
const WBIN = app.isPackaged ? [path.join(process.resourcesPath, 'whisper')]
  : ['build-vulkan', 'build'].map(b => path.join(ROOT, 'whisper.cpp', b, 'bin')).concat(path.join(__dirname, 'whisper-bin'));
const WHISPER = process.env.VOICE_WHISPER || WBIN.map(d => path.join(d, 'whisper-cli')).find(p => fs.existsSync(p));
const WSERVER = WBIN.map(d => path.join(d, 'whisper-server')).find(p => fs.existsSync(p));
// the GTK app's model files (it downloads them); same folders it uses
// q8_0 first: the same text as the full model, ~20% faster on this CPU (whisper-quantize makes it)
const WFILES = { small: ['ggml-small-q8_0.bin', 'ggml-small.bin'], medium: ['ggml-medium-q8_0.bin', 'ggml-medium.bin'], large: ['ggml-large-v3-q5_0.bin'] };
const wmodel = key => (WFILES[key] || []).map(modelFile).find(Boolean);
const THREADS = Math.max(4, Math.min(os.cpus().length - 2, 12));
// installed model keys, best first; [] when whisper.cpp itself is missing
ipcMain.handle('stt:ready', () => WHISPER ? ['medium', 'small', 'large'].filter(wmodel) : []);
// drop non-speech markers like [BLANK_AUDIO] or (μουσική), as clean_transcript does
const cleanText = out => {
  const text = out.replace(/\[[^\]]*\]|\([^)]*\)|\*[^*]*\*/g, ' ').replace(/\s+/g, ' ').trim();
  return /\p{L}|\d/u.test(text) ? text : '';
};
// the encoder always runs on a 30 s window; shrinking it to the clip (never under ~15 s, shorter invents words) is faster
const audioCtx = bytes => Math.min(1500, Math.max(768, Math.round((bytes - 44) / 32000 * 50) + 100));

// whisper-server keeps the model loaded: no ~1 s load per phrase, so phrases come back in ~1–1.5 s on the GPU
let wsrv = null; // { model, port, ready: Promise, proc }
function server(m) {
  if (wsrv && wsrv.model === m && !wsrv.exited) return wsrv;
  if (wsrv) wsrv.proc.kill();
  const port = 18790 + Math.floor(Math.random() * 1000);
  const args = [WSERVER, '-m', m, '-t', String(THREADS), '--host', '127.0.0.1', '--port', String(port)];
  const proc = process.platform === 'win32' ? spawn(args[0], args.slice(1)) : spawn('nice', ['-n', '10', ...args]);
  proc.on('error', () => {});
  const ready = (async () => {
    for (let i = 0; i < 120 && proc.exitCode === null && proc.signalCode === null; i++) {
      try { await fetch(`http://127.0.0.1:${port}/`); return; } catch { await new Promise(r => setTimeout(r, 250)); }
    }
    throw new Error('whisper-server did not start');
  })();
  wsrv = { model: m, port, ready, proc, exited: false };
  proc.on('exit', () => { wsrv && wsrv.proc === proc && (wsrv.exited = true); });
  return wsrv;
}
app.on('will-quit', () => { if (wsrv) wsrv.proc.kill(); });

// translate: whisper's own task, speech in any language → English text only
ipcMain.handle('stt:run', async (_e, wavBytes, { translate = false, lang = 'el', model = 'medium' } = {}) => {
  const m = process.env.WHISPER_MODEL || wmodel(model) || wmodel('medium') || wmodel('small');
  if (WSERVER) {
    try {
      const s = server(m);
      await s.ready;
      const f = new FormData();
      f.append('file', new Blob([wavBytes], { type: 'audio/wav' }), 'a.wav');
      for (const [k, v] of Object.entries({ language: lang, translate, audio_ctx: audioCtx(wavBytes.byteLength), temperature: 0, response_format: 'json' }))
        f.append(k, String(v));
      const r = await fetch(`http://127.0.0.1:${s.port}/inference`, { method: 'POST', body: f });
      if (r.ok) return cleanText((await r.json()).text || '');
    } catch { /* fall back to one whisper-cli run below */ }
  }
  return runCli(m, wavBytes, translate, lang);
});

const runCli = (m, wavBytes, translate, lang) => new Promise((resolve, reject) => {
  const f = path.join(os.tmpdir(), `voice-stt-${process.pid}-${synthN++}.wav`);
  fs.writeFileSync(f, Buffer.from(wavBytes));
  const ctx = audioCtx(wavBytes.byteLength);
  const args = ['-n', '10', WHISPER, '-m', m, '-f', f, '-l', lang, '-t', String(THREADS), '-ac', String(ctx), '-nt', '-np'];
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
// ../models and the GTK app's folder count too, so nothing is fetched twice on a developer machine ----
const { MODEL_URL, MODEL_PACKS } = require(path.join(WEB, 'voxtral.js'));
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
      await fetchResumable(MODEL_URL + f.path.replaceAll('/', '__'), dest, f, ctl.signal, n => { report('down'); done += n; });
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

/** Download to dest.part, picking up where an earlier try stopped; sha256-check before it becomes dest. */
async function fetchResumable(url, dest, f, signal, onBytes) {
  const part = dest + '.part', crypto = require('crypto');
  let have = fs.existsSync(part) ? fs.statSync(part).size : 0;
  if (have > f.size) { fs.rmSync(part); have = 0; }
  const hash = crypto.createHash('sha256');
  if (have) { hash.update(fs.readFileSync(part)); onBytes(have); }
  if (have < f.size) {
    const r = await fetch(url, { signal, headers: have ? { Range: `bytes=${have}-` } : {} });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    if (have && r.status !== 206) { // the server ignored Range: start over
      fs.rmSync(part); onBytes(-have);
      return fetchResumable(url, dest, f, signal, onBytes);
    }
    const out = fs.createWriteStream(part, { flags: 'a' });
    try {
      for await (const chunk of r.body) {
        hash.update(chunk);
        if (!out.write(chunk)) await new Promise(ok => out.once('drain', ok));
        onBytes(chunk.length);
      }
    } finally {
      await new Promise(ok => out.end(ok));
    }
  }
  if (hash.digest('hex') !== f.sha256) { fs.rmSync(part, { force: true }); throw new Error('το αρχείο ήρθε χαλασμένο, ξαναδοκίμασε'); }
  fs.renameSync(part, dest);
}

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

// ---- mini window: a small always-on-top bar that types dictation into whatever app has focus ----
let mainWin = null, mini = null;
ipcMain.handle('mini:open', () => {
  if (!mini) {
    const { workArea: a } = screen.getPrimaryDisplay();
    mini = new BrowserWindow({
      width: 320, height: 76, x: a.x + a.width - 340, y: a.y + a.height - 100,
      frame: false, resizable: false, alwaysOnTop: true, skipTaskbar: false, title: 'VOICE mini',
      // never take keyboard focus: the paste must land in the app the user is typing in
      focusable: false,
      backgroundColor: nativeTheme.shouldUseDarkColors ? '#121416' : '#eef0f1',
      webPreferences: { preload: path.join(__dirname, 'preload.js') },
    });
    mini.setAlwaysOnTop(true, 'floating');
    mini.loadFile(path.join(WEB, 'index.html'), { query: { mini: '1' } });
    mini.on('closed', () => { mini = null; if (mainWin && !mainWin.isDestroyed()) mainWin.show(); });
  }
  if (mainWin) mainWin.hide();
});
ipcMain.handle('mini:close', () => { if (mini) mini.close(); });

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
// No clipboard watching here, ever: polling wl-paste broke menus desktop-wide in the GTK app.
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
