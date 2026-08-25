package com.marketadvisor.companion

import android.net.Uri
import org.json.JSONObject

/**
 * Decode companion setup QR payloads.
 * Primary: ma-companion://v1?url=...&user=...&pass=...&fp=...
 * Also accepts compact JSON: {"v":1,"url":"...","user":"...","pass":"...","fp":"..."}.
 */
object CompanionSetupQr {
    const val SCHEME = "ma-companion"
    const val VERSION = 1

    data class Setup(
        val url: String,
        val user: String = "",
        val pass: String = "",
        val fingerprint: String = "",
    )

    fun parse(raw: String): Setup {
        val text = raw.trim()
        require(text.isNotEmpty()) { "empty payload" }
        return if (text.startsWith("{")) parseJson(text) else parseUri(text)
    }

    private fun parseUri(text: String): Setup {
        val uri = Uri.parse(text)
        require(uri.scheme == SCHEME) { "unsupported scheme: ${uri.scheme}" }
        val host = uri.host ?: uri.pathSegments.firstOrNull().orEmpty()
        require(host.startsWith("v")) { "missing version" }
        val ver = host.removePrefix("v").toIntOrNull()
            ?: throw IllegalArgumentException("invalid version: $host")
        require(ver == VERSION) { "unsupported payload version: $ver" }

        val url = uri.getQueryParameter("url")?.trim().orEmpty()
        require(url.isNotEmpty()) { "missing url" }
        return Setup(
            url = url,
            user = uri.getQueryParameter("user")?.trim().orEmpty(),
            pass = uri.getQueryParameter("pass").orEmpty(),
            fingerprint = uri.getQueryParameter("fp")?.trim().orEmpty(),
        )
    }

    private fun parseJson(text: String): Setup {
        val obj = JSONObject(text)
        val ver = obj.optInt("v", 0)
        require(ver == VERSION) { "unsupported payload version: $ver" }
        val url = obj.optString("url", "").trim()
        require(url.isNotEmpty()) { "missing url" }
        return Setup(
            url = url,
            user = obj.optString("user", "").trim(),
            pass = obj.optString("pass", ""),
            fingerprint = obj.optString("fp", "").trim(),
        )
    }
}
