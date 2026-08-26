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

/**
 * Edge-triggered desktop ops alerts: reauth, DD pause, halt, and new desk signals.
 * Separate notify IDs so one clear does not dismiss the others.
 */
object ReauthNotifier {
    private const val CHANNEL_ID = "ma_ops_alerts"
    private const val PREF = "ma_companion"

    private const val NOTIFY_REAUTH = 3301
    private const val NOTIFY_DD = 3302
    private const val NOTIFY_HALT = 3303
    private const val NOTIFY_SIGNAL = 3304

    private const val KEY_LAST_REAUTH = "last_reauth_needed"
    private const val KEY_LAST_DD = "last_dd_paused"
    private const val KEY_LAST_HALT = "last_halted"
    private const val KEY_LAST_SIGNAL_ID = "last_signal_alert_id"

    fun ensureChannel(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val mgr = ctx.getSystemService(NotificationManager::class.java) ?: return
        val ch = NotificationChannel(
            CHANNEL_ID,
            ctx.getString(R.string.ops_alerts_channel_name),
            NotificationManager.IMPORTANCE_HIGH,
        )
        mgr.createNotificationChannel(ch)
        mgr.deleteNotificationChannel("ma_reauth")
    }

    fun maybeNotify(ctx: Context, reauthNeeded: Boolean) {
        edgeNotify(
            ctx,
            active = reauthNeeded,
            key = KEY_LAST_REAUTH,
            notifyId = NOTIFY_REAUTH,
            titleRes = R.string.reauth_notify_title,
            bodyRes = R.string.reauth_notify_body,
        )
    }

    fun maybeNotifyDdPause(ctx: Context, ddPaused: Boolean) {
        edgeNotify(
            ctx,
            active = ddPaused,
            key = KEY_LAST_DD,
            notifyId = NOTIFY_DD,
            titleRes = R.string.dd_notify_title,
            bodyRes = R.string.dd_notify_body,
        )
    }

    fun maybeNotifyHalt(ctx: Context, halted: Boolean) {
        edgeNotify(
            ctx,
            active = halted,
            key = KEY_LAST_HALT,
            notifyId = NOTIFY_HALT,
            titleRes = R.string.halt_notify_title,
            bodyRes = R.string.halt_notify_body,
        )
    }

    /** Edge when desk radar top signal id changes (new scored BUY). */
    fun maybeNotifySignal(ctx: Context, alert: MonitorApi.SignalAlert?) {
        ensureChannel(ctx)
        val prefs = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)
        val id = alert?.id?.trim().orEmpty()
        val last = prefs.getString(KEY_LAST_SIGNAL_ID, "") ?: ""
        if (id.isBlank()) {
            if (last.isNotBlank()) {
                NotificationManagerCompat.from(ctx).cancel(NOTIFY_SIGNAL)
                prefs.edit().putString(KEY_LAST_SIGNAL_ID, "").apply()
            }
            return
        }
        if (id == last) return
        prefs.edit().putString(KEY_LAST_SIGNAL_ID, id).apply()
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
            NOTIFY_SIGNAL,
            open,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val body = ctx.getString(
            R.string.signal_notify_body,
            alert?.ticker ?: "—",
            alert?.engine ?: "SCAN",
            alert?.score ?: 0.0,
        )
        val note = NotificationCompat.Builder(ctx, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(ctx.getString(R.string.signal_notify_title))
            .setContentText(body)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setAutoCancel(true)
            .setContentIntent(pi)
            .build()
        NotificationManagerCompat.from(ctx).notify(NOTIFY_SIGNAL, note)
    }

    fun maybeNotifyFromStatus(
        ctx: Context,
        reauthNeeded: Boolean,
        ddPaused: Boolean,
        halted: Boolean,
        signalAlert: MonitorApi.SignalAlert? = null,
    ) {
        maybeNotify(ctx, reauthNeeded)
        maybeNotifyDdPause(ctx, ddPaused)
        maybeNotifyHalt(ctx, halted)
        maybeNotifySignal(ctx, signalAlert)
    }

    private fun edgeNotify(
        ctx: Context,
        active: Boolean,
        key: String,
        notifyId: Int,
        titleRes: Int,
        bodyRes: Int,
    ) {
        ensureChannel(ctx)
        val prefs = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)
        val wasActive = prefs.getBoolean(key, false)
        prefs.edit().putBoolean(key, active).apply()
        if (!active) {
            if (wasActive) {
                NotificationManagerCompat.from(ctx).cancel(notifyId)
            }
            return
        }
        if (wasActive) return
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
            notifyId,
            open,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val note = NotificationCompat.Builder(ctx, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(ctx.getString(titleRes))
            .setContentText(ctx.getString(bodyRes))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(pi)
            .build()
        NotificationManagerCompat.from(ctx).notify(notifyId, note)
    }

    fun clear(ctx: Context) {
        NotificationManagerCompat.from(ctx).cancel(NOTIFY_REAUTH)
        ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)
            .edit().putBoolean(KEY_LAST_REAUTH, false).apply()
    }

    fun clearAll(ctx: Context) {
        val nm = NotificationManagerCompat.from(ctx)
        nm.cancel(NOTIFY_REAUTH)
        nm.cancel(NOTIFY_DD)
        nm.cancel(NOTIFY_HALT)
        nm.cancel(NOTIFY_SIGNAL)
        ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(KEY_LAST_REAUTH, false)
            .putBoolean(KEY_LAST_DD, false)
            .putBoolean(KEY_LAST_HALT, false)
            .putString(KEY_LAST_SIGNAL_ID, "")
            .apply()
    }
}
