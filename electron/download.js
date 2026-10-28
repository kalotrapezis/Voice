// Resumable, checksum-verified download for the offline models (main.js's download manager).
// Self-test: node download.js <url> <size> <sha256>
'use strict';
const fs = require('fs');
const crypto = require('crypto');

/** Download to dest.part, picking up where an earlier try stopped; sha256-check before it becomes dest. */
async function fetchResumable(url, dest, f, signal, onBytes) {
  const part = dest + '.part';
  let have = fs.existsSync(part) ? fs.statSync(part).size : 0;
  if (have > f.size) { fs.rmSync(part); have = 0; }
  const hash = crypto.createHash('sha256');
  if (have) { for await (const c of fs.createReadStream(part)) hash.update(c); onBytes(have); } // streamed: a part can be 800 MB
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

module.exports = { fetchResumable };

if (require.main === module) {
  // download, stop after ~40%, resume, verify; then a corrupt copy must be refused
  const [url, size, sha256] = process.argv.slice(2), f = { size: Number(size), sha256 };
  const dest = require('path').join(require('os').tmpdir(), 'voice-dl-test.bin');
  fs.rmSync(dest, { force: true }); fs.rmSync(dest + '.part', { force: true });
  (async () => {
    const ctl = new AbortController();
    let got = 0;
    await fetchResumable(url, dest, f, ctl.signal, n => { got += n; if (got > f.size * 0.4) ctl.abort(); }).catch(e => console.log('stopped:', e.name, e.message, '· part:', fs.existsSync(dest + '.part') ? fs.statSync(dest + '.part').size : 'none'));
    got = 0;
    await fetchResumable(url, dest, f, new AbortController().signal, n => { got += n; });
    console.log('resumed and verified:', fs.statSync(dest).size === f.size, 'progress counted', got, 'of', f.size);
    fs.rmSync(dest);
    await fetchResumable(url, dest, { ...f, sha256: '0'.repeat(64) }, new AbortController().signal, () => {})
      .then(() => console.log('BAD: a wrong checksum was accepted'), e => console.log('wrong checksum refused:', e.message, '· file kept:', fs.existsSync(dest)));
  })();
}
