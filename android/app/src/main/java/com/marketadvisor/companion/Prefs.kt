package com.marketadvisor.companion

import android.content.Context

object Prefs {
    private const val NAME = "ma_companion"
    private const val KEY_URL = "base_url"
    private const val KEY_USER = "username"
    private const val KEY_PASS = "password"
    private const val KEY_FP = "tls_fingerprint"

    fun baseUrl(ctx: Context): String =
        ctx.getSharedPreferences(NAME, Context.MODE_PRIVATE)
            .getString(KEY_URL, "https://127.0.0.1:8791/")!!
            .trim()
            .let { if (it.endsWith("/")) it else "$it/" }

    fun username(ctx: Context): String =
        ctx.getSharedPreferences(NAME, Context.MODE_PRIVATE).getString(KEY_USER, "") ?: ""

    fun password(ctx: Context): String =
        ctx.getSharedPreferences(NAME, Context.MODE_PRIVATE).getString(KEY_PASS, "") ?: ""

    fun fingerprint(ctx: Context): String =
        ctx.getSharedPreferences(NAME, Context.MODE_PRIVATE).getString(KEY_FP, "") ?: ""

    fun save(ctx: Context, url: String, user: String, pass: String, fingerprint: String) {
        ctx.getSharedPreferences(NAME, Context.MODE_PRIVATE).edit()
            .putString(KEY_URL, url.trim())
            .putString(KEY_USER, user.trim())
            .putString(KEY_PASS, pass)
            .putString(KEY_FP, fingerprint.trim())
            .apply()
    }
}
