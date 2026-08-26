package com.marketadvisor.companion

import android.util.Base64
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL
import java.security.KeyStore
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

class MonitorApiException(
    message: String,
    val httpCode: Int = 0,
    val lockoutSeconds: Int? = null,
) : Exception(message)

object MonitorApi {
    data class BrokerInfo(
        val connected: Boolean,
        val liveTrading: Boolean,
        val reauthNeeded: Boolean,
        val armed: Boolean,
        val ddPause: Boolean = false,
        val ddReason: String = "",
        val environment: String = "",
        val sandboxNoBp: Boolean = false,
        val liveZeroBp: Boolean = false,
        val buyEnginesParked: Boolean = false,
        val buyingPower: Double? = null,
        val protectiveStops: Boolean? = null,
    )

    data class ClusterHeat(
        val name: String,
        val count: Int,
        val max: Int,
        val full: Boolean,
        val held: List<String>,
    )

    data class ProtectiveHealth(
        val missingCount: Int,
        val expected: Int,
        val tracked: Int,
        val ok: Boolean,
        val fractionalNaCount: Int = 0,
        val cryptoNaCount: Int = 0,
    )

    /** Combined portfolio heat (open risk / session / DD). */
    data class PortfolioHeat(
        val openRiskDollars: Double = 0.0,
        val openRiskPct: Double = 0.0,
        val sessionRiskUsedPct: Double = 0.0,
        val ddPaused: Boolean = false,
        val ddReason: String = "",
        val peakDdWorstPct: Double = 0.0,
        val present: Boolean = false,
    )

    data class BrokerBalance(
        val equity: Double = 0.0,
        val cash: Double = 0.0,
        val dayPnl: Double = 0.0,
    )

    data class RecentTrade(
        val timestamp: String = "",
        val broker: String = "",
        val side: String = "",
        val ticker: String = "",
        val status: String = "",
    )

    data class LockedCapital(
        val total: Double = 0.0,
        val count: Int = 0,
        val byBroker: Map<String, Double> = emptyMap(),
        val countByBroker: Map<String, Int> = emptyMap(),
        val present: Boolean = false,
    )

    data class ShadowGuard(
        val status: String = "",
        val tip: String = "",
        val tighten: Boolean = false,
        val sizeMult: Double = 1.0,
        val present: Boolean = false,
    )

    data class FracPolicy(
        val preferWholeShares: Boolean = true,
        val allowTtpOnly: Boolean = true,
        val present: Boolean = false,
    )

    data class EtradeInfo(
        val environment: String = "",
        val sandboxNoBp: Boolean = false,
        val liveZeroBp: Boolean = false,
        val buyEnginesParked: Boolean = false,
        val buyingPower: Double? = null,
        val note: String = "",
        val protectiveStops: Boolean = false,
        val present: Boolean = false,
    )

    data class WalkForwardPart(
        val note: String = "",
        val oosNetSum: Double? = null,
        val oosSteps: Int? = null,
        val nTrades: Int? = null,
    )

    data class WalkForward(
        val journal: WalkForwardPart = WalkForwardPart(),
        val bar: WalkForwardPart = WalkForwardPart(),
        val present: Boolean = false,
    )

    data class SignalAlert(
        val id: String = "",
        val ticker: String = "",
        val engine: String = "",
        val score: Double = 0.0,
        val broker: String = "",
        val reason: String = "",
    )

    data class Status(
        val controlsEnabled: Boolean,
        val autoTrader: Map<String, Boolean>,
        val brokers: Map<String, BrokerInfo>,
        val banner: String,
        val mode: String,
        val market: String,
        val tls: Boolean,
        val fingerprint: String,
        val combinedEquity: Double,
        val combinedCash: Double,
        val combinedPnl: Double,
        val clusterHeat: List<ClusterHeat> = emptyList(),
        val protectiveHealth: ProtectiveHealth = ProtectiveHealth(0, 0, 0, true),
        val portfolioHeat: PortfolioHeat = PortfolioHeat(),
        val shadowGuard: ShadowGuard = ShadowGuard(),
        val fracPolicy: FracPolicy = FracPolicy(),
        val etrade: EtradeInfo = EtradeInfo(),
        val walkForward: WalkForward = WalkForward(),
        val lockedCapital: LockedCapital = LockedCapital(),
        val halted: Boolean = false,
        val signalAlert: SignalAlert? = null,
        val brokerBalances: Map<String, BrokerBalance> = emptyMap(),
        val recentTrades: List<RecentTrade> = emptyList(),
        val recentLog: List<String> = emptyList(),
        val queue: List<String> = emptyList(),
        val updatedAt: String = "",
        val app: String = "",
        val version: String = "",
    )

