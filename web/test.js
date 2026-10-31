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

// automixer: the device nearer the speaker leads, the same voice is not doubled, mute means silent
{
  const tone = (n, amp) => new Int16Array(n).map((_, i) => Math.round(amp * Math.sin(i / 3)));
  const mix = new v.AutoMixer(), B = v.SR / 10, out = [];
  // 1 s of room noise on both, then the left phone hears a voice loud and the right one faintly, then the reverse
  const feed = (l, r, blocks) => { for (let k = 0; k < blocks; k++) { mix.push('L', tone(B, l)); mix.push('R', tone(B, r)); out.push(v.rms(mix.pull(B), 0, B)); } };
  feed(100, 100, 10);
  assert.strictEqual(out.slice(0, 4).reduce((a, b) => a + b), 0, 'nothing comes out before 0.5 s is buffered');
  feed(8000, 1500, 20);
  feed(1500, 8000, 20);
  const late = (from, to) => out.slice(from, to).reduce((a, b) => a + b) / (to - from);
  // allow for the 0.5 s buffer: blocks 20–30 are the left voice, 40–50 the right one
  assert.ok(late(20, 30) > 5000 && late(20, 30) < 6200, 'left leads: ' + late(20, 30));
  assert.ok(late(40, 50) > 5000 && late(40, 50) < 6200, 'right leads: ' + late(40, 50));
  // both hear it equally: about one voice's level, not two summed
  const same = new v.AutoMixer(), lv = [];
  for (let k = 0; k < 30; k++) { same.push('a', tone(B, k < 10 ? 100 : 6000)); same.push('b', tone(B, k < 10 ? 100 : 6000)); lv.push(v.rms(same.pull(B), 0, B)); }
  assert.ok(lv[29] < 4600, 'not doubled: ' + lv[29]);
  // a muted device never comes through, even when it is the only loud one
  const mute = new v.AutoMixer();
  let last = 0;
  for (let k = 0; k < 30; k++) {
    mute.push('a', tone(B, 100)); mute.push('b', tone(B, k < 10 ? 100 : 8000));
    if (k === 10) mute.inputs.get('b').muted = true;
    last = v.rms(mute.pull(B), 0, B);
  }
  assert.ok(last < 200, 'muted device leaks: ' + last);
  // a device that fell far behind skips ahead to the 0.5 s buffer
  const lag = new v.AutoMixer();
  lag.push('a', tone(3 * v.SR, 100));
  lag.pull(B);
  assert.strictEqual(lag.inputs.get('a').len, v.SR / 2 - B);
}

// the QR code the phone scans: round trip, and junk is refused
{
  const p = { addrs: ['192.168.1.5', '10.0.0.2'], port: 8790, code: '0427', pc: 'Λάπτοπ & Co' };
  assert.deepStrictEqual(v.parsePair(v.pairText(p)), p);
  assert.strictEqual(v.parsePair('https://example.com'), null);
  assert.strictEqual(v.parsePair('voicemic:1?a=evil.com&p=8790&c=1234'), null);
  assert.strictEqual(v.parsePair('voicemic:1?a=10.0.0.1&p=8790&c=12'), null);
}

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
