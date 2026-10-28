package gr.voice.app;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Insets;
import android.media.AudioManager;
import android.os.Build;
import android.os.Bundle;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognitionSupport;
import android.speech.RecognitionSupportCallback;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.util.Base64;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.FrameLayout;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;
import java.security.KeyStore;
import java.util.Locale;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * VOICE for Android: hosts the shared web UI (../web, packed into assets) in a WebView.
 * The page does capture and the Voxtral calls itself; this class only gives it the microphone,
 * the Keystore-protected API key, the system TTS, sharing and keep-screen-on (see Bridge).
 */
public class MainActivity extends Activity {
    WebView web;
    SharedPreferences prefs;
    TextToSpeech tts;
    String ttsProblem = "Η εκφώνηση ξεκινά…";
    PermissionRequest pendingMic;
    android.webkit.ValueCallback<android.net.Uri[]> fileCb;
    volatile String lang = "el-GR"; // set from Settings → Γλώσσα ομιλίας
    // on-device captions: Android's SpeechRecognizer, restarted after every utterance so it never stops
    final Handler ui = new Handler(Looper.getMainLooper());
    SpeechRecognizer recognizer;
    boolean listening, onDevice, triedOnDevice, preferOffline = true;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        prefs = getSharedPreferences("voice", MODE_PRIVATE);
        boolean dark = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;

