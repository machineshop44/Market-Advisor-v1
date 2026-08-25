package com.marketadvisor.companion

import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.net.http.SslError
import android.os.Bundle
import android.view.View
import android.webkit.HttpAuthHandler
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.switchmaterial.SwitchMaterial
import com.marketadvisor.companion.databinding.ActivityMainBinding
import com.marketadvisor.companion.databinding.ItemBrokerArmBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.NumberFormat
import java.util.Locale

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private var pollJob: Job? = null
    private var refreshJob: Job? = null
    private var syncingUi = false
    private var lastStatusOk = false
    private val moneyFmt: NumberFormat = NumberFormat.getCurrencyInstance(Locale.US)

    private data class BrokerRow(
        val name: String,
        val binding: ItemBrokerArmBinding,
    )

    private lateinit var brokerRows: List<BrokerRow>

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val versionName = try {
            packageManager.getPackageInfo(packageName, 0).versionName ?: "—"
        } catch (_: PackageManager.NameNotFoundException) {
            "—"
        }
        binding.versionLabel.text = getString(R.string.version_label, versionName) +
            " · " + getString(R.string.compat_note)

        brokerRows = listOf(
            BrokerRow("Robinhood", binding.rowRobinhood),
            BrokerRow("Coinbase", binding.rowCoinbase),
            BrokerRow("E*TRADE", binding.rowEtrade),
        )
        for (row in brokerRows) {
            row.binding.brokerName.text = row.name
            row.binding.brokerSwitch.setOnCheckedChangeListener { _, checked ->
                if (syncingUi) return@setOnCheckedChangeListener
                confirmAuto(row.name, checked, row.binding.brokerSwitch)
            }
        }

        binding.webView.settings.javaScriptEnabled = true
        binding.webView.settings.domStorageEnabled = true
        // Tablet readability: monitor WebView was tiny after metrics/controls stacked above.
        binding.webView.settings.textZoom = 140
        binding.webView.settings.loadWithOverviewMode = true
        binding.webView.settings.useWideViewPort = true
        binding.webView.webChromeClient = WebChromeClient()
        binding.webView.webViewClient = object : WebViewClient() {
            override fun onReceivedHttpAuthRequest(
                view: WebView?,
                handler: HttpAuthHandler?,
                host: String?,
                realm: String?,
            ) {
                handler?.proceed(Prefs.username(this@MainActivity), Prefs.password(this@MainActivity))
            }

            override fun onReceivedSslError(view: WebView?, handler: SslErrorHandler?, error: SslError?) {
                val pin = Prefs.fingerprint(this@MainActivity)
                val got = TlsPin.fingerprintFromSslCertificate(error?.certificate)
                if (pin.isNotBlank() && got != null && TlsPin.matches(pin, got)) {
                    handler?.proceed()
                } else {
                    handler?.cancel()
                    val msg = if (pin.isBlank()) {
                        getString(R.string.tls_pin_required)
                    } else {
                        getString(R.string.tls_pin_mismatch)
                    }
                    Toast.makeText(this@MainActivity, msg, Toast.LENGTH_LONG).show()
                }
            }

            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                return false
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                binding.swipe.isRefreshing = false
            }
        }

        binding.btnSettings.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        binding.btnHaltAll.setOnClickListener { confirmHaltAll() }
        binding.btnHaltAll.isEnabled = false
        binding.btnScanSetupQr.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        binding.btnOpenMonitor.setOnClickListener { loadMonitor() }
        binding.swipe.setOnRefreshListener {
            loadMonitor()
            refreshStatus()
        }

        updateSetupOverlay()
        if (!needsSetup()) {
            loadMonitor()
        }
    }

    override fun onResume() {
        super.onResume()
        updateSetupOverlay()
        if (!needsSetup()) {
            loadMonitor()
            startPolling()
        } else {
            pollJob?.cancel()
            pollJob = null
        }
    }

    override fun onPause() {
        super.onPause()
        pollJob?.cancel()
        pollJob = null
    }

    private fun needsSetup(): Boolean {
        val url = Prefs.baseUrl(this)
        val fp = Prefs.fingerprint(this)
        if (fp.isBlank()) return true
        return try {
            val host = Uri.parse(url).host?.lowercase(Locale.US).orEmpty()
            host.isBlank() || host == "127.0.0.1" || host == "localhost" || host == "::1"
        } catch (_: Exception) {
            true
        }
    }

    private fun updateSetupOverlay() {
        val show = needsSetup()
        binding.setupOverlay.visibility = if (show) View.VISIBLE else View.GONE
    }

    private fun loadMonitor() {
        if (needsSetup()) return
        val url = Prefs.baseUrl(this)
        binding.swipe.isRefreshing = true
        binding.webView.loadUrl(url)
    }

    private fun startPolling() {
        pollJob?.cancel()
        pollJob = lifecycleScope.launch {
            while (isActive) {
                val inflight = refreshJob
                if (inflight != null && inflight.isActive) {
                    inflight.join()
                } else {
                    refreshStatus()
                    refreshJob?.join()
                }
                delay(3000)
            }
        }
    }

    private fun money(n: Double): String = moneyFmt.format(n)

    private fun setPill(view: TextView, armed: Boolean) {
        view.text = if (armed) "ON" else "OFF"
        view.setBackgroundResource(if (armed) R.drawable.bg_pill_on else R.drawable.bg_pill_off)
        view.setTextColor(
            ContextCompat.getColor(this, if (armed) R.color.ok else R.color.muted),
        )
    }

    private fun markMetricsStale() {
        fun stamp(tv: TextView) {
            val t = tv.text?.toString().orEmpty()
            if (t.isBlank() || t == "—") {
                tv.text = "—"
            } else if (!t.contains("stale", ignoreCase = true)) {
                tv.text = "$t (stale)"
            }
        }
        stamp(binding.metricEquity)
        stamp(binding.metricCash)
        stamp(binding.metricPnl)
        stamp(binding.metricStops)
        stamp(binding.metricClusters)
        stamp(binding.metricRisk)
        stamp(binding.metricShadow)
        stamp(binding.policyLine)
        stamp(binding.walkForwardLine)
    }

    private fun brokerSubtitle(name: String, info: MonitorApi.BrokerInfo?, etrade: MonitorApi.EtradeInfo): String {
        if (info == null && name != "E*TRADE") return ""
        val bits = mutableListOf<String>()
        if (info != null) {
            bits += when {
                info.reauthNeeded -> "Reauth needed"
                info.connected -> "Connected"
                else -> "Disconnected"
            }
            if (info.ddPause) bits += "DD pause"
            if (!info.liveTrading) bits += "live trading off"
            info.buyingPower?.let { bp ->
                if (name != "E*TRADE") {
                    bits += String.format(Locale.US, "BP $%,.0f", bp)
                }
            }
        }
        if (name == "E*TRADE") {
            when {
                info?.reauthNeeded == true -> { /* already in bits */ }
                info?.sandboxNoBp == true || etrade.sandboxNoBp -> bits += "Sandbox/no BP"
                info?.liveZeroBp == true || etrade.liveZeroBp -> bits += "Live/$0 BP"
                info?.buyEnginesParked == true || etrade.buyEnginesParked -> bits += "buys parked"
                !info?.environment.isNullOrBlank() -> bits += info!!.environment
                etrade.environment.isNotBlank() -> bits += etrade.environment
            }
            val bp = info?.buyingPower ?: etrade.buyingPower
            if (bp != null) {
                bits += String.format(Locale.US, "BP $%,.2f", bp)
            }
            bits += "stops N/A"
        }
        return bits.joinToString(" · ")
    }

    private fun formatWalkPart(label: String, part: MonitorApi.WalkForwardPart): String? {
        val note = part.note.trim()
        if (note.isNotBlank()) {
            val short = if (note.length > 48) note.take(45) + "…" else note
            return "$label $short"
        }
        if (part.oosSteps != null) {
            val net = part.oosNetSum?.let { String.format(Locale.US, "%.2f", it) } ?: "—"
            return "$label ${part.oosSteps} OOS · net $net"
        }
        if (part.nTrades != null) {
            val net = part.oosNetSum?.let { String.format(Locale.US, "%.2f", it) } ?: "—"
            return "$label ${part.nTrades} trades · OOS $net"
        }
        return null
    }

    /** Coalesced: one in-flight refresh; overlaps are skipped. */
    private fun refreshStatus() {
        if (refreshJob?.isActive == true) return
        refreshJob = lifecycleScope.launch {
            val url = Prefs.baseUrl(this@MainActivity)
            val user = Prefs.username(this@MainActivity)
            val pass = Prefs.password(this@MainActivity)
            val pin = Prefs.fingerprint(this@MainActivity)
            try {
                val status = withContext(Dispatchers.IO) {
                    MonitorApi.fetchStatus(url, user, pass, pin)
                }
                lastStatusOk = true
                val tlsBit = if (status.tls) "HTTPS" else "HTTP"
                val controlsBit = if (status.controlsEnabled) "controls on" else "read-only"
                val haltBit = if (status.halted) " · HALTED" else ""
                binding.statusLine.text =
                    "$tlsBit · ${status.mode.ifBlank { "—" }} · ${status.market.ifBlank { "—" }} · $controlsBit$haltBit"
                binding.bannerLine.text = status.banner.ifBlank { "Waiting for desktop…" }
                binding.metricEquity.text = money(status.combinedEquity)
                binding.metricCash.text = money(status.combinedCash)
                binding.metricPnl.text = money(status.combinedPnl)
                binding.metricPnl.setTextColor(
                    ContextCompat.getColor(
                        this@MainActivity,
                        when {
                            status.combinedPnl > 0.001 -> R.color.ok
                            status.combinedPnl < -0.001 -> R.color.danger
                            else -> R.color.text
                        },
                    ),
                )

                val ph = status.protectiveHealth
                val stopParts = mutableListOf<String>()
                if (ph.expected > 0) {
                    stopParts += if (ph.missingCount <= 0) {
                        "OK · ${ph.tracked}/${ph.expected}"
                    } else {
                        "Missing ${ph.missingCount} · ${ph.tracked}/${ph.expected}"
                    }
                }
                if (ph.fractionalNaCount > 0) {
                    stopParts += "frac N/A ${ph.fractionalNaCount}"
                }
                binding.metricStops.text = stopParts.joinToString(" · ").ifBlank { "—" }
                binding.metricStops.setTextColor(
                    ContextCompat.getColor(
                        this@MainActivity,
                        when {
                            ph.missingCount > 0 -> R.color.danger
                            ph.fractionalNaCount > 0 -> R.color.warn
                            ph.expected > 0 -> R.color.ok
                            else -> R.color.muted
                        },
                    ),
                )

                val hot = status.clusterHeat.filter { it.count > 0 || it.full }
                binding.metricClusters.text = if (hot.isEmpty()) {
                    "No cluster load"
                } else {
                    hot.take(3).joinToString(" · ") { c ->
                        val tag = if (c.full) "FULL" else "${c.count}/${c.max}"
                        "${c.name} $tag"
                    }
                }
                binding.metricClusters.setTextColor(
                    ContextCompat.getColor(
                        this@MainActivity,
                        if (hot.any { it.full }) R.color.danger else R.color.text,
                    ),
                )

                val heat = status.portfolioHeat
                if (heat.present) {
                    val riskBits = mutableListOf<String>()
                    riskBits += String.format(
                        Locale.US,
                        "%s (%.1f%%)",
                        money(heat.openRiskDollars),
                        heat.openRiskPct,
                    )
                    riskBits += String.format(Locale.US, "sess %.0f%%", heat.sessionRiskUsedPct)
                    if (heat.ddPaused) riskBits += "DD"
                    if (status.halted) riskBits += "HALTED"
                    binding.metricRisk.text = riskBits.joinToString(" · ")
                    binding.metricRisk.setTextColor(
                        ContextCompat.getColor(
                            this@MainActivity,
                            when {
                                status.halted || heat.ddPaused -> R.color.danger
                                heat.sessionRiskUsedPct >= 80.0 -> R.color.warn
                                else -> R.color.text
                            },
                        ),
                    )
                } else {
                    binding.metricRisk.text = if (status.halted) "HALTED" else "—"
                    binding.metricRisk.setTextColor(
                        ContextCompat.getColor(
                            this@MainActivity,
                            if (status.halted) R.color.danger else R.color.muted,
                        ),
                    )
                }

                val sg = status.shadowGuard
                if (sg.present) {
                    val sgBits = mutableListOf<String>()
                    val st = sg.status.ifBlank { "—" }
                    sgBits += st
                    if (sg.tighten) {
                        sgBits += String.format(Locale.US, "size×%.2f", sg.sizeMult)
                    }
                    binding.metricShadow.text = sgBits.joinToString(" · ")
                    binding.metricShadow.setTextColor(
                        ContextCompat.getColor(
                            this@MainActivity,
                            when {
                                sg.tighten -> R.color.warn
                                sg.status.equals("ok", ignoreCase = true) -> R.color.ok
                                else -> R.color.text
                            },
                        ),
                    )
                } else {
                    binding.metricShadow.text = "—"
                    binding.metricShadow.setTextColor(
                        ContextCompat.getColor(this@MainActivity, R.color.muted),
                    )
                }

                val policyBits = mutableListOf<String>()
                if (status.fracPolicy.present) {
                    val fp = status.fracPolicy
                    policyBits += "Frac: whole=${if (fp.preferWholeShares) "yes" else "no"} · TTP-only=${if (fp.allowTtpOnly) "yes" else "no"}"
                }
                if (status.etrade.present) {
                    val et = status.etrade
                    when {
                        et.sandboxNoBp -> policyBits += "ET sandbox/no BP"
                        et.liveZeroBp -> policyBits += "ET live/${'$'}0 BP · buys parked"
                        et.buyEnginesParked -> policyBits += "ET buys parked"
                        et.note.isNotBlank() -> {
                            val short = if (et.note.length > 64) et.note.take(61) + "…" else et.note
                            policyBits += short
                        }
                        et.environment.isNotBlank() -> policyBits += "ET ${et.environment}"
                    }
                    et.buyingPower?.let { bp ->
                        policyBits += String.format(Locale.US, "ET BP $%,.2f", bp)
                    }
                }
                binding.policyLine.text = policyBits.joinToString(" · ").ifBlank { "—" }
                binding.policyLine.setTextColor(
                    ContextCompat.getColor(
                        this@MainActivity,
                        if (status.etrade.sandboxNoBp || status.etrade.liveZeroBp || status.etrade.buyEnginesParked)
                            R.color.warn else R.color.muted,
                    ),
                )

                val wf = status.walkForward
                if (wf.present) {
                    val parts = listOfNotNull(
                        formatWalkPart("Journal", wf.journal),
                        formatWalkPart("Bar", wf.bar),
                    )
                    binding.walkForwardLine.text = parts.joinToString(" · ").ifBlank { "Walk-forward: —" }
                } else {
                    binding.walkForwardLine.text = "—"
                }

                binding.controlsHint.setText(
                    if (status.controlsEnabled) {
                        R.string.controls_hint
                    } else {
                        R.string.controls_hint_readonly
                    },
                )
                binding.btnHaltAll.isEnabled = status.controlsEnabled

                syncingUi = true
                for (row in brokerRows) {
                    val info = status.brokers[row.name]
                    val armed = info?.armed ?: (status.autoTrader[row.name] == true)
                    // Controls off → disabled. Controls on → disable if reauth or disconnected.
                    val switchEnabled = status.controlsEnabled &&
                        (info == null || (!info.reauthNeeded && info.connected))
                    row.binding.brokerSwitch.isEnabled = switchEnabled
                    if (row.binding.brokerSwitch.isChecked != armed) {
                        row.binding.brokerSwitch.isChecked = armed
                    }
                    setPill(row.binding.brokerPill, armed)
                    val detail = brokerSubtitle(row.name, info, status.etrade)
                    if (detail.isNotBlank()) {
                        row.binding.brokerDetail.visibility = View.VISIBLE
                        row.binding.brokerDetail.text = detail
                    } else {
                        row.binding.brokerDetail.visibility = View.GONE
                    }
                }
                syncingUi = false
            } catch (e: Exception) {
                lastStatusOk = false
                val detail = when (e) {
                    is MonitorApiException -> {
                        val lock = e.lockoutSeconds
                        if (lock != null && lock > 0) {
                            "${e.message} (lockout ${lock}s)"
                        } else {
                            e.message ?: e.javaClass.simpleName
                        }
                    }
                    else -> e.message ?: e.javaClass.simpleName
                }
                binding.statusLine.text = "Offline / stale: $detail"
                binding.bannerLine.text = "Monitor offline or app not publishing yet"
                binding.controlsHint.setText(R.string.controls_hint_offline)
                markMetricsStale()
                syncingUi = true
                for (row in brokerRows) {
                    row.binding.brokerSwitch.isEnabled = false
                }
                binding.btnHaltAll.isEnabled = false
                syncingUi = false
            } finally {
                binding.swipe.isRefreshing = false
            }
        }
    }

    private fun confirmHaltAll() {
        AlertDialog.Builder(this)
            .setTitle(R.string.halt_all)
            .setMessage(R.string.confirm_halt)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(android.R.string.ok) { _, _ -> postHaltAll() }
            .show()
    }

    private fun postHaltAll() {
        lifecycleScope.launch {
            val url = Prefs.baseUrl(this@MainActivity)
            val user = Prefs.username(this@MainActivity)
            val pass = Prefs.password(this@MainActivity)
            val pin = Prefs.fingerprint(this@MainActivity)
            val result = withContext(Dispatchers.IO) {
                runCatching { MonitorApi.haltAll(url, user, pass, pin) }
                    .getOrElse { MonitorApi.AutoResult(false, it.message) }
            }
            if (result.ok) {
                Toast.makeText(this@MainActivity, "Halted all brokers", Toast.LENGTH_SHORT).show()
                refreshStatus()
            } else {
                Toast.makeText(
                    this@MainActivity,
                    result.error ?: "Halt failed",
                    Toast.LENGTH_LONG,
                ).show()
            }
        }
    }

    private fun confirmAuto(broker: String, armed: Boolean, switch: SwitchMaterial) {
        val msg = getString(if (armed) R.string.confirm_arm else R.string.confirm_disarm, broker)
        AlertDialog.Builder(this)
            .setTitle(if (armed) R.string.arm else R.string.disarm)
            .setMessage(msg)
            .setNegativeButton(android.R.string.cancel) { _, _ ->
                syncingUi = true
                switch.isChecked = !armed
                syncingUi = false
            }
            .setPositiveButton(android.R.string.ok) { _, _ -> postAuto(broker, armed) }
            .setOnCancelListener {
                syncingUi = true
                switch.isChecked = !armed
                syncingUi = false
            }
            .show()
    }

    private fun postAuto(broker: String, armed: Boolean) {
        lifecycleScope.launch {
            val url = Prefs.baseUrl(this@MainActivity)
            val user = Prefs.username(this@MainActivity)
            val pass = Prefs.password(this@MainActivity)
            val pin = Prefs.fingerprint(this@MainActivity)
            val result = withContext(Dispatchers.IO) {
                runCatching { MonitorApi.setArmed(url, user, pass, pin, broker, armed) }
                    .getOrElse { MonitorApi.AutoResult(false, it.message) }
            }
            if (result.ok) {
                Toast.makeText(
                    this@MainActivity,
                    if (armed) "Armed $broker" else "Disarmed $broker",
                    Toast.LENGTH_SHORT,
                ).show()
                refreshStatus()
                loadMonitor()
            } else {
                val err = buildString {
                    append(result.error ?: "Request failed")
                    result.lockoutSeconds?.takeIf { it > 0 }?.let { append(" (lockout ${it}s)") }
                }
                Toast.makeText(this@MainActivity, err, Toast.LENGTH_LONG).show()
                refreshStatus()
            }
        }
    }
}
