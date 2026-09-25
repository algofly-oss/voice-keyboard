package ai.algofly.voicekeyboard

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.drawable.Drawable
import android.view.View
import kotlin.math.min

/** Round mic button with a halo whose size follows the input level. */
class MicButton(context: Context) : View(context) {
    enum class Mode { IDLE, CONNECTING, RECORDING, FINISHING }

    var mode: Mode = Mode.IDLE
        set(v) { field = v; if (v != Mode.RECORDING) level = 0f; updateDescription(); invalidate() }

    var level: Float = 0f
        set(v) { field = v.coerceIn(0f, 1f); invalidate() }

    private val accent = context.getColor(R.color.accent)
    private val danger = context.getColor(R.color.danger)
    private val surface = context.getColor(R.color.surface_hi)
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val halo = Paint(Paint.ANTI_ALIAS_FLAG)
    private val mic: Drawable = context.getDrawable(R.drawable.ic_mic)!!
    private val stop: Drawable = context.getDrawable(R.drawable.ic_stop)!!
    private var shown = 0f // smoothed level

    init {
        isClickable = true
        isFocusable = true
        updateDescription()
    }

    private fun updateDescription() {
        contentDescription = if (mode == Mode.IDLE) "Start dictation" else "Stop dictation"
    }

    override fun onDraw(canvas: Canvas) {
        val cx = width / 2f
        val cy = height / 2f
        val maxR = min(width, height) / 2f
        val r = maxR * 0.66f
        shown += (level - shown) * 0.5f
        val color = when (mode) {
            Mode.IDLE -> accent
            Mode.CONNECTING, Mode.FINISHING -> surface
            Mode.RECORDING -> danger
        }
        if (mode == Mode.RECORDING) {
            halo.color = danger
            halo.alpha = 70
            canvas.drawCircle(cx, cy, r + (maxR - r) * (0.25f + 0.75f * shown), halo)
        }
        fill.color = color
        if (isPressed) fill.alpha = 200
        canvas.drawCircle(cx, cy, r, fill)
        val icon = if (mode == Mode.IDLE) mic else stop
        val s = (r * 0.55f).toInt()
        icon.setBounds((cx - s).toInt(), (cy - s).toInt(), (cx + s).toInt(), (cy + s).toInt())
        icon.draw(canvas)
        if (mode == Mode.RECORDING && kotlin.math.abs(level - shown) > 0.01f) postInvalidateOnAnimation()
    }

    override fun drawableStateChanged() {
        super.drawableStateChanged()
        invalidate()
    }
}