        web = new WebView(this);
        // a WebView ignores its own padding, so the bars' insets go on a frame around it
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(dark ? 0xFF121416 : 0xFFEEF0F1);
        web.setBackgroundColor(dark ? 0xFF121416 : 0xFFEEF0F1);
        root.addView(web);
        // targetSdk 35+ draws edge-to-edge: pad for status/nav bars and the keyboard ourselves
        root.setOnApplyWindowInsetsListener((v, in) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                Insets i = in.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
                v.setPadding(i.left, i.top, i.right, i.bottom);
                return WindowInsets.CONSUMED;
            }
            v.setPadding(in.getSystemWindowInsetLeft(), in.getSystemWindowInsetTop(),
                    in.getSystemWindowInsetRight(), in.getSystemWindowInsetBottom());
            return in.consumeSystemWindowInsets();
        });
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);           // localStorage keeps settings and the last transcript
        // the offline translator reads its model files from the APK's assets (file:///android_asset/models)
        s.setAllowFileAccess(true);
        s.setAllowFileAccessFromFileURLs(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        web.addJavascriptInterface(new Bridge(), "VoiceAndroid");
        web.setWebChromeClient(new WebChromeClient() {
            // <input type=file> (Ανάγνωση → Άνοιγμα αρχείου) needs the app to show the system picker
            @Override public boolean onShowFileChooser(WebView v, android.webkit.ValueCallback<android.net.Uri[]> cb, FileChooserParams params) {
                if (fileCb != null) fileCb.onReceiveValue(null);
                fileCb = cb;
                try { startActivityForResult(params.createIntent(), 7); }
                catch (RuntimeException e) { fileCb = null; return false; }
                return true;
            }

            @Override public void onPermissionRequest(PermissionRequest req) {
                runOnUiThread(() -> {
                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                        req.grant(req.getResources());
                    } else {
                        pendingMic = req;
                        requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1);
                    }
                });
            }
        });
        setContentView(root);
        web.loadUrl("file:///android_asset/index.html");

        tts = new TextToSpeech(this, status -> {
            if (status != TextToSpeech.SUCCESS) { ttsProblem = "Δεν βρέθηκε μηχανή εκφώνησης στη συσκευή."; return; }
            int r = tts.setLanguage(new Locale("el", "GR"));
            ttsProblem = r == TextToSpeech.LANG_MISSING_DATA || r == TextToSpeech.LANG_NOT_SUPPORTED
                    ? "Λείπει η ελληνική φωνή: Ρυθμίσεις συσκευής → Μετατροπή κειμένου σε ομιλία → εγκατάσταση ελληνικών."
                    : "";
            tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                @Override public void onStart(String id) {}
                @Override public void onDone(String id) { if (id.startsWith("last")) ended(); }
                @Override public void onError(String id) { ended(); }
            });
        });
    }

    void js(String fn, String a, String b) {
        if (a.equals("fatal")) runOnUiThread(() -> beeps(true)); // captions stopped for good: sound back on
        String call = "window." + fn + " && " + fn + "(" + JSONObject.quote(a) + "," + JSONObject.quote(b) + ")";
        runOnUiThread(() -> web.evaluateJavascript(call, null));
    }

    Intent recognizeIntent() {
        return new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE, lang)
                .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                .putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, preferOffline)
                .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
                .putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, getPackageName());
    }

    /** Main thread only. Prefers the on-device recognizer (API 31+): no network, no audio leaves the tablet. */
    void listen() {
        if (!listening) return;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 2);
            return;
        }
        if (recognizer == null) {
            onDevice = !triedOnDevice && Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(this);
            if (onDevice) recognizer = SpeechRecognizer.createOnDeviceSpeechRecognizer(this);
            else if (SpeechRecognizer.isRecognitionAvailable(this)) recognizer = SpeechRecognizer.createSpeechRecognizer(this);
            else { listening = false; js("onRecog", "fatal", "Η συσκευή δεν έχει υπηρεσία αναγνώρισης ομιλίας (π.χ. την εφαρμογή Google)."); return; }
            recognizer.setRecognitionListener(new Listener());
            js("onRecog", "engine", onDevice ? "device" : "service");
        }
        recognizer.startListening(recognizeIntent());
    }

    void restart(long delay) {
        if (listening) ui.postDelayed(this::listen, delay);
    }

    /** Google's recognizer beeps on every restart, i.e. every few seconds in class: mute media while captions run. */
    void beeps(boolean on) {
        try {
            ((AudioManager) getSystemService(AUDIO_SERVICE)).adjustStreamVolume(AudioManager.STREAM_MUSIC,
                    on ? AudioManager.ADJUST_UNMUTE : AudioManager.ADJUST_MUTE, 0);
        } catch (RuntimeException ignored) { } // some devices refuse in Do Not Disturb; the beep is only a nuisance
    }

    void dropRecognizer() {
        if (recognizer != null) { recognizer.destroy(); recognizer = null; }
    }

    static String best(Bundle b) {
        java.util.ArrayList<String> r = b == null ? null : b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
        return r == null || r.isEmpty() || r.get(0) == null ? "" : r.get(0);
    }

    // Noise (footsteps, slippers on the floor) never sounds like silence, so the recognizer
    // keeps the phrase open forever. End it ourselves: 1.5 s without new words, or 15 s in all.
    final Runnable endPhrase = () -> { if (recognizer != null && listening) recognizer.stopListening(); };
    final Runnable capPhrase = () -> { if (recognizer != null && listening) recognizer.stopListening(); };
    String lastPartial = "";

    void phraseDone() { ui.removeCallbacks(endPhrase); ui.removeCallbacks(capPhrase); lastPartial = ""; }

    class Listener implements RecognitionListener {
        @Override public void onReadyForSpeech(Bundle p) {}
        @Override public void onBeginningOfSpeech() { ui.removeCallbacks(capPhrase); ui.postDelayed(capPhrase, 15000); }
        @Override public void onRmsChanged(float db) { js("onRecog", "level", Float.toString(db)); }
        @Override public void onBufferReceived(byte[] b) {}
        @Override public void onEndOfSpeech() {}
        @Override public void onEvent(int t, Bundle p) {}
        @Override public void onPartialResults(Bundle b) {
            String t = best(b);
            if (t.isEmpty() || t.equals(lastPartial)) return;
            lastPartial = t;
            js("onRecog", "partial", t);
            ui.removeCallbacks(endPhrase);
            ui.postDelayed(endPhrase, 1500);
        }
        @Override public void onResults(Bundle b) { phraseDone(); js("onRecog", "final", best(b)); restart(0); }
        @Override public void onError(int e) {
            phraseDone();
            switch (e) {
                case SpeechRecognizer.ERROR_NO_MATCH:
                case SpeechRecognizer.ERROR_SPEECH_TIMEOUT:
                    js("onRecog", "final", "");
                    restart(0);
                    break;
                case SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS:
                    listening = false;
                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) micDenied();
                    // VOICE has it, so the recognizer (the Google app) is the one without the microphone
                    else js("onRecog", "fatal", "Η εφαρμογή Google, που κάνει την αναγνώριση, δεν έχει άδεια μικροφώνου: Ρυθμίσεις → Εφαρμογές → Google → Άδειες → Μικρόφωνο → Να επιτρέπεται.");
                    break;
                case 12: // ERROR_LANGUAGE_NOT_SUPPORTED (API 31)
                case 13: // ERROR_LANGUAGE_UNAVAILABLE: the Greek offline pack is not downloaded
                    if (onDevice) {
                        // fall back to the recognition service, which may still have Greek (online or its own pack)
                        triedOnDevice = true;
                        dropRecognizer();
                        js("onRecog", "error", "Τα ελληνικά δεν είναι κατεβασμένα για χρήση χωρίς internet· δοκιμάζω την κανονική υπηρεσία.");
                        restart(300);
                    } else if (preferOffline) {
                        // Google's service has no offline Greek pack yet: go online rather than show nothing
                        preferOffline = false;
                        js("onRecog", "error", "Δεν υπάρχει το ελληνικό πακέτο χωρίς internet· οι υπότιτλοι γίνονται με internet. Δες Ρυθμίσεις → Αναγνώριση ομιλίας.");
                        restart(300);
                    } else {
                        listening = false;
                        js("onRecog", "fatal", "Τα ελληνικά δεν είναι διαθέσιμα στην αναγνώριση ομιλίας της συσκευής. Δες Ρυθμίσεις → Αναγνώριση ομιλίας.");
                    }
                    break;
                case SpeechRecognizer.ERROR_NETWORK:
                case SpeechRecognizer.ERROR_NETWORK_TIMEOUT:
                case SpeechRecognizer.ERROR_SERVER:
                    js("onRecog", "error", "Η αναγνώριση χρειάζεται internet ή το ελληνικό πακέτο χωρίς internet. Ξαναδοκιμάζω…");
                    restart(2000);
                    break;
                default: // busy / client / audio: start over with a fresh recognizer
                    dropRecognizer();
                    restart(500);
            }
        }
    }

    void ended() { runOnUiThread(() -> web.evaluateJavascript("window.onSpeakEnd && onSpeakEnd()", null)); }

    @Override protected void onActivityResult(int code, int result, Intent data) {
        if (code == 7 && fileCb != null) {
            fileCb.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data));
            fileCb = null;
        } else super.onActivityResult(code, result, data);
    }

    @Override public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        boolean granted = results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED;
        if (code == 2) {
            if (granted) listen();
            else { listening = false; micDenied(); }
            return;
        }
        if (pendingMic == null) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) pendingMic.grant(pendingMic.getResources());
        else { pendingMic.deny(); micDenied(); }
        pendingMic = null;
    }

    /** After "Don't allow" Android stops asking, so say where to switch it on and open that page. */
    void micDenied() {
        js("onRecog", "fatal", "Το VOICE δεν έχει άδεια μικροφώνου. Στη σελίδα που άνοιξε: Άδειες → Μικρόφωνο → Να επιτρέπεται.");
        startActivity(new android.content.Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                android.net.Uri.fromParts("package", getPackageName(), null)));
    }

    @Override protected void onDestroy() {
        if (listening) beeps(true);
        listening = false;
        dropRecognizer();
        tts.shutdown();
        web.destroy();
        super.onDestroy();
    }

    /** window.VoiceAndroid in the page. Called on a WebView thread, not the UI thread. */
    class Bridge {
        @JavascriptInterface public String getKey() {
            String s = prefs.getString("key", "");
            if (s.isEmpty()) return "";
            try {
                String[] parts = s.split(":");
                Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
                c.init(Cipher.DECRYPT_MODE, keystoreKey(), new GCMParameterSpec(128, Base64.decode(parts[0], Base64.NO_WRAP)));
                return new String(c.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)), StandardCharsets.UTF_8);
            } catch (Exception e) {
                return ""; // keystore key lost (e.g. device reset): the user enters the key again
            }
        }

        @JavascriptInterface public boolean setKey(String key) {
            try {
                Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
                c.init(Cipher.ENCRYPT_MODE, keystoreKey());
                String enc = Base64.encodeToString(c.getIV(), Base64.NO_WRAP) + ":"
                        + Base64.encodeToString(c.doFinal(key.trim().getBytes(StandardCharsets.UTF_8)), Base64.NO_WRAP);
                return prefs.edit().putString("key", enc).commit();
            } catch (Exception e) {
                return false;
            }
        }

        @JavascriptInterface public void speak(String text, float rate) {
            tts.stop();
            tts.setSpeechRate(rate);
            // engines refuse utterances above a max length, so queue it in pieces; the last one reports the end
            int max = Math.min(TextToSpeech.getMaxSpeechInputLength(), 3000);
            java.util.List<String> parts = new java.util.ArrayList<>();
            for (String para : text.split("\n+")) {
                for (int i = 0; i < para.length(); i += max) {
                    String p = para.substring(i, Math.min(para.length(), i + max)).trim();
                    if (!p.isEmpty()) parts.add(p);
                }
            }
            if (parts.isEmpty()) { ended(); return; }
            for (int i = 0; i < parts.size(); i++) {
                int r = tts.speak(parts.get(i), TextToSpeech.QUEUE_ADD, null, (i == parts.size() - 1 ? "last" : "u") + i);
                if (r != TextToSpeech.SUCCESS) { ended(); return; }
            }
        }

        @JavascriptInterface public void stopSpeak() { tts.stop(); }

        @JavascriptInterface public String ttsInfo() {
            return ttsProblem.isEmpty() ? "Φωνή: η ελληνική φωνή της συσκευής." : ttsProblem;
        }

        @JavascriptInterface public boolean share(String text) {
            runOnUiThread(() -> startActivity(Intent.createChooser(new Intent(Intent.ACTION_SEND)
                    .setType("text/plain").putExtra(Intent.EXTRA_TEXT, text), "Κοινοποίηση")));
            return false; // nothing saved yet; the share sheet takes it from here
        }

        @JavascriptInterface public void setLang(String l) { lang = l; }

        @JavascriptInterface public void listen(boolean on) {
            runOnUiThread(() -> {
                listening = on;
                ui.removeCallbacksAndMessages(null);
                beeps(!on);
                if (on) { triedOnDevice = false; preferOffline = true; MainActivity.this.listen(); }
                else dropRecognizer();
            });
        }

        /** Reports, through window.onRecogInfo(json), what local recognition this device can do. */
        @JavascriptInterface public void checkRecognizer() {
            runOnUiThread(() -> {
                JSONObject o = new JSONObject();
                try {
                    o.put("service", SpeechRecognizer.isRecognitionAvailable(MainActivity.this));
                    o.put("onDevice", Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(MainActivity.this));
                    o.put("sdk", Build.VERSION.SDK_INT);
                } catch (Exception ignored) { }
                if (Build.VERSION.SDK_INT < 33 || !o.optBoolean("onDevice")) { info(o); return; }
                SpeechRecognizer r = SpeechRecognizer.createOnDeviceSpeechRecognizer(MainActivity.this);
                r.checkRecognitionSupport(recognizeIntent(), getMainExecutor(), new RecognitionSupportCallback() {
                    @Override public void onSupportResult(RecognitionSupport s) {
                        try {
                            o.put("greek", hasGreek(s.getInstalledOnDeviceLanguages()) ? "installed"
                                    : hasGreek(s.getPendingOnDeviceLanguages()) ? "pending"
                                    : hasGreek(s.getSupportedOnDeviceLanguages()) ? "downloadable" : "none");
                        } catch (Exception ignored) { }
                        r.destroy();
                        info(o);
                    }
                    @Override public void onError(int e) { r.destroy(); info(o); }
                });
            });
        }

        /** Android 13+: asks the on-device recognizer to download Greek (the system shows its own progress). */
        @JavascriptInterface public void downloadGreek() {
            runOnUiThread(() -> {
                if (Build.VERSION.SDK_INT < 33) return;
                SpeechRecognizer r = SpeechRecognizer.createOnDeviceSpeechRecognizer(MainActivity.this);
                r.triggerModelDownload(recognizeIntent());
                ui.postDelayed(r::destroy, 5000);
            });
        }

        @JavascriptInterface public void fullscreen(boolean on) {
            runOnUiThread(() -> {
                if (Build.VERSION.SDK_INT < 30) return;
                WindowInsetsController c = getWindow().getInsetsController();
                if (on) {
                    c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                    c.hide(WindowInsets.Type.systemBars());
                } else c.show(WindowInsets.Type.systemBars());
            });
        }

        @JavascriptInterface public void keepAwake(boolean on) {
            // recording stops if the screen sleeps
            runOnUiThread(() -> {
                if (on) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            });
        }
    }

    void info(JSONObject o) { runOnUiThread(() -> web.evaluateJavascript("window.onRecogInfo && onRecogInfo(" + o + ")", null)); }

    static boolean hasGreek(java.util.List<String> langs) {
        for (String l : langs) if (l.toLowerCase(Locale.ROOT).startsWith("el")) return true;
        return false;
    }

    /** AES key that lives in the Android Keystore and never leaves it. */
    static SecretKey keystoreKey() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        if (!ks.containsAlias("voice-api-key")) {
            KeyGenerator g = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            g.init(new KeyGenParameterSpec.Builder("voice-api-key",
                    KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .build());
            return g.generateKey();
        }
        return (SecretKey) ks.getKey("voice-api-key", null);
    }
}
