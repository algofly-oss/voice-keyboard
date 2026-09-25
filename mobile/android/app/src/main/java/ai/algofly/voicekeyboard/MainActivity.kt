package ai.algofly.voicekeyboard

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.InputMethodManager
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import java.util.concurrent.Executors

/** Setup: pair, microphone permission, enable + select the keyboard, language. */
class MainActivity : Activity() {
    private lateinit var prefs: Prefs
    private val io = Executors.newSingleThreadExecutor()
    private var pairing = false

    private lateinit var pairStatus: TextView
    private lateinit var serverField: EditText
    private lateinit var codeField: EditText
    private lateinit var pairButton: Button
    private lateinit var micStatus: TextView
    private lateinit var micButton: Button
    private lateinit var enableStatus: TextView
    private lateinit var switchStatus: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(buildUi())
        handleIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    // The input-method picker is a dialog over us, so onResume does not fire after it.
    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) refresh()
    }

    override fun onDestroy() {
        io.shutdownNow()
        super.onDestroy()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_MIC && grantResults.firstOrNull() != PackageManager.PERMISSION_GRANTED &&
            !shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO)) {
            // "Don't ask again": the only way left is the app's settings page.
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", packageName, null)))
        }
        refresh()
    }

    // ---- deep link: voicekeyboard://pair?server=<origin>&token=<token> ----

    private fun handleIntent(intent: Intent?) {
        val uri = intent?.data ?: return
        if (uri.scheme != "voicekeyboard" || uri.host != "pair") return
        val server = uri.getQueryParameter("server").orEmpty()
        val token = uri.getQueryParameter("token").orEmpty()
        if (server.isEmpty() || token.isEmpty()) {
            setPairStatus("Pairing link is incomplete. Enter the server and code below.", error = true)
            return
        }
        serverField.setText(server)
        codeField.setText(token)
        intent.data = null // don't re-pair with a spent token on recreation
        doPair(server, token)
    }

    private fun doPair(server: String, token: String) {
        if (pairing) return
        if (token.isBlank()) { setPairStatus("Enter the pairing code.", error = true); return }
        pairing = true
        pairButton.isEnabled = false
        setPairStatus("Pairing…", error = false)
        val clientId = prefs.clientIdForPairing()
        io.execute {
            val result = try {
                Result.success(Net.pair(server, token, clientId))
            } catch (e: Exception) {
                Result.failure(e)
            }
            runOnUiThread {
                pairing = false
                if (isDestroyed) return@runOnUiThread
                pairButton.isEnabled = true
                result.onSuccess {
                    prefs.savePairing(it.server, it.client, it.credential)
                    codeField.setText("")
                    refresh()
                }.onFailure {
                    setPairStatus("Pairing failed: ${it.message}", error = true)
                }
            }
        }
    }

    // ---- status ----

    private fun refresh() {
        if (!pairing) {
            if (prefs.isPaired) setPairStatus("✓ Paired with ${prefs.server}", ok = true)
            else if (prefs.server.isNotEmpty() && prefs.client.isNotEmpty())
                setPairStatus("Pairing is no longer valid. Tap “Pair this phone” on the server's /downloads page again.", error = true)
            else setPairStatus("Not paired. Open /downloads on your server and tap “Pair this phone”, or enter a code below.")
        }

        val mic = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        status(micStatus, if (mic) "✓ Granted" else "Not granted", mic)
        micButton.visibility = if (mic) View.GONE else View.VISIBLE

        val imm = getSystemService(InputMethodManager::class.java)
        val enabled = imm.enabledInputMethodList.any { it.packageName == packageName }
        status(enableStatus, if (enabled) "✓ Enabled" else "Not enabled", enabled)

        val current = Settings.Secure.getString(contentResolver, Settings.Secure.DEFAULT_INPUT_METHOD).orEmpty()
        val selected = current.startsWith("$packageName/")
        status(switchStatus, if (selected) "✓ Voice Keyboard is the current keyboard" else "Not the current keyboard", selected)
    }

    private fun setPairStatus(text: String, error: Boolean = false, ok: Boolean = false) {
        pairStatus.text = text
        pairStatus.setTextColor(getColor(if (error) R.color.danger else if (ok) R.color.ok else R.color.muted))
    }

    private fun status(v: TextView, text: String, ok: Boolean) {
        v.text = text
        v.setTextColor(getColor(if (ok) R.color.ok else R.color.muted))
    }

    // ---- UI ----

    private fun buildUi(): View {
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(24), dp(20), dp(32))
        }
        col.addView(TextView(this).apply {
            text = getString(R.string.app_name)
            textSize = 26f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(getColor(R.color.text))
        })
        col.addView(TextView(this).apply {
            text = "Dictate into any text field using your own Whisper server."
            setTextColor(getColor(R.color.muted))
            setPadding(0, dp(4), 0, dp(8))
        })

        // 1. Pair
        val pair = card(col, "1. Pair with server")
        pairStatus = body(pair)
        serverField = field(pair, "Server URL", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI).apply {
            setText(prefs.server.ifEmpty { DEFAULT_SERVER })
        }
        codeField = field(pair, "Pairing code", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS)
        pairButton = button(pair, "Pair") { doPair(serverField.text.toString(), codeField.text.toString()) }

        // 2. Microphone
        val mic = card(col, "2. Microphone permission")
        micStatus = body(mic)
        micButton = button(mic, "Allow microphone") {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC)
        }

        // 3. Enable
        val enable = card(col, "3. Enable the keyboard")
        enableStatus = body(enable)
        button(enable, "Open keyboard settings") {
            startActivity(Intent(Settings.ACTION_INPUT_METHOD_SETTINGS))
        }

        // 4. Switch
        val sw = card(col, "4. Switch to Voice Keyboard")
        switchStatus = body(sw)
        button(sw, "Choose keyboard") {
            getSystemService(InputMethodManager::class.java).showInputMethodPicker()
        }

        // Settings
        val settings = card(col, "Settings")
        body(settings).apply { text = "Dictation language"; setTextColor(getColor(R.color.muted)) }
        val labels = Prefs.LANGUAGES.map { it.second }
        val spinner = Spinner(this).apply {
            adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_item, labels).also {
                it.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
            }
            setSelection(Prefs.LANGUAGES.indexOfFirst { it.first == prefs.language }.coerceAtLeast(0))
            onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
                override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) {
                    prefs.language = Prefs.LANGUAGES[pos].first
                }
                override fun onNothingSelected(p: AdapterView<*>?) {}
            }
        }
        settings.addView(spinner, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48)))

        // Try it
        val tryIt = card(col, "Try it")
        field(tryIt, "Tap here and dictate", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE).apply {
            minLines = 2
            gravity = Gravity.TOP or Gravity.START
        }

        return ScrollView(this).apply {
            isFillViewport = true
            setBackgroundColor(getColor(R.color.bg))
            addView(col)
        }
    }

    private fun card(parent: LinearLayout, title: String): LinearLayout {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = roundedBg(getColor(R.color.surface), 12)
            setPadding(dp(16), dp(14), dp(16), dp(14))
        }
        card.addView(TextView(this).apply {
            text = title
            textSize = 17f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(getColor(R.color.text))
        })
        parent.addView(card, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(12) })
        return card
    }

    private fun body(parent: LinearLayout) = TextView(this).apply {
        textSize = 14f
        setPadding(0, dp(4), 0, dp(4))
        parent.addView(this)
    }

    private fun field(parent: LinearLayout, hint: String, type: Int) = EditText(this).apply {
        this.hint = hint
        inputType = type
        setTextColor(getColor(R.color.text))
        setHintTextColor(getColor(R.color.muted))
        textSize = 15f
        parent.addView(this, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(4) })
    }

    private fun button(parent: LinearLayout, label: String, onClick: () -> Unit) = Button(this).apply {
        text = label
        isAllCaps = false
        setTextColor(getColor(R.color.text))
        background = roundedBg(getColor(R.color.accent), 8)
        setOnClickListener { onClick() }
        parent.addView(this, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(44)).apply { topMargin = dp(8) })
    }

    companion object {
        private const val REQ_MIC = 1
        private const val DEFAULT_SERVER = "https://voice-keyboard-transcription.algofly.ai"
    }
}
