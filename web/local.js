// Offline helpers. Translation runs inside the page (desktop and Android alike): Mozilla's Firefox
// models el→en and en→el (Bergamot, WASM), 15 MB each, shipped with every package.
// sayNumber/mmsIds feed the Greek voice (Meta MMS), which electron/main.js runs natively.
'use strict';

const UNITS = 'μηδέν ένα δύο τρία τέσσερα πέντε έξι επτά οκτώ εννέα'.split(' ');
const TEENS = 'δέκα έντεκα δώδεκα δεκατρία δεκατέσσερα δεκαπέντε δεκαέξι δεκαεπτά δεκαοκτώ δεκαεννέα'.split(' ');
const TENS = '_ _ είκοσι τριάντα σαράντα πενήντα εξήντα εβδομήντα ογδόντα ενενήντα'.split(' ');
const HUNDREDS = '_ εκατό διακόσια τριακόσια τετρακόσια πεντακόσια εξακόσια επτακόσια οκτακόσια εννιακόσια'.split(' ');
const THOUSANDS = { 3: 'τρεις', 4: 'τέσσερις' }; // feminine before «χιλιάδες»

/** Greek words for 0..9999; longer numbers digit by digit (the voice cannot read digits). */
function sayNumber(n) {
  if (n >= 10000) return [...String(n)].map(d => UNITS[d]).join(' ');
  const words = [];
  const th = Math.floor(n / 1000), h = Math.floor(n / 100) % 10, r = n % 100;
  if (th) words.push(th === 1 ? 'χίλια' : `${THOUSANDS[th] || UNITS[th]} χιλιάδες`);
  if (h) words.push(h === 1 && r ? 'εκατόν' : HUNDREDS[h]);
  if (r >= 20) words.push(TENS[Math.floor(r / 10)] + (r % 10 ? ' ' + UNITS[r % 10] : ''));
  else if (r >= 10) words.push(TEENS[r - 10]);
  else if (r || !words.length) words.push(UNITS[r]);
  return words.join(' ');
}

/** VitsTokenizer for mms-tts-ell: lowercase, unknown characters dropped, a blank (0) around every symbol. */
function mmsIds(text, vocab) {
  text = text.replace(/\d+/g, m => ' ' + sayNumber(Number(m)) + ' ');
  // punctuation is not in the vocabulary: a space keeps its pause. Two spaces around the text give
  // very short phrases («Ναι.») room; without them VITS mumbles them almost silently.
  text = '  ' + text.replace(/[.,;:!?·…]+/g, ' ') + '  ';
  const ids = [0];
  for (const c of text.toLowerCase()) if (c in vocab) ids.push(vocab[c], 0);
  return ids;
}

const local = {
  base: 'models/', // Android: inside the APK; the desktop sets its own (see app.js)
  translator: null,
  /** from/to: 'el' | 'en'. */
  async translate(text, from, to) {
    if (from === to) return text;
    if (!this.translator) this.translator = (async () => {
      const M = await import('./vendor/bergamot/translator.js');
      const base = this.base;
      class Local extends M.TranslatorBacking {
        async loadModelRegistery() {
          return ['elen', 'enel'].map(p => ({ from: p.slice(0, 2), to: p.slice(2), files: {
            model: { name: `${base}bergamot/${p}/model.${p}.intgemm.alphas.bin` },
            lex: { name: `${base}bergamot/${p}/lex.50.50.${p}.s2t.bin` },
            vocab: { name: `${base}bergamot/${p}/vocab.${p}.spm` } } }));
        }
        // XHR, not fetch: Android's WebView cannot fetch() file:// URLs (the APK's assets)
        fetch(url) {
          return new Promise((ok, fail) => {
            const x = new XMLHttpRequest();
            x.open('GET', url);
            x.responseType = 'arraybuffer';
            x.onload = () => (x.status === 200 || x.status === 0) && x.response ? ok(x.response) : fail(new Error('δεν διαβάστηκε ' + url));
            x.onerror = () => fail(new Error('δεν διαβάστηκε ' + url));
            x.send();
          });
        }
      }
      return new M.LatencyOptimisedTranslator({}, new Local({ workerUrl: 'vendor/bergamot/worker/translator-worker.js', pivotLanguage: null }));
    })().catch(e => { this.translator = null; throw e; });
    // one at a time: this translator cancels a waiting request when a newer one arrives
    const tr = await this.translator;
    const job = (this.queue || Promise.resolve()).then(() => tr.translate({ from, to, text, html: false }));
    this.queue = job.catch(() => {});
    // the small model doubles a lone word with a full stop («Δύο.» → «Two. Two.»)
    return (await job).target.text.replace(/^(.+?)\s+\1$/, '$1');
  },
};

if (typeof module !== 'undefined') module.exports = { sayNumber, mmsIds };
