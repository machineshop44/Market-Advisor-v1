package com.marketadvisor.companion

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

class ReauthPollWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        val url = Prefs.baseUrl(applicationContext)
        val user = Prefs.username(applicationContext)
        val pass = Prefs.password(applicationContext)
        val pin = Prefs.fingerprint(applicationContext)
        if (pin.isBlank()) return Result.success()
        return try {
            val status = MonitorApi.fetchStatus(url, user, pass, pin)
            val need = status.brokers["E*TRADE"]?.reauthNeeded == true
            ReauthNotifier.maybeNotify(applicationContext, need)
            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }

    companion object {
        private const val UNIQUE = "ma_reauth_poll"

        fun schedule(ctx: Context) {
            val req = PeriodicWorkRequestBuilder<ReauthPollWorker>(15, TimeUnit.MINUTES)
                .build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                UNIQUE,
                ExistingPeriodicWorkPolicy.UPDATE,
                req,
            )
        }
    }
}
