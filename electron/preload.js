// The only doorway from the page (../web/app.js) to the desktop: see main.js for each call.
const { contextBridge, ipcRenderer } = require('electron');

let onPcm = null;
ipcRenderer.on('sys:pcm', (_e, buf) => {
  if (onPcm) onPcm(new Int16Array(buf.buffer, buf.byteOffset, buf.byteLength / 2).slice());
});

contextBridge.exposeInMainWorld('voiceElectron', {
  platform: process.platform,
  getKey: () => ipcRenderer.invoke('key:get'),
  setKey: k => ipcRenderer.invoke('key:set', k),
  piper: () => ipcRenderer.invoke('tts:piper'),
  whisperReady: () => ipcRenderer.invoke('stt:ready'),
  whisper: (wav, opts) => ipcRenderer.invoke('stt:run', wav, opts),
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
  openMini: () => ipcRenderer.invoke('mini:open'),
  closeMini: () => ipcRenderer.invoke('mini:close'),
  type: text => ipcRenderer.invoke('type', text),
  keepAwake: on => ipcRenderer.invoke('awake', on),
  save: (text, name) => ipcRenderer.invoke('save', text, name),
});
