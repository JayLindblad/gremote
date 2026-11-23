package com.gremote.messenger;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Base64;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.nio.charset.StandardCharsets;

/**
 * Shows one message on top of everything, then goes away.
 *
 * Extras (all optional except text):
 *   text     message, base64 UTF-8 (so any characters survive the ADB shell)
 *   title    heading, base64 UTF-8
 *   mode     "banner" (strip at the top; video keeps playing and the remote keeps working)
 *            or "full" (covers the screen; any remote key closes it)
 *   seconds  how long it stays up (0 = until a key is pressed, full mode only)
 *   clear    true: just remove what is showing
 *
 * The overlay needs "display over other apps"; gremote grants it with
 * `appops set com.gremote.messenger SYSTEM_ALERT_WINDOW allow`.
 */
public class MessageService extends Service {
    private static final String CHANNEL = "messages";
    private final Handler handler = new Handler(Looper.getMainLooper());
    private WindowManager windows;
    private View shown;
    private final Runnable hide = this::finish;

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        windows = (WindowManager) getSystemService(WINDOW_SERVICE);
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(
                new NotificationChannel(CHANNEL, "Messages", NotificationManager.IMPORTANCE_MIN));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification n = new Notification.Builder(this, CHANNEL)
                .setContentTitle("gremote message")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .build();
        startForeground(1, n);
        if (intent == null || intent.getBooleanExtra("clear", false)) {
            finish();
            return START_NOT_STICKY;
        }
        String text = decode(intent.getStringExtra("text"));
        String title = decode(intent.getStringExtra("title"));
        boolean full = "full".equals(intent.getStringExtra("mode"));
        int seconds = intent.getIntExtra("seconds", 10);
        if (!full && seconds <= 0) seconds = 10;
        show(title, text, full, seconds);
        return START_NOT_STICKY;
    }

    private static String decode(String b64) {
        if (b64 == null || b64.isEmpty()) return "";
        try {
            return new String(Base64.decode(b64, Base64.DEFAULT), StandardCharsets.UTF_8);
        } catch (IllegalArgumentException e) {
            return b64;
        }
    }

    private int dp(float v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
                getResources().getDisplayMetrics());
    }

    private void show(String title, String text, boolean full, int seconds) {
        removeShown();
        handler.removeCallbacks(hide);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER_HORIZONTAL);
        int pad = dp(full ? 48 : 20);
        box.setPadding(pad, dp(full ? 40 : 16), pad, dp(full ? 40 : 16));
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.argb(full ? 235 : 225, 16, 18, 24));
        bg.setCornerRadius(dp(full ? 0 : 14));
        bg.setStroke(full ? 0 : dp(2), Color.argb(255, 255, 196, 64));
        box.setBackground(bg);

        if (!title.isEmpty()) {
            TextView t = new TextView(this);
            t.setText(title);
            t.setTextColor(Color.rgb(255, 196, 64));
            t.setTextSize(TypedValue.COMPLEX_UNIT_SP, full ? 40 : 22);
            t.setGravity(Gravity.CENTER);
            t.setTypeface(t.getTypeface(), android.graphics.Typeface.BOLD);
            box.addView(t);
        }
        TextView body = new TextView(this);
        body.setText(text);
        body.setTextColor(Color.WHITE);
        body.setTextSize(TypedValue.COMPLEX_UNIT_SP, full ? 56 : 28);
        body.setGravity(Gravity.CENTER);
        body.setLineSpacing(0, 1.1f);
        box.addView(body);
        if (full) {
            TextView hint = new TextView(this);
            hint.setText("Press any button on the remote to close");
            hint.setTextColor(Color.argb(160, 255, 255, 255));
            hint.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
            hint.setGravity(Gravity.CENTER);
            hint.setPadding(0, dp(32), 0, 0);
            box.addView(hint);
        }

        WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                full ? WindowManager.LayoutParams.MATCH_PARENT
                     : WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
                        | (full ? 0 : WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                                    | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE),
                PixelFormat.TRANSLUCENT);
        lp.gravity = Gravity.TOP | Gravity.CENTER_HORIZONTAL;

        View root;
        if (full) {
            // Focusable so the remote's keys reach us: any key closes the message.
            FrameLayout frame = new FrameLayout(this) {
                @Override
                public boolean dispatchKeyEvent(KeyEvent e) {
                    if (e.getAction() == KeyEvent.ACTION_UP) finish();
                    return true;
                }
            };
            frame.setFocusable(true);
            frame.setFocusableInTouchMode(true);
            frame.addView(box, new FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
            box.setGravity(Gravity.CENTER);
            root = frame;
        } else {
            FrameLayout frame = new FrameLayout(this);
            int m = dp(32);
            frame.setPadding(m * 3, m, m * 3, 0);
            frame.addView(box, new FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT));
            root = frame;
        }

        try {
            windows.addView(root, lp);
        } catch (RuntimeException e) {
            // No overlay permission (or the window manager refused): nothing we can show.
            android.util.Log.e("gremote", "could not show message", e);
            finish();
            return;
        }
        shown = root;
        if (full) root.requestFocus();
        if (seconds > 0) handler.postDelayed(hide, seconds * 1000L);
    }

    private void removeShown() {
        if (shown != null) {
            try { windows.removeView(shown); } catch (RuntimeException ignored) { }
            shown = null;
        }
    }

    private void finish() {
        handler.removeCallbacks(hide);
        removeShown();
        stopForeground(true);
        stopSelf();
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacks(hide);
        removeShown();
        super.onDestroy();
    }
}
