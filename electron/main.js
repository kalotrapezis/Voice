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
// Windows: whisper.cpp's own release build (CPU), see tools/fetch-whisper-windows.sh
const EXE = process.platform === 'win32' ? '.exe' : '';
const WHISPER = process.env.VOICE_WHISPER || WBIN.map(d => path.join(d, 'whisper-cli' + EXE)).find(p => fs.existsSync(p));
const WSERVER = WBIN.map(d => path.join(d, 'whisper-server' + EXE)).find(p => fs.existsSync(p));
// the model files the download manager below fetches
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
