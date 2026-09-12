package com.marketadvisor.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Reschedule companion desk polling after device reboot. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val action = intent?.action ?: return
        if (
            action != Intent.ACTION_BOOT_COMPLETED &&
            action != Intent.ACTION_MY_PACKAGE_REPLACED
        ) {
            return
        }
        ReauthPollWorker.schedule(context.applicationContext)
    }
}
