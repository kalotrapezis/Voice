// Self-check of the shared core: node web/test.js
const assert = require('assert');
const v = require('./voxtral.js');

// speaker lines: merge runs, number speakers by first appearance, shift by chunk offset
assert.deepStrictEqual(v.speakerLines({ segments: [
  { text: ' Καλημέρα.', start: 3.2, speaker_id: 'speaker_b' },
  { text: 'Ξεκινάμε.', start: 5, speaker_id: 'speaker_b' },
  { text: 'Ναι.', start: 71.9, speaker_id: 'speaker_a' },
  { text: 'Τέλος.', start: 80, speaker_id: 'speaker_b' }] }, 10), [
  { t: 13.2, who: 'Ομιλητής 1', text: 'Καλημέρα. Ξεκινάμε.' },
  { t: 81.9, who: 'Ομιλητής 2', text: 'Ναι.' },
  { t: 90, who: 'Ομιλητής 1', text: 'Τέλος.' }]);
assert.deepStrictEqual(v.speakerLines({ text: ' σκέτο ' }), [{ t: 0, who: '', text: 'σκέτο' }]);

assert.strictEqual(v.stamp(3725.9), '01:02:05');

// the cut lands on the quiet frame inside the last 5 s
const pcm = new Int16Array(v.CHUNK).fill(3000);
const quiet = v.CHUNK - 2 * v.SR;
pcm.fill(0, quiet, quiet + v.FRAME);
assert.strictEqual(v.quietestCut(pcm), quiet);
assert.ok(v.isSilent(new Int16Array(v.SR)));
assert.ok(!v.isSilent(pcm));

// WAV header
const w = new DataView(v.wav(new Int16Array(10)));
assert.strictEqual(w.byteLength, 64);
assert.strictEqual(w.getUint32(24, true), 16000);

// VAD: 0.5 s noise floor, 1 s loud, 1 s quiet → one phrase starting near 0.5 s
const phrases = [];
const vad = new v.Vad((p, start) => phrases.push([p.length, start]));
const frame = level => new Int16Array(v.FRAME).map((_, i) => (i % 2 ? level : -level));
for (let i = 0; i < 17; i++) vad.push(frame(50));
for (let i = 0; i < 33; i++) vad.push(frame(5000));
for (let i = 0; i < 33; i++) vad.push(frame(50));
assert.strictEqual(phrases.length, 1);
assert.ok(Math.abs(phrases[0][1] / v.SR - 0.5) < 0.25, 'phrase start ' + phrases[0][1]);

assert.deepStrictEqual(v.ttsChunks('Ένα. Δύο; Τρία'), ['Ένα.', 'Δύο;', 'Τρία']);
assert.deepStrictEqual(v.terms(' a, ,b '), ['a', 'b']);
// offline voice: number words and tokens match tts_server.py's (the Python original)
const { sayNumber, mmsIds } = require('./local.js');
assert.strictEqual(sayNumber(2026), 'δύο χιλιάδες είκοσι έξι');
assert.strictEqual(sayNumber(115), 'εκατόν δεκαπέντε');
assert.strictEqual(sayNumber(100), 'εκατό');
assert.strictEqual(sayNumber(3400), 'τρεις χιλιάδες τετρακόσια');
assert.strictEqual(sayNumber(12345), 'ένα δύο τρία τέσσερα πέντε');
assert.deepStrictEqual(mmsIds('Να', { 'ν': 5, 'α': 7, ' ': 1 }), [0, 1, 0, 1, 0, 5, 0, 7, 0, 1, 0, 1, 0]);
console.log('voxtral.js OK');
