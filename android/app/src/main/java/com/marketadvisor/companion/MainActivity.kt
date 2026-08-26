package com.marketadvisor.companion

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
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
    /** Bumped on local arm/disarm/halt so stale in-flight polls cannot paint old ON state. */
    private var statusEpoch = 0L
    /** Broker whose confirm dialog is open — poll must not overwrite that switch mid-tap. */
    private var dialogBroker: String? = null
    private val moneyFmt: NumberFormat = NumberFormat.getCurrencyInstance(Locale.US)

    private data class BrokerRow(
        val name: String,
        val binding: ItemBrokerArmBinding,
    )

    private lateinit var brokerRows: List<BrokerRow>

    private val notifyPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { /* optional — background poll still works without toast spam */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        ReauthNotifier.ensureChannel(this)
        if (Build.VERSION.SDK_INT >= 33) {
            val granted = ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.POST_NOTIFICATIONS,
            ) == PackageManager.PERMISSION_GRANTED
            if (!granted) {
                notifyPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }

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
            row.binding.btnReauth.setOnClickListener {
                if (row.name == "E*TRADE") startEtradeReauth()
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
        binding.swipe.setOnRefreshListener { refreshStatus(force = true) }

        updateSetupOverlay()
    }

    override fun onResume() {
        super.onResume()
        updateSetupOverlay()
        if (!needsSetup()) {
            ReauthPollWorker.schedule(this)
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

    private fun pnlColor(n: Double): Int = ContextCompat.getColor(
        this,
        when {
            n > 0.001 -> R.color.ok
            n < -0.001 -> R.color.danger
            else -> R.color.text
        },
    )

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
        stamp(binding.recentTradesLine)
        stamp(binding.activityLogLine)
    }

    private fun brokerBalanceLine(bal: MonitorApi.BrokerBalance?): String {
        if (bal == null) return ""
        return String.format(
            Locale.US,
            "%s equity · %s cash · %s day",
            money(bal.equity),
            money(bal.cash),
            money(bal.dayPnl),
        )
    }

    private fun brokerSubtitle(
        name: String,
        info: MonitorApi.BrokerInfo?,
        etrade: MonitorApi.EtradeInfo,
        locked: MonitorApi.LockedCapital,
    ): String {
        if (info == null && name != "E*TRADE") return ""
        val bits = mutableListOf<String>()
        if (info != null) {
            bits += when {
                info.reauthNeeded -> "Reauth needed"
                info.connected -> "Connected"
                else -> "Disconnected"
            }
            if (info.ddPause) {
                val ddBit = if (info.ddReason.isNotBlank()) {
                    val short = if (info.ddReason.length > 28) {
                        info.ddReason.take(25) + "…"
                    } else {
                        info.ddReason
                    }
                    "DD pause · $short"
                } else {
                    "DD pause"
                }
                bits += ddBit
            }
            val lk = locked.byBroker[name]
            val lkCnt = locked.countByBroker[name]
            if (lk != null && lk > 0.01) {
                bits += if (lkCnt != null && lkCnt > 0) {
                    String.format(Locale.US, "locked $%,.0f (%d)", lk, lkCnt)
                } else {
                    String.format(Locale.US, "locked $%,.0f", lk)
                }
            }
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

    private fun formatRecentTrades(trades: List<MonitorApi.RecentTrade>): String {
        if (trades.isEmpty()) return "No recent trades"
        return trades.takeLast(8).joinToString("\n") { t ->
            val ts = t.timestamp.takeLast(8).ifBlank { "—" }
            val side = t.side.ifBlank { "?" }
            val tick = t.ticker.ifBlank { "?" }
            val st = t.status.ifBlank { "—" }
            "$ts  ${t.broker}  $side $tick  $st"
        }
    }

    private fun formatActivityLog(lines: List<String>): String {
        if (lines.isEmpty()) return "Waiting for desktop activity…"
        return lines.takeLast(18).joinToString("\n")
    }

    /**
     * Fetch monitor status. [force] cancels an in-flight poll and always starts a new one
     * (needed after arm/disarm/halt so stale ON state cannot win the race).
     */
    private fun refreshStatus(force: Boolean = false) {
        if (!force && refreshJob?.isActive == true) return
        val prior = refreshJob
        val launchEpoch = statusEpoch
        refreshJob = lifecycleScope.launch {
            if (force && prior != null && prior.isActive) {
                prior.cancel()
                runCatching { prior.join() }
            }
            val url = Prefs.baseUrl(this@MainActivity)
            val user = Prefs.username(this@MainActivity)
            val pass = Prefs.password(this@MainActivity)
            val pin = Prefs.fingerprint(this@MainActivity)
            try {
                val status = withContext(Dispatchers.IO) {
                    MonitorApi.fetchStatus(url, user, pass, pin)
                }
                // Stale: a newer local control happened while this request was in flight.
                if (launchEpoch != statusEpoch) return@launch
                val tlsBit = if (status.tls) "HTTPS" else "HTTP"
                val controlsBit = if (status.controlsEnabled) "controls on" else "read-only"
                val haltBit = if (status.halted) " · HALTED" else ""
                val updatedBit = if (status.updatedAt.isNotBlank()) " · ${status.updatedAt}" else ""
                binding.statusLine.text =
                    "$tlsBit · ${status.mode.ifBlank { "—" }} · ${status.market.ifBlank { "—" }} · $controlsBit$haltBit$updatedBit"
                binding.bannerLine.text = status.banner.ifBlank { "Waiting for desktop…" }
                binding.metricEquity.text = money(status.combinedEquity)
                binding.metricCash.text = money(status.combinedCash)
                binding.metricPnl.text = money(status.combinedPnl)
                binding.metricPnl.setTextColor(pnlColor(status.combinedPnl))

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
                    if (heat.peakDdWorstPct < -0.001) {
                        riskBits += String.format(Locale.US, "peak %.1f%%", heat.peakDdWorstPct * 100.0)
                    }
                    val locked = status.lockedCapital
                    if (locked.present && locked.total > 0.01) {
                        val lockedTxt = if (locked.count > 0) {
                            String.format(Locale.US, "locked $%,.0f (%d)", locked.total, locked.count)
                        } else {
                            String.format(Locale.US, "locked $%,.0f", locked.total)
                        }
                        riskBits += lockedTxt
                    }
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

                if (status.queue.isNotEmpty()) {
                    binding.queueLine.visibility = View.VISIBLE
                    binding.queueLine.text = getString(R.string.queue_prefix) + " " +
                        status.queue.take(6).joinToString(" · ")
                } else {
                    binding.queueLine.visibility = View.GONE
                }

                binding.recentTradesLine.text = formatRecentTrades(status.recentTrades)
                binding.activityLogLine.text = formatActivityLog(status.recentLog)

                binding.controlsHint.setText(
                    if (status.controlsEnabled) {
                        R.string.controls_hint
                    } else {
                        R.string.controls_hint_readonly
                    },
                )
                binding.btnHaltAll.isEnabled = status.controlsEnabled

                val etNeedReauth = status.brokers["E*TRADE"]?.reauthNeeded == true
                ReauthNotifier.maybeNotifyFromStatus(
                    this@MainActivity,
                    reauthNeeded = etNeedReauth,
                    ddPaused = status.portfolioHeat.ddPaused,
                    halted = status.halted,
                )

                syncingUi = true
                for (row in brokerRows) {
                    val info = status.brokers[row.name]
                    val armed = info?.armed ?: (status.autoTrader[row.name] == true)
                    val switchEnabled = status.controlsEnabled &&
                        (info == null || (!info.reauthNeeded && info.connected))
                    row.binding.brokerSwitch.isEnabled = switchEnabled
                    // Don't yank the switch while the user is confirming arm/disarm.
                    if (dialogBroker != row.name) {
                        if (row.binding.brokerSwitch.isChecked != armed) {
                            row.binding.brokerSwitch.isChecked = armed
                        }
                        setPill(row.binding.brokerPill, armed)
                    }

                    val showReauth = row.name == "E*TRADE" &&
                        status.controlsEnabled &&
                        (info?.reauthNeeded == true)
                    row.binding.btnReauth.visibility = if (showReauth) View.VISIBLE else View.GONE

                    val balLine = brokerBalanceLine(status.brokerBalances[row.name])
                    if (balLine.isNotBlank()) {
                        row.binding.brokerBalance.visibility = View.VISIBLE
                        row.binding.brokerBalance.text = balLine
                        row.binding.brokerBalance.setTextColor(
                            ContextCompat.getColor(this@MainActivity, R.color.text),
                        )
                    } else {
                        row.binding.brokerBalance.visibility = View.GONE
                    }

                    val detail = brokerSubtitle(row.name, info, status.etrade, status.lockedCapital)
                    if (detail.isNotBlank()) {
                        row.binding.brokerDetail.visibility = View.VISIBLE
                        row.binding.brokerDetail.text = detail
                    } else {
                        row.binding.brokerDetail.visibility = View.GONE
                    }
                }
                syncingUi = false
            } catch (e: Exception) {
                if (launchEpoch != statusEpoch) return@launch
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
                binding.bannerLine.text = "Desktop offline or not publishing yet"
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

    /** Paint switches/pills immediately after a successful control (before next poll). */
    private fun applyArmedOptimistic(armedByBroker: Map<String, Boolean>) {
        statusEpoch += 1
        syncingUi = true
        for (row in brokerRows) {
            val armed = armedByBroker[row.name] ?: continue
            if (row.binding.brokerSwitch.isChecked != armed) {
                row.binding.brokerSwitch.isChecked = armed
            }
            setPill(row.binding.brokerPill, armed)
        }
        syncingUi = false
    }

    private fun confirmHaltAll() {
        AlertDialog.Builder(this)
            .setTitle(R.string.halt_all)
            .setMessage(R.string.confirm_halt)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(android.R.string.ok) { _, _ -> postHaltAll() }
            .show()
    }

    private fun startEtradeReauth() {
        lifecycleScope.launch {
            val url = Prefs.baseUrl(this@MainActivity)
            val user = Prefs.username(this@MainActivity)
            val pass = Prefs.password(this@MainActivity)
            val pin = Prefs.fingerprint(this@MainActivity)
            val start = withContext(Dispatchers.IO) {
                runCatching { MonitorApi.etradeOauthStart(url, user, pass, pin) }
                    .getOrElse { MonitorApi.OAuthStartResult(false, error = it.message) }
            }
            if (!start.ok || start.authorizeUrl.isNullOrBlank()) {
                Toast.makeText(
                    this@MainActivity,
                    start.error ?: "Could not start E*TRADE OAuth",
                    Toast.LENGTH_LONG,
                ).show()
                return@launch
            }
            try {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(start.authorizeUrl)))
            } catch (_: Exception) {
                Toast.makeText(this@MainActivity, "No browser available", Toast.LENGTH_LONG).show()
                return@launch
            }
            val input = EditText(this@MainActivity).apply {
                hint = getString(R.string.reauth_paste_code)
                setSingleLine()
            }
            AlertDialog.Builder(this@MainActivity)
                .setTitle(R.string.reauth_title)
                .setMessage(R.string.reauth_body)
                .setView(input)
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.reauth_complete) { _, _ ->
                    val code = input.text?.toString()?.trim().orEmpty()
                    if (code.isBlank()) {
                        Toast.makeText(this@MainActivity, "Enter the verification code", Toast.LENGTH_SHORT).show()
                        return@setPositiveButton
                    }
                    completeEtradeReauth(code)
                }
                .show()
        }
    }

    private fun completeEtradeReauth(verifier: String) {
        lifecycleScope.launch {
            val url = Prefs.baseUrl(this@MainActivity)
            val user = Prefs.username(this@MainActivity)
            val pass = Prefs.password(this@MainActivity)
            val pin = Prefs.fingerprint(this@MainActivity)
            val result = withContext(Dispatchers.IO) {
                runCatching { MonitorApi.etradeOauthComplete(url, user, pass, pin, verifier) }
                    .getOrElse { MonitorApi.OAuthCompleteResult(false, error = it.message) }
            }
            if (result.ok) {
                ReauthNotifier.clear(this@MainActivity)
                val note = if (result.armed) {
                    "E*TRADE reauthed and re-armed"
                } else {
                    "E*TRADE reauthed (arm from companion if needed)"
                }
                Toast.makeText(this@MainActivity, note, Toast.LENGTH_LONG).show()
                refreshStatus(force = true)
            } else {
                Toast.makeText(
                    this@MainActivity,
                    result.error ?: "Reauth failed",
                    Toast.LENGTH_LONG,
                ).show()
            }
        }
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
                applyArmedOptimistic(brokerRows.associate { it.name to false })
                Toast.makeText(this@MainActivity, "Halted all brokers", Toast.LENGTH_SHORT).show()
                refreshStatus(force = true)
            } else {
                Toast.makeText(
                    this@MainActivity,
                    result.error ?: "Halt failed",
                    Toast.LENGTH_LONG,
                ).show()
                refreshStatus(force = true)
            }
        }
    }

    private fun confirmAuto(broker: String, armed: Boolean, switch: SwitchMaterial) {
        dialogBroker = broker
        val msg = getString(if (armed) R.string.confirm_arm else R.string.confirm_disarm, broker)
        AlertDialog.Builder(this)
            .setTitle(if (armed) R.string.arm else R.string.disarm)
            .setMessage(msg)
            .setNegativeButton(android.R.string.cancel) { _, _ ->
                dialogBroker = null
                syncingUi = true
                switch.isChecked = !armed
                syncingUi = false
            }
            .setPositiveButton(android.R.string.ok) { _, _ -> postAuto(broker, armed) }
            .setOnCancelListener {
                dialogBroker = null
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
            dialogBroker = null
            if (result.ok) {
                applyArmedOptimistic(mapOf(broker to armed))
                Toast.makeText(
                    this@MainActivity,
                    if (armed) "Armed $broker" else "Disarmed $broker",
                    Toast.LENGTH_SHORT,
                ).show()
                refreshStatus(force = true)
            } else {
                val err = buildString {
                    append(result.error ?: "Request failed")
                    result.lockoutSeconds?.takeIf { it > 0 }?.let { append(" (lockout ${it}s)") }
                }
                Toast.makeText(this@MainActivity, err, Toast.LENGTH_LONG).show()
                refreshStatus(force = true)
            }
        }
    }
}
