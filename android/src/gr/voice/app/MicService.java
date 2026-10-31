package gr.voice.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.audiofx.AutomaticGainControl;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;

/**
 * «Μικρόφωνο PC»: this phone/tablet is one more microphone for VOICE on a computer (electron/main.js, POST /mic).
 * A foreground service, so it keeps sending with the screen off or locked; the page only starts, stops and polls it.
 * Raw 16 kHz mono Int16 PCM goes out every 250 ms; the computer mixes all the devices.
 */
public class MicService extends Service {
    static final int SR = 16000;
    // read by MainActivity.Bridge.micState(); written by the two threads below
    static volatile boolean on;
    static volatile long okAt;          // last time the computer took a chunk
    static volatile String problem = ""; // "code" (wrong pairing code) or "" while it only cannot reach it
    static volatile float level;        // rms of the latest 100 ms, for the wave

    String[] addrs = {};
    int port, cur;
    String code, query;
    Thread reader, sender;
    final ByteArrayOutputStream pending = new ByteArrayOutputStream();
    PowerManager.WakeLock wake;
    WifiManager.WifiLock wifi;

    @Override public IBinder onBind(Intent i) { return null; }

    @Override public int onStartCommand(Intent i, int flags, int id) {
        if (i == null || "stop".equals(i.getAction())) { stopSelf(); return START_NOT_STICKY; }
        if (on) return START_NOT_STICKY;
        addrs = i.getStringExtra("addrs").split(",");
        port = i.getIntExtra("port", 8790);
        code = i.getStringExtra("code");
        try {
            query = "/mic?id=" + URLEncoder.encode(i.getStringExtra("id"), "UTF-8") + "&name=" + URLEncoder.encode(i.getStringExtra("name"), "UTF-8");
        } catch (java.io.UnsupportedEncodingException e) { throw new RuntimeException(e); }

        if (Build.VERSION.SDK_INT >= 26) getSystemService(NotificationManager.class)
                .createNotificationChannel(new NotificationChannel("mic", "Μικρόφωνο PC", NotificationManager.IMPORTANCE_LOW));
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, MicService.class).setAction("stop"), PendingIntent.FLAG_IMMUTABLE);
        Notification n = (Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, "mic") : new Notification.Builder(this))
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentTitle("VOICE: μικρόφωνο για τον υπολογιστή")
                .setContentText("Στέλνει ήχο στο " + i.getStringExtra("pc"))
                .setContentIntent(open).setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "Διακοπή", stop).build())
                .build();
        if (Build.VERSION.SDK_INT >= 29) startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
        else startForeground(1, n);

        // screen off: the CPU and the Wi-Fi radio would sleep and the sound would arrive in bursts or not at all
        wake = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "voice:mic");
        wake.acquire();
        wifi = ((WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE)).createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "voice:mic");
        wifi.acquire();

        on = true;
        okAt = 0;
        problem = "";
        reader = new Thread(this::record, "voice-mic-rec");
        sender = new Thread(this::send, "voice-mic-send");
        reader.start();
        sender.start();
        return START_NOT_STICKY;
    }

    void record() {
        int min = AudioRecord.getMinBufferSize(SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord r;
        try {
            // VOICE_RECOGNITION: tuned for recognizers, no call-style processing
            r = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, SR, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, Math.max(min, SR));
        } catch (SecurityException e) { stopSelf(); return; }
        if (r.getState() != AudioRecord.STATE_INITIALIZED) { r.release(); stopSelf(); return; } // another app holds the mic
        // devices across the room hear the teacher at very different volumes; the phone's AGC evens that out
        if (AutomaticGainControl.isAvailable()) {
            AutomaticGainControl agc = AutomaticGainControl.create(r.getAudioSessionId());
            if (agc != null) agc.setEnabled(true);
        }
        r.startRecording();
        byte[] b = new byte[SR / 10 * 2];
        while (on) {
            int n = r.read(b, 0, b.length);
            if (n <= 0) continue;
            long sum = 0;
            for (int k = 0; k + 1 < n; k += 2) { int s = (short) ((b[k] & 0xff) | (b[k + 1] << 8)); sum += (long) s * s; }
            level = (float) Math.sqrt(sum / (double) (n / 2));
            synchronized (pending) {
                pending.write(b, 0, n);
                // a slow network: keep the newest 2 s, the computer would skip older sound anyway
                if (pending.size() > 4 * SR) {
                    byte[] all = pending.toByteArray();
                    pending.reset();
                    pending.write(all, all.length - 4 * SR, 4 * SR);
                }
            }
        }
        r.stop();
        r.release();
    }

    void send() {
        while (on) {
            long t = System.currentTimeMillis();
            byte[] body;
            synchronized (pending) { body = pending.toByteArray(); pending.reset(); }
            if (body.length > 0 && !post(body)) {
                cur = (cur + 1) % addrs.length; // the computer has several addresses (Wi-Fi, cable, hotspot): try the next
                try { Thread.sleep(500); } catch (InterruptedException e) { return; }
            }
            long wait = 250 - (System.currentTimeMillis() - t);
            if (wait > 0) try { Thread.sleep(wait); } catch (InterruptedException e) { return; }
        }
    }

    boolean post(byte[] body) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL("http://" + addrs[cur] + ":" + port + query).openConnection();
            c.setConnectTimeout(2000);
            c.setReadTimeout(3000);
            c.setDoOutput(true);
            c.setRequestMethod("POST");
            c.setRequestProperty("X-Voice-Code", code);
            c.setRequestProperty("Content-Type", "application/octet-stream");
            c.setFixedLengthStreamingMode(body.length);
            try (OutputStream o = c.getOutputStream()) { o.write(body); }
            int s = c.getResponseCode();
            if (s == 403) { problem = "code"; return true; } // the address is right; scanning again fixes the code
            if (s != 200) return false;
            problem = "";
            okAt = System.currentTimeMillis();
            return true;
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    @Override public void onDestroy() {
        on = false;
        level = 0;
        if (sender != null) sender.interrupt();
        if (wake != null && wake.isHeld()) wake.release();
        if (wifi != null && wifi.isHeld()) wifi.release();
        super.onDestroy();
    }
}
