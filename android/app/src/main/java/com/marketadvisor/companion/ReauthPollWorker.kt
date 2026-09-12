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
            val needBrokers = status.brokers.filter { it.value.reauthNeeded }.keys.toList()
            val dd = status.portfolioHeat.ddPaused
            ReauthNotifier.maybeNotifyFromStatus(
                applicationContext,
                reauthNeeded = needBrokers.isNotEmpty(),
                ddPaused = dd,
                halted = status.halted,
                signalAlert = status.signalAlert,
                advisorCount = status.advisor.count,
                reauthBrokers = needBrokers,
            )
            Result.success()
        } catch (e: Exception) {
            val tls = e is MonitorApiException && e.kind == MonitorApiException.KIND_TLS
            ReauthNotifier.maybeNotifyUnreachable(
                applicationContext,
                unreachable = true,
                tlsPin = tls,
            )
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
                ExistingPeriodicWorkPolicy.KEEP,
                req,
            )
        }
    }
}
