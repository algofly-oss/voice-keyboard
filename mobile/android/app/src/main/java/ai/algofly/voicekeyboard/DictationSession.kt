package ai.algofly.voicekeyboard

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.Looper
import android.os.Process
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import kotlin.math.min
import kotlin.math.sqrt

/**
 * One dictation: records 16 kHz mono PCM16 with AudioRecord on a background thread and
 * streams it to {server}/v1/stream. All [Listener] callbacks run on the main thread and
 * stop after [cancel]. Single use: create a new session per dictation.
 */
class DictationSession(
    private val server: String,
    private val client: String,
    private val credential: String,
    private val language: String,
    private val listener: Listener,
) {
    interface Listener {
        fun onListening()
        fun onLevel(level: Float)
        fun onSegment(text: String)
        fun onFinishing()
        /** Always called exactly once unless cancelled. [error] null means success. */
        fun onEnded(error: String?, authFailed: Boolean)
    }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var recording = false
    @Volatile private var done = false
    private var ws: WebSocket? = null
    private var audioThread: Thread? = null
    private var serverError: String? = null

    private val capTimeout = Runnable { stop() }
    private val finishTimeout = Runnable { end("Server did not finish in time", false); ws?.cancel() }

    fun start() {
        val url = try {
            Net.streamUrl(server, credential)
        } catch (e: Exception) {
            end("Bad server URL", false); return
        }
        val socket = Net.http.newWebSocket(Request.Builder().url(url).build(), SocketListener())
        ws = socket
        // OkHttp queues messages sent before the handshake completes, so start capturing
        // immediately instead of losing the first words to connection latency.
        val hello = JSONObject()
            .put("type", "start")
            .put("language", language)
            .put("prompt", "")
            .put("model", "")
            .put("live", true)
            .put("room", "mobile-$client") // keeps text off desktop keyboards in the default room
            .put("client", client)
        socket.send(hello.toString())
        recording = true
        audioThread = Thread({ recordLoop(socket) }, "vk-audio").also { it.start() }
        main.postDelayed(capTimeout, MAX_SESSION_MS)
    }

    /** Finish: server flushes remaining audio, sends `final`, then closes. */
    fun stop() {
        if (done || !recording) return
        recording = false
        main.removeCallbacks(capTimeout)
        listener.onFinishing()
        main.postDelayed(finishTimeout, FINISH_TIMEOUT_MS)
        // The audio thread sends {"type":"stop"} after its last frame so ordering is kept.
    }

    /** Abort without further callbacks. */
    fun cancel() {
        if (done) return
        done = true
        recording = false
        main.removeCallbacks(capTimeout)
        main.removeCallbacks(finishTimeout)
        ws?.let {
            it.send("{\"type\":\"cancel\"}")
            it.close(1000, "cancel")
        }
    }

    val isRecording: Boolean get() = recording

    @SuppressLint("MissingPermission") // checked by the caller before starting
    private fun recordLoop(socket: WebSocket) {
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val record = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, FRAME_SAMPLES * 2 * 4))
        } catch (e: Exception) {
            fail("Microphone unavailable: ${e.message}"); return
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release(); fail("Microphone unavailable"); return
        }
        val samples = ShortArray(FRAME_SAMPLES)
        val bytes = ByteArray(FRAME_SAMPLES * 2)
        try {
            record.startRecording()
            if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                fail("Microphone is in use by another app"); return
            }
            main.post { if (!done) listener.onListening() }
            var filled = 0
            while (recording && !done) {
                val n = record.read(samples, filled, FRAME_SAMPLES - filled)
                if (n < 0) { fail("Microphone read error ($n)"); return }
                filled += n
                if (filled < FRAME_SAMPLES) continue
                filled = 0
                var sum = 0.0
                for (i in 0 until FRAME_SAMPLES) {
                    val s = samples[i].toInt()
                    bytes[2 * i] = (s and 0xff).toByte()          // little-endian
                    bytes[2 * i + 1] = ((s shr 8) and 0xff).toByte()
                    sum += s.toDouble() * s
                }
                if (!socket.send(bytes.toByteString())) break // socket closed/failed
                val rms = sqrt(sum / FRAME_SAMPLES) / 32768.0
                val level = min(1.0, sqrt(rms) * 2.2).toFloat()   // perceptual-ish scaling
                socket.send("{\"type\":\"level\",\"v\":${"%.3f".format(java.util.Locale.US, level)}}")
                main.post { if (!done) listener.onLevel(level) }
            }
        } finally {
            try { record.stop() } catch (_: Exception) {}
            record.release()
        }
        if (!done) socket.send("{\"type\":\"stop\"}")
    }

    private fun fail(message: String) {
        recording = false
        main.post {
            if (done) return@post
            end(message, false)
            ws?.cancel()
        }
    }

    /** Main thread only. */
    private fun end(error: String?, authFailed: Boolean) {
        if (done) return
        done = true
        recording = false
        main.removeCallbacks(capTimeout)
        main.removeCallbacks(finishTimeout)
        listener.onEnded(error, authFailed)
    }

    private inner class SocketListener : WebSocketListener() {
        override fun onMessage(webSocket: WebSocket, text: String) {
            val msg = try { JSONObject(text) } catch (e: Exception) { return }
            when (msg.optString("type")) {
                "segment" -> {
                    val piece = msg.optString("text")
                    if (piece.isNotEmpty()) main.post { if (!done) listener.onSegment(piece) }
                }
                "final" -> main.post { end(null, false) }
                "error" -> {
                    serverError = msg.optString("message").ifEmpty { "Server error" }
                    main.post { end(serverError, false) }
                }
                "remote_stop" -> main.post { stop() }
                "remote_cancel" -> main.post { end("Cancelled from another device", false); ws?.cancel() }
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {}

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(1000, null)
            main.post { handleClose(code, reason) }
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            main.post { handleClose(code, reason) }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            recording = false
            val msg = when {
                response?.code == 401 || response?.code == 403 -> null
                else -> "Connection failed: ${t.message ?: t.javaClass.simpleName}"
            }
            main.post {
                if (msg == null) end("Pair again: credential rejected", true) else end(msg, false)
            }
        }
    }

    private fun handleClose(code: Int, reason: String) {
        recording = false
        when {
            code == 4401 -> end("Pair again: ${reason.ifEmpty { "credential rejected" }}", true)
            serverError != null -> end(serverError, false)
            // Closed without a final (e.g. server restarted); keep what was already typed.
            else -> end(if (code == 1000) null else "Connection closed ($code)", false)
        }
    }

    companion object {
        const val SAMPLE_RATE = 16_000
        const val FRAME_SAMPLES = SAMPLE_RATE / 10 // 100 ms
        const val MAX_SESSION_MS = 10 * 60 * 1000L
        const val FINISH_TIMEOUT_MS = 45_000L
    }
}
