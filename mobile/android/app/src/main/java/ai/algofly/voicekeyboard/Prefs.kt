package ai.algofly.voicekeyboard

import android.content.Context
import android.content.SharedPreferences
import java.security.SecureRandom

/** Pairing + settings, kept in app-private SharedPreferences (MODE_PRIVATE). */
class Prefs(context: Context) {
    private val p: SharedPreferences =
        context.applicationContext.getSharedPreferences("voice_keyboard", Context.MODE_PRIVATE)

    var server: String
        get() = p.getString(KEY_SERVER, "") ?: ""
        set(v) = p.edit().putString(KEY_SERVER, v).apply()

    var client: String
        get() = p.getString(KEY_CLIENT, "") ?: ""
        set(v) = p.edit().putString(KEY_CLIENT, v).apply()

    var credential: String
        get() = p.getString(KEY_CREDENTIAL, "") ?: ""
        set(v) = p.edit().putString(KEY_CREDENTIAL, v).apply()

    var language: String
        get() = p.getString(KEY_LANGUAGE, "") ?: ""
        set(v) = p.edit().putString(KEY_LANGUAGE, v).apply()

    val isPaired: Boolean get() = server.isNotEmpty() && credential.isNotEmpty() && client.isNotEmpty()

    /** Stable id sent when pairing; the server may echo it back or assign its own. */
    fun clientIdForPairing(): String {
        client.takeIf { it.isNotEmpty() }?.let { return it }
        val bytes = ByteArray(4).also { SecureRandom().nextBytes(it) }
        return "android-" + bytes.joinToString("") { "%02x".format(it) }
    }

    fun savePairing(server: String, client: String, credential: String) {
        p.edit()
            .putString(KEY_SERVER, server)
            .putString(KEY_CLIENT, client)
            .putString(KEY_CREDENTIAL, credential)
            .apply()
    }

    fun clearCredential() = p.edit().remove(KEY_CREDENTIAL).apply()

    companion object {
        private const val KEY_SERVER = "server"
        private const val KEY_CLIENT = "client"
        private const val KEY_CREDENTIAL = "credential"
        private const val KEY_LANGUAGE = "language"

        /** Code to label; "" means auto-detect. */
        val LANGUAGES = listOf(
            "" to "Auto-detect",
            "en" to "English",
            "hi" to "Hindi",
            "es" to "Spanish",
            "fr" to "French",
            "de" to "German",
            "it" to "Italian",
            "pt" to "Portuguese",
            "nl" to "Dutch",
            "ru" to "Russian",
            "ja" to "Japanese",
            "ko" to "Korean",
            "zh" to "Chinese",
            "ar" to "Arabic",
            "bn" to "Bengali",
            "ta" to "Tamil",
            "te" to "Telugu",
            "mr" to "Marathi",
            "ur" to "Urdu",
        )
    }
}
