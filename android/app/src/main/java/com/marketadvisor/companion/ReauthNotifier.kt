package com.marketadvisor.companion

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat

object ReauthNotifier {
    private const val CHANNEL_ID = "ma_reauth"
    private const val NOTIFY_ID = 3301
    private const val PREF = "ma_companion"
    private const val KEY_LAST = "last_reauth_needed"

    fun ensureChannel(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val mgr = ctx.getSystemService(NotificationManager::class.java) ?: return
        val ch = NotificationChannel(
            CHANNEL_ID,
            ctx.getString(R.string.reauth_channel_name),
            NotificationManager.IMPORTANCE_HIGH,
        )
        mgr.createNotificationChannel(ch)
    }

    fun maybeNotify(ctx: Context, reauthNeeded: Boolean) {
        ensureChannel(ctx)
        val prefs = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)
        val wasNeeded = prefs.getBoolean(KEY_LAST, false)
        prefs.edit().putBoolean(KEY_LAST, reauthNeeded).apply()
        if (!reauthNeeded) {
            if (wasNeeded) {
                NotificationManagerCompat.from(ctx).cancel(NOTIFY_ID)
            }
            return
        }
        if (wasNeeded) return
        if (Build.VERSION.SDK_INT >= 33) {
            val ok = ContextCompat.checkSelfPermission(
                ctx,
                android.Manifest.permission.POST_NOTIFICATIONS,
            ) == PackageManager.PERMISSION_GRANTED
            if (!ok) return
        }
        val open = Intent(ctx, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pi = PendingIntent.getActivity(
            ctx,
            0,
            open,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val note = NotificationCompat.Builder(ctx, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(ctx.getString(R.string.reauth_notify_title))
            .setContentText(ctx.getString(R.string.reauth_notify_body))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(pi)
            .build()
        NotificationManagerCompat.from(ctx).notify(NOTIFY_ID, note)
    }

    fun clear(ctx: Context) {
        NotificationManagerCompat.from(ctx).cancel(NOTIFY_ID)
        ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)
            .edit().putBoolean(KEY_LAST, false).apply()
    }
}
