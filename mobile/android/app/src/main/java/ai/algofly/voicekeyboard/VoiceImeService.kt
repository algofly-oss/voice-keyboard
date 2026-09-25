package ai.algofly.voicekeyboard

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.inputmethodservice.InputMethodService
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.TextView

class VoiceImeService : InputMethodService(), DictationSession.Listener {
    private lateinit var prefs: Prefs
    private val main = Handler(Looper.getMainLooper())
    private var session: DictationSession? = null

    private lateinit var status: TextView
    private lateinit var mic: MicButton
    private lateinit var micArea: FrameLayout
    private lateinit var setupArea: LinearLayout
    private lateinit var setupText: TextView
    private lateinit var setupButton: Button

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onCreateInputView(): View {
        val bg = getColor(R.color.bg)
        val surface = getColor(R.color.surface)
        val textColor = getColor(R.color.text)
        val muted = getColor(R.color.muted)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(bg)
            setPadding(dp(8), dp(8), dp(8), dp(8))
        }

        status = TextView(this).apply {
            setTextColor(muted)
            textSize = 14f
            gravity = Gravity.CENTER
            maxLines = 2
            setPadding(dp(8), 0, dp(8), dp(4))
        }
        root.addView(status, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))

        // Centre: mic button, or a setup prompt when pairing/permission is missing.
        val centre = FrameLayout(this)
        mic = MicButton(this).apply { setOnClickListener { toggle() } }
        micArea = FrameLayout(this).apply {
            addView(mic, FrameLayout.LayoutParams(dp(140), dp(140), Gravity.CENTER))
        }
        centre.addView(micArea, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        setupText = TextView(this).apply {
            setTextColor(textColor)
            textSize = 15f
            gravity = Gravity.CENTER
        }
        setupButton = Button(this).apply {
            setTextColor(textColor)
            isAllCaps = false
            background = roundedBg(getColor(R.color.accent), 20)
            setPadding(dp(20), 0, dp(20), 0)
            setOnClickListener { openSetup() }
        }
        setupArea = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            addView(setupText, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = dp(12) })
            addView(setupButton, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(44)))
        }
        centre.addView(setupArea, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        root.addView(centre, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(160)))

        // Bottom row: switch keyboard, space, backspace, enter.
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(8), 0, 0)
        }
        fun key(icon: Int, desc: String): ImageButton = ImageButton(this).apply {
            setImageResource(icon)
            contentDescription = desc
            background = roundedBg(surface)
        }
        val globe = key(R.drawable.ic_globe, "Switch keyboard").apply {
            setOnClickListener { switchKeyboard() }
            setOnLongClickListener { imm().showInputMethodPicker(); true }
        }
        val space = Button(this).apply {
            text = "space"
            isAllCaps = false
            setTextColor(muted)
            background = roundedBg(surface)
            setOnClickListener { haptic(it); currentInputConnection?.commitText(" ", 1) }
            setOnLongClickListener { imm().showInputMethodPicker(); true }
        }
        val backspace = key(R.drawable.ic_backspace, "Delete")
        backspace.setOnTouchListener(RepeatListener { deleteOne() })
        val enter = key(R.drawable.ic_enter, "Enter").apply {
            background = roundedBg(getColor(R.color.accent))
            setOnClickListener { haptic(it); enter() }
        }
        val h = dp(48)
        fun lp(weight: Float) = LinearLayout.LayoutParams(0, h, weight).apply { marginStart = dp(3); marginEnd = dp(3) }
        row.addView(globe, lp(1f))
        row.addView(space, lp(3f))
        row.addView(backspace, lp(1f))
        row.addView(enter, lp(1f))
        root.addView(row, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))

        refreshState()
        return root
    }

    override fun onStartInputView(info: EditorInfo?, restarting: Boolean) {
        super.onStartInputView(info, restarting)
        if (session == null) refreshState()
    }

    override fun onFinishInputView(finishingInput: Boolean) {
        super.onFinishInputView(finishingInput)
        cancelSession()
    }

    override fun onDestroy() {
        cancelSession()
        main.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    // ---- state ----

    private fun hasMic() = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun refreshState(message: String? = null) {
        if (!::status.isInitialized) return
        val ready = prefs.isPaired && hasMic()
        micArea.visibility = if (ready) View.VISIBLE else View.GONE
        setupArea.visibility = if (ready) View.GONE else View.VISIBLE
        when {
            !prefs.isPaired -> {
                setupText.text = message ?: "Pair this phone with your Voice Keyboard server first."
                setupButton.text = if (message != null) "Pair again" else "Open setup"
                status.text = ""
            }
            !hasMic() -> {
                setupText.text = "Microphone permission is needed to dictate."
                setupButton.text = "Grant permission"
                status.text = ""
            }
            else -> {
                mic.mode = MicButton.Mode.IDLE
                status.text = message ?: "Tap to dictate"
            }
        }
    }

    private fun openSetup() {
        startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    // ---- dictation ----

    private fun toggle() {
        val s = session
        if (s == null) startSession()
        else if (s.isRecording) s.stop()
        else { cancelSession(); refreshState() } // tap while connecting/finishing aborts
    }

    private fun startSession() {
        if (!prefs.isPaired || !hasMic()) { refreshState(); return }
        mic.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY)
        mic.mode = MicButton.Mode.CONNECTING
        status.text = "Connecting…"
        session = DictationSession(prefs.server, prefs.client, prefs.credential, prefs.language, this)
            .also { it.start() }
    }

    private fun cancelSession() {
        session?.cancel()
        session = null
        if (::mic.isInitialized) mic.mode = MicButton.Mode.IDLE
    }

    override fun onListening() {
        mic.mode = MicButton.Mode.RECORDING
        status.text = "Listening…"
    }

    override fun onLevel(level: Float) {
        mic.level = level
    }

    override fun onSegment(text: String) {
        val ic = currentInputConnection ?: return
        var piece = text
        // Pieces carry their own leading separator; drop it at the start of a field/line.
        if (piece.startsWith(" ")) {
            val before = ic.getTextBeforeCursor(1, 0)
            if (before.isNullOrEmpty() || before.last().isWhitespace()) piece = piece.trimStart(' ')
        }
        if (piece.isNotEmpty()) ic.commitText(piece, 1)
    }

    override fun onFinishing() {
        mic.mode = MicButton.Mode.FINISHING
        status.text = "Finishing…"
    }

    override fun onEnded(error: String?, authFailed: Boolean) {
        session = null
        if (authFailed) {
            prefs.clearCredential()
            refreshState("This phone's pairing is no longer valid. Pair again from the setup app.")
            return
        }
        refreshState(error)
        if (error != null) status.setTextColor(getColor(R.color.danger))
        else status.setTextColor(getColor(R.color.muted))
        if (error != null) main.postDelayed({
            if (session == null && ::status.isInitialized) {
                status.setTextColor(getColor(R.color.muted)); status.text = "Tap to dictate"
            }
        }, 6000)
    }

    // ---- keys ----

    private fun haptic(v: View) = v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)

    private fun imm() = getSystemService(InputMethodManager::class.java)

    @Suppress("DEPRECATION")
    private fun switchKeyboard() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            if (!switchToNextInputMethod(false)) imm().showInputMethodPicker()
        } else {
            val token = window?.window?.attributes?.token
            if (token == null || !imm().switchToNextInputMethod(token, false)) imm().showInputMethodPicker()
        }
    }

    private fun deleteOne() {
        val ic = currentInputConnection ?: return
        val selected = ic.getSelectedText(0)
        if (!selected.isNullOrEmpty()) ic.commitText("", 1)
        else sendDownUpKeyEvents(KeyEvent.KEYCODE_DEL) // handles surrogate pairs/emoji
    }

    private fun enter() {
        val ic = currentInputConnection ?: return
        val ei = currentInputEditorInfo
        val action = ei?.imeOptions?.and(EditorInfo.IME_MASK_ACTION) ?: EditorInfo.IME_ACTION_NONE
        val noAction = ei != null && (ei.imeOptions and EditorInfo.IME_FLAG_NO_ENTER_ACTION) != 0
        if (!noAction && action != EditorInfo.IME_ACTION_NONE && action != EditorInfo.IME_ACTION_UNSPECIFIED) {
            ic.performEditorAction(action)
        } else {
            sendDownUpKeyEvents(KeyEvent.KEYCODE_ENTER)
        }
    }

    /** Fires once on press, then repeats while held. */
    private inner class RepeatListener(private val action: () -> Unit) : View.OnTouchListener {
        private val repeat = object : Runnable {
            override fun run() { action(); main.postDelayed(this, 50) }
        }

        override fun onTouch(v: View, e: MotionEvent): Boolean {
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    v.isPressed = true; haptic(v); action()
                    main.postDelayed(repeat, 400)
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    v.isPressed = false; main.removeCallbacks(repeat)
                    if (e.actionMasked == MotionEvent.ACTION_UP) v.performClick()
                }
            }
            return true
        }
    }
}
