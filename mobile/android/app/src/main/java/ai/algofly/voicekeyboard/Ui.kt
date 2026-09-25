package ai.algofly.voicekeyboard

import android.content.Context
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.content.res.ColorStateList

internal fun Context.dp(v: Int): Int = (v * resources.displayMetrics.density + 0.5f).toInt()

/** Rounded filled background with a ripple, used for keys and buttons. */
internal fun Context.roundedBg(color: Int, radiusDp: Int = 8): RippleDrawable {
    val shape = GradientDrawable().apply {
        setColor(color)
        cornerRadius = dp(radiusDp).toFloat()
    }
    return RippleDrawable(ColorStateList.valueOf(0x33FFFFFF), shape, null)
}
