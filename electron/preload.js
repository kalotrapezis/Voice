// The only doorway from the page (../web/app.js) to the desktop: see main.js for each call.
const { contextBridge, ipcRenderer } = require('electron');

let onPcm = null;
ipcRenderer.on('sys:pcm', (_e, buf) => {
  if (onPcm) onPcm(new Int16Array(buf.buffer, buf.byteOffset, buf.byteLength / 2).slice());
});

let onWifi = null;
ipcRenderer.on('wifi:pcm', (_e, id, name, buf) => {
  if (onWifi) onWifi(id, name, new Int16Array(buf.buffer, buf.byteOffset, buf.byteLength / 2).slice());
});

let onRt = null;
ipcRenderer.on('rt:event', (_e, ev) => { if (onRt) onRt(ev); });

contextBridge.exposeInMainWorld('voiceElectron', {
  platform: process.platform,
  getKey: () => ipcRenderer.invoke('key:get'),
  setKey: k => ipcRenderer.invoke('key:set', k),
  piper: () => ipcRenderer.invoke('tts:piper'),
  whisperReady: () => ipcRenderer.invoke('stt:ready'),
  whisper: (wav, opts) => ipcRenderer.invoke('stt:run', wav, opts),
  whisperWarm: (wav, opts) => ipcRenderer.invoke('stt:warm', wav, opts),
  mms: text => ipcRenderer.invoke('mms:synth', text),
  models: {
    status: () => ipcRenderer.invoke('models:status'),
    base: () => ipcRenderer.invoke('models:base'),
    download: id => ipcRenderer.invoke('models:download', id),
    cancel: id => ipcRenderer.invoke('models:cancel', id),
    remove: id => ipcRenderer.invoke('models:remove', id),
    onProgress: cb => ipcRenderer.on('models:progress', (_e, p) => cb(p)),
  },
  synth: (text, rate) => ipcRenderer.invoke('tts:synth', text, rate),
  startSystem: cb => { onPcm = cb; return ipcRenderer.invoke('sys:start'); },
  stopSystem: () => { onPcm = null; return ipcRenderer.invoke('sys:stop'); },
  wifiStart: (code, cb) => { onWifi = cb; return ipcRenderer.invoke('wifi:start', code); },
  rtStart: (opts, cb) => { onRt = cb; return ipcRenderer.invoke('rt:start', opts); },
  rtAudio: pcm => ipcRenderer.send('rt:audio', new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength)),
  rtStop: () => ipcRenderer.invoke('rt:stop'),
  // caption overlay: the main page opens it and sends it text; the overlay listens; its ✕ closes it
  capOpen: onClosed => { ipcRenderer.removeAllListeners('cap:closed'); ipcRenderer.on('cap:closed', () => onClosed()); return ipcRenderer.invoke('cap:open'); },
  capText: t => ipcRenderer.send('cap:text', t),
  capClose: () => ipcRenderer.send('cap:close'),
  capDodge: () => ipcRenderer.send('cap:dodge'),
  onCapText: cb => ipcRenderer.on('cap:text', (_e, t) => cb(t)),
  openMini: onTop => ipcRenderer.invoke('mini:open', onTop),
  closeMini: () => ipcRenderer.invoke('mini:close'),
  type: text => ipcRenderer.invoke('type', text),
  keepAwake: on => ipcRenderer.invoke('awake', on),
  save: (text, name) => ipcRenderer.invoke('save', text, name),
  pdf: (html, name) => ipcRenderer.invoke('pdf', html, name),
});
