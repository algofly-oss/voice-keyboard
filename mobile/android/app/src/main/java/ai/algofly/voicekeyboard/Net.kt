package ai.algofly.voicekeyboard

import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

object Net {
    /** Shared client: one connection pool / dispatcher for the whole process. */
    val http: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(15, TimeUnit.SECONDS)
            .pingInterval(20, TimeUnit.SECONDS)
            .build()
    }

    /** "example.com" -> "https://example.com"; strips trailing slashes. Null if unusable. */
    fun normalizeServer(input: String): String? {
        var s = input.trim()
        if (s.isEmpty()) return null
        if (!s.contains("://")) s = "https://$s"
        val url = s.toHttpUrlOrNull() ?: return null
        return url.toString().trimEnd('/')
    }

    /** http(s)://host[/prefix] -> ws(s)://host[/prefix]/v1/stream?token=... */
    fun streamUrl(server: String, credential: String): String {
        val base = server.toHttpUrlOrNull() ?: throw IOException("Bad server URL")
        val http = base.newBuilder()
            .addPathSegments("v1/stream")
            .addQueryParameter("token", credential)
            .build()
            .toString()
        return if (http.startsWith("https:")) "wss:" + http.removePrefix("https:")
        else "ws:" + http.removePrefix("http:")
    }

    class PairResult(val server: String, val client: String, val credential: String)

    class PairException(message: String) : Exception(message)

    /** Blocking; call off the main thread. */
    fun pair(serverInput: String, token: String, clientId: String): PairResult {
        val server = normalizeServer(serverInput) ?: throw PairException("Invalid server URL")
        val url = server.toHttpUrlOrNull()!!.newBuilder().addPathSegments("api/pair").build()
        val body = JSONObject().put("token", token.trim()).put("client", clientId).toString()
            .toRequestBody("application/json".toMediaType())
        val req = Request.Builder().url(url).post(body).build()
        try {
            http.newCall(req).execute().use { resp ->
                val text = resp.body?.string().orEmpty()
                if (resp.code == 401) throw PairException("This pairing code was replaced. Open /downloads again and tap Pair this phone.")
                if (!resp.isSuccessful) throw PairException("Server returned HTTP ${resp.code}")
                val json = try { JSONObject(text) } catch (e: Exception) {
                    throw PairException("Unexpected server response")
                }
                val credential = json.optString("credential")
                if (credential.isEmpty()) throw PairException("Server did not return a credential")
                // Keep the URL we actually reached; it is known to work from this phone.
                return PairResult(server, json.optString("client").ifEmpty { clientId }, credential)
            }
        } catch (e: IOException) {
            throw PairException("Cannot reach server: ${e.message ?: e.javaClass.simpleName}")
        }
    }
}
