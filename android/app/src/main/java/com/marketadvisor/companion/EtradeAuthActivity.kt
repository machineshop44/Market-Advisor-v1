package com.marketadvisor.companion

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONArray

/**
 * E*TRADE OAuth inside the Companion. A plain ACTION_VIEW on us.etrade.com is captured
 * by the E*TRADE app (App Links) and the post-login redirect drops the authorize step,
 * which took three taps. Here every etrade.com page stays in this WebView, the authorize
 * page is reloaded once logged in, and the verifier code is read off the page.
 */
class EtradeAuthActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var status: TextView
    private lateinit var codeInput: EditText
    private var authorizeUrl: String = ""
    private var sawLogin = false
    private var reloads = 0
    private var delivered = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        authorizeUrl = intent.getStringExtra(EXTRA_URL).orEmpty()
        if (authorizeUrl.isBlank()) {
            finish()
            return
        }
        title = getString(R.string.reauth_title)

        val pad = (12 * resources.displayMetrics.density).toInt()
        status = TextView(this).apply {
            setPadding(pad, pad / 2, pad, pad / 2)
            text = getString(R.string.reauth_web_status_login)
        }
        codeInput = EditText(this).apply {
            hint = getString(R.string.reauth_paste_code)
            setSingleLine()
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val submit = Button(this).apply {
            text = getString(R.string.reauth_complete)
            setOnClickListener { deliver(codeInput.text?.toString().orEmpty()) }
        }
        val reload = Button(this).apply {
            text = getString(R.string.reauth_web_reload)
            setOnClickListener { web.loadUrl(authorizeUrl) }
        }
        val browser = Button(this).apply {
            text = getString(R.string.reauth_web_browser)
            setOnClickListener { openInBrowser() }
        }
        val codeRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(pad, 0, pad, 0)
            addView(codeInput)
            addView(submit)
        }
        val toolRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(pad, 0, pad, 0)
            addView(reload)
            addView(browser)
        }

        web = WebView(this).apply {
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            // E*TRADE serves a degraded/blocked login to the embedded-WebView UA token.
            settings.userAgentString = settings.userAgentString.replace("; wv", "")
            webViewClient = AuthClient()
        }
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true)

        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(status)
            addView(codeRow)
            addView(toolRow)
            addView(web)
        })

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })

        web.loadUrl(authorizeUrl)
    }

    private inner class AuthClient : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val scheme = request.url.scheme?.lowercase().orEmpty()
            // http(s) stays here; intent:// / etrade:// would launch the E*TRADE app.
            return scheme != "http" && scheme != "https"
        }

        override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
            if (isLoginUrl(url)) sawLogin = true
        }

        override fun onPageFinished(view: WebView, url: String) {
            view.evaluateJavascript(FIND_CODE_JS) { raw ->
                val found = parseCode(raw)
                when {
                    found != null -> {
                        val (code, fromInput) = found
                        codeInput.setText(code)
                        status.text = getString(R.string.reauth_web_status_code, code)
                        if (fromInput) deliver(code)
                    }
                    isLoginUrl(url) -> status.text = getString(R.string.reauth_web_status_login)
                    url.contains("/etws/authorize", ignoreCase = true) ->
                        status.text = getString(R.string.reauth_web_status_accept)
                    sawLogin && isEtrade(url) && reloads < MAX_RELOADS -> {
                        // Logged in but E*TRADE dropped us on home/error — go back to authorize.
                        reloads += 1
                        status.text = getString(R.string.reauth_web_status_return)
                        view.loadUrl(authorizeUrl)
                    }
                }
            }
        }
    }

    private fun deliver(code: String) {
        val c = code.trim()
        if (c.isBlank()) {
            Toast.makeText(this, "Enter the verification code", Toast.LENGTH_SHORT).show()
            return
        }
        if (delivered) return
        delivered = true
        setResult(Activity.RESULT_OK, Intent().putExtra(EXTRA_CODE, c))
        finish()
    }

    private fun openInBrowser() {
        val view = Intent(Intent.ACTION_VIEW, Uri.parse(authorizeUrl))
        // Pin to the default browser so App Links can't hand the URL to the E*TRADE app.
        val probe = Intent(Intent.ACTION_VIEW, Uri.parse("https://example.com"))
        val pkg = packageManager.resolveActivity(probe, 0)?.activityInfo?.packageName
        if (!pkg.isNullOrBlank() && pkg != "android") view.setPackage(pkg)
        try {
            startActivity(view)
        } catch (_: Exception) {
            view.setPackage(null)
            runCatching { startActivity(view) }
        }
    }

    override fun onDestroy() {
        if (::web.isInitialized) web.destroy()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_URL = "authorize_url"
        const val EXTRA_CODE = "verifier"
        private const val MAX_RELOADS = 2

        private fun isEtrade(url: String): Boolean =
            Uri.parse(url).host?.lowercase()?.endsWith("etrade.com") == true

        private fun isLoginUrl(url: String): Boolean {
            val u = url.lowercase()
            return isEtrade(url) && (u.contains("login") || u.contains("logon") || u.contains("signin"))
        }

        /** Verifier codes are short upper-case alphanumerics shown in a read-only box. */
        private val FIND_CODE_JS = """
            (function(){
              var out=[];
              var re=/^[A-Z0-9]{4,10}$/;
              var ins=document.querySelectorAll('input');
              for(var i=0;i<ins.length;i++){
                var v=(ins[i].value||'').trim();
                var t=(ins[i].type||'').toLowerCase();
                if(t==='hidden'||t==='password') continue;
                if(re.test(v)&&(ins[i].readOnly||/code|verif/i.test(ins[i].name+' '+ins[i].id))) out.push(v);
              }
              if(out.length) return ['input',out[0]];
              var body=(document.body&&document.body.innerText)||'';
              var m=body.match(/[Vv]erification [Cc]ode[^A-Za-z0-9]{0,40}([A-Z0-9]{4,10})(?![A-Za-z0-9])/);
              if(m && /[0-9]/.test(m[1])) return ['text',m[1]];
              return [];
            })();
        """.trimIndent()

        /** (code, fromInput) — only an input-box match is trusted enough to auto-submit. */
        private fun parseCode(raw: String?): Pair<String, Boolean>? = runCatching {
            val arr = JSONArray(raw ?: "[]")
            if (arr.length() >= 2) arr.getString(1) to (arr.getString(0) == "input") else null
        }.getOrNull()
    }
}