    data class AutoResult(val ok: Boolean, val error: String?, val lockoutSeconds: Int? = null)

    private fun authHeader(user: String, pass: String): String? {
        if (user.isBlank()) return null
        val raw = "$user:$pass"
        val b64 = Base64.encodeToString(raw.toByteArray(Charsets.UTF_8), Base64.NO_WRAP)
        return "Basic $b64"
    }

    private fun pinnedTrustManager(expectedFp: String): X509TrustManager {
        val expected = TlsPin.normalize(expectedFp)
        val defaultTm = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).run {
            init(null as KeyStore?)
            trustManagers.filterIsInstance<X509TrustManager>().first()
        }
        return object : X509TrustManager {
            override fun getAcceptedIssuers(): Array<X509Certificate> = defaultTm.acceptedIssuers

            override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
                defaultTm.checkClientTrusted(chain, authType)
            }

            override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
                if (chain.isNullOrEmpty()) throw CertificateException("Empty certificate chain")
                val leaf = chain[0]
                val got = TlsPin.normalize(TlsPin.certFingerprint(leaf))
                if (expected.isNotBlank() && got == expected) return
                try {
                    defaultTm.checkServerTrusted(chain, authType)
                } catch (e: CertificateException) {
                    if (expected.isBlank()) {
                        throw CertificateException(
                            "Untrusted TLS cert. Paste the PC fingerprint into Companion Settings.",
                            e,
                        )
                    }
                    throw CertificateException(
                        "TLS fingerprint mismatch. Expected pin from PC Settings.",
                        e,
                    )
                }
            }
        }
    }

    private fun open(url: String, user: String, pass: String, method: String, pinFp: String): HttpURLConnection {
        val conn = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 10000
            readTimeout = 10000
            setRequestProperty("Accept", "application/json")
            setRequestProperty("Cache-Control", "no-store")
            authHeader(user, pass)?.let { setRequestProperty("Authorization", it) }
        }
        if (conn is HttpsURLConnection) {
            val tm = pinnedTrustManager(pinFp)
            val ctx = SSLContext.getInstance("TLS")
            ctx.init(null, arrayOf<TrustManager>(tm), null)
            conn.sslSocketFactory = ctx.socketFactory
            conn.hostnameVerifier = HostnameVerifier { _, _ -> pinFp.isNotBlank() }
        }
        return conn
    }

    private fun readBody(conn: HttpURLConnection, code: Int): String {
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        return stream?.let { BufferedReader(InputStreamReader(it)).readText() } ?: ""
    }

    private fun parseErrorMessage(text: String, code: Int): Pair<String, Int?> {
        return try {
            val json = JSONObject(text.ifBlank { "{}" })
            val err = json.optString("error", "").takeIf { it.isNotBlank() }
            val lockout = if (json.has("lockout_seconds")) json.optInt("lockout_seconds") else null
            val msg = when {
                err != null -> err
                code == 401 -> "Unauthorized"
                code == 403 -> "Forbidden"
                else -> "HTTP $code"
            }
            msg to lockout
        } catch (_: Exception) {
            val fallback = when (code) {
                401 -> "Unauthorized"
                403 -> "Forbidden"
                else -> text.ifBlank { "HTTP $code" }
            }
            fallback to null
        }
    }

    private fun optDoubleOrNull(obj: JSONObject?, key: String): Double? {
        if (obj == null || !obj.has(key) || obj.isNull(key)) return null
        return try {
            obj.getDouble(key)
        } catch (_: Exception) {
            null
        }
    }

    private fun optIntOrNull(obj: JSONObject?, key: String): Int? {
        if (obj == null || !obj.has(key) || obj.isNull(key)) return null
        return try {
            obj.getInt(key)
        } catch (_: Exception) {
            null
        }
    }

    private fun parseWalkPart(obj: JSONObject?): WalkForwardPart {
        if (obj == null) return WalkForwardPart()
        return WalkForwardPart(
            note = obj.optString("note", ""),
            oosNetSum = optDoubleOrNull(obj, "oos_net_sum"),
            oosSteps = optIntOrNull(obj, "oos_steps"),
            nTrades = optIntOrNull(obj, "n_trades"),
        )
    }

    private fun parseSignalAlert(obj: JSONObject?): SignalAlert? {
        if (obj == null) return null
        val id = obj.optString("id", "").trim()
        if (id.isBlank()) return null
        return SignalAlert(
            id = id,
            ticker = obj.optString("ticker", ""),
            engine = obj.optString("engine", ""),
            score = obj.optDouble("score", 0.0),
            broker = obj.optString("broker", ""),
            reason = obj.optString("reason", ""),
        )
    }

    fun fetchStatus(baseUrl: String, user: String, pass: String, pinFp: String): Status {
        val conn = open("${baseUrl.trimEnd('/')}/api/status", user, pass, "GET", pinFp)
        val code = conn.responseCode
        val text = readBody(conn, code)
        if (code !in 200..299) {
            val (msg, lockout) = parseErrorMessage(text, code)
            throw MonitorApiException(msg, httpCode = code, lockoutSeconds = lockout)
        }
        val json = JSONObject(text)
        val autos = mutableMapOf<String, Boolean>()
        val autoObj = json.optJSONObject("auto_trader")
        if (autoObj != null) {
            val keys = autoObj.keys()
            while (keys.hasNext()) {
                val k = keys.next()
                autos[k] = autoObj.optBoolean(k, false)
            }
        }
        val brokers = mutableMapOf<String, BrokerInfo>()
        val brokersObj = json.optJSONObject("brokers")
        if (brokersObj != null) {
            val keys = brokersObj.keys()
            while (keys.hasNext()) {
                val k = keys.next()
                val o = brokersObj.optJSONObject(k) ?: continue
                val armed = if (o.has("armed")) {
                    o.optBoolean("armed", false)
                } else {
                    autos[k] == true
                }
                val protectiveStops = if (o.has("protective_stops") && !o.isNull("protective_stops")) {
                    o.optBoolean("protective_stops", true)
                } else {
                    null
                }
                brokers[k] = BrokerInfo(
                    connected = o.optBoolean("connected", false),
                    liveTrading = o.optBoolean("live_trading", true),
                    reauthNeeded = o.optBoolean("reauth_needed", false),
                    armed = armed,
                    ddPause = o.optBoolean("dd_pause", false),
                    ddReason = o.optString("dd_reason", ""),
                    environment = o.optString("environment", ""),
                    sandboxNoBp = o.optBoolean("sandbox_no_bp", false),
                    liveZeroBp = o.optBoolean("live_zero_bp", false),
                    buyEnginesParked = o.optBoolean("buy_engines_parked", false),
                    buyingPower = if (o.has("buying_power") && !o.isNull("buying_power")) {
                        o.optDouble("buying_power")
                    } else null,
                    protectiveStops = protectiveStops,
                )
            }
        }
        val combined = json.optJSONObject("balances")?.optJSONObject("combined")
        val clusters = mutableListOf<ClusterHeat>()
        val chArr = json.optJSONArray("cluster_heat")
        if (chArr != null) {
            for (i in 0 until chArr.length()) {
                val o = chArr.optJSONObject(i) ?: continue
                val held = mutableListOf<String>()
                val heldArr = o.optJSONArray("held")
                if (heldArr != null) {
                    for (j in 0 until heldArr.length()) {
                        val t = heldArr.optString(j, "")
                        if (t.isNotBlank()) held += t
                    }
                }
                clusters += ClusterHeat(
                    name = o.optString("name", ""),
                    count = o.optInt("count", 0),
                    max = o.optInt("max", 2),
                    full = o.optBoolean("full", false),
                    held = held,
                )
            }
        }
        val phObj = json.optJSONObject("protective_health")
        val ph = if (phObj != null) {
            ProtectiveHealth(
                missingCount = phObj.optInt("missing_count", 0),
                expected = phObj.optInt("expected", 0),
                tracked = phObj.optInt("tracked", 0),
                ok = phObj.optBoolean("ok", true),
                fractionalNaCount = phObj.optInt("fractional_na_count", 0),
                cryptoNaCount = phObj.optInt("crypto_na_count", 0),
            )
        } else {
            ProtectiveHealth(0, 0, 0, true)
        }

        val heatObj = json.optJSONObject("portfolio_heat")
        val heatCombined = heatObj?.optJSONObject("combined")
        val portfolioHeat = if (heatCombined != null) {
            PortfolioHeat(
                openRiskDollars = heatCombined.optDouble("open_risk_dollars", 0.0),
                openRiskPct = heatCombined.optDouble("open_risk_pct", 0.0),
                sessionRiskUsedPct = heatCombined.optDouble("session_risk_used_pct", 0.0),
                ddPaused = heatCombined.optBoolean("dd_paused", false),
                ddReason = heatCombined.optString("dd_reason", ""),
                peakDdWorstPct = heatCombined.optDouble("peak_dd_worst_pct", 0.0),
                present = true,
            )
        } else {
            PortfolioHeat()
        }

        val lockedObj = json.optJSONObject("locked_capital")
        val lockedCapital = if (lockedObj != null) {
            val byB = mutableMapOf<String, Double>()
            val countByB = mutableMapOf<String, Int>()
            val byBrokerObj = lockedObj.optJSONObject("by_broker")
            if (byBrokerObj != null) {
                val keys = byBrokerObj.keys()
                while (keys.hasNext()) {
                    val k = keys.next()
                    val row = byBrokerObj.optJSONObject(k)
                    val v = row?.optDouble("value", 0.0) ?: byBrokerObj.optDouble(k, 0.0)
                    val c = row?.optInt("count", 0) ?: 0
                    if (v > 0.001) byB[k] = v
                    if (c > 0) countByB[k] = c
                }
            }
            LockedCapital(
                total = lockedObj.optDouble("total", 0.0),
                count = lockedObj.optInt("count", 0),
                byBroker = byB,
                countByBroker = countByB,
                present = true,
            )
        } else {
            LockedCapital()
        }

        val sgObj = json.optJSONObject("shadow_guard")
        val shadowGuard = if (sgObj != null && sgObj.length() > 0) {
            ShadowGuard(
                status = sgObj.optString("status", ""),
                tip = sgObj.optString("tip", ""),
                tighten = sgObj.optBoolean("tighten", false),
                sizeMult = sgObj.optDouble("size_mult", 1.0),
                present = true,
            )
        } else {
            ShadowGuard()
        }

        val fpObj = json.optJSONObject("frac_policy")
        val fracPolicy = if (fpObj != null) {
            FracPolicy(
                preferWholeShares = fpObj.optBoolean("prefer_whole_shares", true),
                allowTtpOnly = fpObj.optBoolean("allow_ttp_only", true),
                present = true,
            )
        } else {
            FracPolicy()
        }

        val etObj = json.optJSONObject("etrade")
        val etrade = if (etObj != null) {
            EtradeInfo(
                environment = etObj.optString("environment", ""),
                sandboxNoBp = etObj.optBoolean("sandbox_no_bp", false),
                liveZeroBp = etObj.optBoolean("live_zero_bp", false),
                buyEnginesParked = etObj.optBoolean("buy_engines_parked", false),
                buyingPower = if (etObj.has("buying_power") && !etObj.isNull("buying_power")) {
                    etObj.optDouble("buying_power")
                } else null,
                note = etObj.optString("note", ""),
                protectiveStops = etObj.optBoolean("protective_stops", false),
                present = true,
            )
        } else {
            EtradeInfo()
        }

        val wfObj = json.optJSONObject("walk_forward")
        val walkForward = if (wfObj != null) {
            WalkForward(
                journal = parseWalkPart(wfObj.optJSONObject("journal")),
                bar = parseWalkPart(wfObj.optJSONObject("bar")),
                present = true,
            )
        } else {
            WalkForward()
        }

        val brokerBalances = mutableMapOf<String, BrokerBalance>()
        val balRoot = json.optJSONObject("balances")
        if (balRoot != null) {
            val balKeys = balRoot.keys()
            while (balKeys.hasNext()) {
                val k = balKeys.next()
                if (k == "combined") continue
                val o = balRoot.optJSONObject(k) ?: continue
                brokerBalances[k] = BrokerBalance(
                    equity = o.optDouble("equity", 0.0),
                    cash = o.optDouble("cash", 0.0),
                    dayPnl = o.optDouble("day_pnl", 0.0),
                )
            }
        }

        val recentTrades = mutableListOf<RecentTrade>()
        val tradesArr = json.optJSONArray("recent_trades")
        if (tradesArr != null) {
            for (i in 0 until tradesArr.length()) {
                val o = tradesArr.optJSONObject(i) ?: continue
                recentTrades += RecentTrade(
                    timestamp = o.optString("timestamp", ""),
                    broker = o.optString("broker", ""),
                    side = o.optString("side", ""),
                    ticker = o.optString("ticker", ""),
                    status = o.optString("status", ""),
                )
            }
        }

        val recentLog = mutableListOf<String>()
        val logArr = json.optJSONArray("recent_log")
        if (logArr != null) {
            for (i in 0 until logArr.length()) {
                val line = logArr.optString(i, "").trim()
                if (line.isNotBlank()) recentLog += line
            }
        }

        val queue = mutableListOf<String>()
        val queueArr = json.optJSONArray("queue")
        if (queueArr != null) {
            for (i in 0 until queueArr.length()) {
                val q = queueArr.optString(i, "").trim()
                if (q.isNotBlank()) queue += q
            }
        }

        return Status(
            controlsEnabled = json.optBoolean("controls_enabled", false),
            autoTrader = autos,
            brokers = brokers,
            banner = json.optString("banner", ""),
            mode = json.optString("mode", ""),
            market = json.optString("market", ""),
            tls = json.optBoolean("tls", false),
            fingerprint = json.optString("cert_fingerprint", ""),
            combinedEquity = combined?.optDouble("equity", 0.0) ?: 0.0,
            combinedCash = combined?.optDouble("cash", 0.0) ?: 0.0,
            combinedPnl = combined?.optDouble("day_pnl", 0.0) ?: 0.0,
            clusterHeat = clusters,
            protectiveHealth = ph,
            portfolioHeat = portfolioHeat,
            shadowGuard = shadowGuard,
            fracPolicy = fracPolicy,
            etrade = etrade,
            walkForward = walkForward,
            lockedCapital = lockedCapital,
            halted = json.optBoolean("halted", false),
            signalAlert = parseSignalAlert(json.optJSONObject("signal_alert")),
            brokerBalances = brokerBalances,
            recentTrades = recentTrades,
            recentLog = recentLog,
            queue = queue,
            updatedAt = json.optString("updated_at", ""),
            app = json.optString("app", ""),
            version = json.optString("version", ""),
        )
    }

    fun setArmed(
        baseUrl: String,
        user: String,
        pass: String,
        pinFp: String,
        broker: String,
        armed: Boolean,
    ): AutoResult {
        val conn = open("${baseUrl.trimEnd('/')}/api/auto", user, pass, "POST", pinFp)
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        val body = JSONObject()
            .put("broker", broker)
            .put("armed", armed)
            .toString()
        OutputStreamWriter(conn.outputStream, Charsets.UTF_8).use { it.write(body) }
        val code = conn.responseCode
        val text = readBody(conn, code)
        return try {
            val json = JSONObject(text.ifBlank { "{}" })
            val err = json.optString("error", "")
            val lockout = if (json.has("lockout_seconds")) json.optInt("lockout_seconds") else null
            AutoResult(
                ok = json.optBoolean("ok", code in 200..299),
                error = err.takeIf { it.isNotBlank() } ?: if (code !in 200..299) {
                    parseErrorMessage(text, code).first
                } else {
                    null
                },
                lockoutSeconds = lockout,
            )
        } catch (_: Exception) {
            val (msg, lockout) = parseErrorMessage(text, code)
            AutoResult(ok = code in 200..299, error = msg, lockoutSeconds = lockout)
        }
    }

    fun haltAll(
        baseUrl: String,
        user: String,
        pass: String,
        pinFp: String,
    ): AutoResult {
        val conn = open("${baseUrl.trimEnd('/')}/api/halt", user, pass, "POST", pinFp)
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        OutputStreamWriter(conn.outputStream, Charsets.UTF_8).use { it.write("{}") }
        val code = conn.responseCode
        val text = readBody(conn, code)
        return try {
            val json = JSONObject(text.ifBlank { "{}" })
            val err = json.optString("error", "")
            AutoResult(
                ok = json.optBoolean("ok", code in 200..299),
                error = err.takeIf { it.isNotBlank() } ?: if (code !in 200..299) {
                    parseErrorMessage(text, code).first
                } else {
                    null
                },
            )
        } catch (_: Exception) {
            val (msg, lockout) = parseErrorMessage(text, code)
            AutoResult(ok = code in 200..299, error = msg, lockoutSeconds = lockout)
        }
    }

    data class OAuthStartResult(
        val ok: Boolean,
        val authorizeUrl: String? = null,
        val error: String? = null,
    )

    data class OAuthCompleteResult(
        val ok: Boolean,
        val armed: Boolean = false,
        val error: String? = null,
    )

    fun etradeOauthStart(
        baseUrl: String,
        user: String,
        pass: String,
        pinFp: String,
    ): OAuthStartResult {
        val conn = open("${baseUrl.trimEnd('/')}/api/etrade/oauth/start", user, pass, "POST", pinFp)
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        OutputStreamWriter(conn.outputStream, Charsets.UTF_8).use { it.write("{}") }
        val code = conn.responseCode
        val text = readBody(conn, code)
        return try {
            val json = JSONObject(text.ifBlank { "{}" })
            OAuthStartResult(
                ok = json.optBoolean("ok", code in 200..299),
                authorizeUrl = json.optString("authorize_url", "").takeIf { it.isNotBlank() },
                error = json.optString("error", "").takeIf { it.isNotBlank() }
                    ?: if (code !in 200..299) parseErrorMessage(text, code).first else null,
            )
        } catch (_: Exception) {
            val (msg, _) = parseErrorMessage(text, code)
            OAuthStartResult(ok = false, error = msg)
        }
    }

    fun etradeOauthComplete(
        baseUrl: String,
        user: String,
        pass: String,
        pinFp: String,
        verifier: String,
    ): OAuthCompleteResult {
        val conn = open("${baseUrl.trimEnd('/')}/api/etrade/oauth/complete", user, pass, "POST", pinFp)
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        val body = JSONObject().put("verifier", verifier).toString()
        OutputStreamWriter(conn.outputStream, Charsets.UTF_8).use { it.write(body) }
        val code = conn.responseCode
        val text = readBody(conn, code)
        return try {
            val json = JSONObject(text.ifBlank { "{}" })
            OAuthCompleteResult(
                ok = json.optBoolean("ok", code in 200..299),
                armed = json.optBoolean("armed", false),
                error = json.optString("error", "").takeIf { it.isNotBlank() }
                    ?: if (code !in 200..299) parseErrorMessage(text, code).first else null,
            )
        } catch (_: Exception) {
            val (msg, _) = parseErrorMessage(text, code)
            OAuthCompleteResult(ok = false, error = msg)
        }
    }
}
