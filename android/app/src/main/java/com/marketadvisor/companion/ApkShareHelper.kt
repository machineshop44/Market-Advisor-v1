package com.marketadvisor.companion

import android.content.ClipData
import android.content.Context
import android.content.Intent
import android.util.Log
import android.widget.Toast
import androidx.core.content.FileProvider
import java.io.File

/**
 * Copies this install's APK into cache and opens the system share sheet
 * (Nearby Share / Quick Share, Bluetooth, Drive, etc.).
 * Mirrors Ava Bedtime / Arrs Hub Status FileProvider share pattern.
 */
object ApkShareHelper {
    private const val TAG = "ApkShare"
    private const val SHARE_NAME = "MarketAdvisorCompanion-update.apk"

    fun shareInstalledApk(context: Context) {
        runCatching {
            val src = File(context.applicationInfo.sourceDir)
            if (!src.exists()) error("Installed APK not found")

            val outDir = File(context.cacheDir, "share").apply { mkdirs() }
            val out = File(outDir, SHARE_NAME)
            src.inputStream().use { input ->
                out.outputStream().use { output -> input.copyTo(output) }
            }

            val authority = "${context.packageName}.fileprovider"
            val uri = FileProvider.getUriForFile(context, authority, out)

            val send = Intent(Intent.ACTION_SEND).apply {
                // octet-stream is more often accepted by Quick Share than package-archive,
                // while keeping the .apk filename so the receiver can install it directly.
                type = "application/octet-stream"
                putExtra(Intent.EXTRA_STREAM, uri)
                putExtra(Intent.EXTRA_SUBJECT, "Market Advisor Companion update")
                putExtra(
                    Intent.EXTRA_TEXT,
                    "Open MarketAdvisorCompanion-update.apk and tap Install. " +
                        "Allow installs from Files if Android asks.",
                )
                clipData = ClipData.newUri(
                    context.contentResolver,
                    "Market Advisor Companion update",
                    uri,
                )
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }

            val chooser = Intent.createChooser(send, "Share Market Advisor Companion APK").apply {
                clipData = send.clipData
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(chooser)
            Log.i(TAG, "Sharing APK (${out.length()} bytes)")
        }.onFailure { err ->
            Log.e(TAG, "Share failed", err)
            Toast.makeText(
                context,
                "Could not share app: ${err.message}",
                Toast.LENGTH_LONG,
            ).show()
        }
    }
}
