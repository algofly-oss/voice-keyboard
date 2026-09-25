import AVFoundation
import Foundation

/// Records in the containing app and streams 16 kHz mono PCM to /v1/stream.
///
/// A "session" keeps the audio engine running so iOS lets the app stay alive in
/// the background while the user is back in another app using the keyboard.
/// Audio is only sent to the server between a keyboard `start` and `stop`.
@MainActor
final class DictationEngine: ObservableObject {
    static let shared = DictationEngine()

    @Published private(set) var sessionActive = false
    @Published private(set) var dictating = false
    @Published private(set) var lastError: String?

    /// How long the session lingers without dictation before releasing the mic.
    var idleTimeout: TimeInterval = 5 * 60
    private static let maxDictation: TimeInterval = 10 * 60

    private let engine = AVAudioEngine()
    private var socket: URLSessionWebSocketTask?
    private var observers: [DarwinObserver] = []
    private var heartbeat: Timer?
    private var lastActivity = Date()
    private var dictationStarted = Date()

    private init() {
        observers = DictationBridge.observeCommands { [weak self] command in
            guard let self else { return }
            switch command {
            case .start: self.startDictation()
            case .stop: self.stopDictation(cancel: false)
            case .cancel: self.stopDictation(cancel: true)
            }
        }
    }

    // MARK: Session

    func startSession() throws {
        guard !sessionActive else { return }
        let audio = AVAudioSession.sharedInstance()
        try audio.setCategory(.playAndRecord, mode: .measurement, options: [.mixWithOthers, .allowBluetooth, .defaultToSpeaker])
        try audio.setActive(true)

        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard let converter = PCMConverter(from: format) else { throw CocoaError(.featureUnsupported) }
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            // Convert on the render thread: the engine may reuse `buffer` after we return.
            guard let chunk = converter.convert(buffer) else { return }
            Task { @MainActor in self?.send(chunk) }
        }
        engine.prepare()
        try engine.start()

        sessionActive = true
        lastActivity = Date()
        DictationBridge.heartbeat()
        DictationBridge.publish(state: .idle)
        heartbeat = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
    }

    func endSession() {
        stopDictation(cancel: true)
        heartbeat?.invalidate()
        heartbeat = nil
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        sessionActive = false
        DictationBridge.publish(state: .idle, message: "Open Voice Keyboard to start")
    }

    private func tick() {
        DictationBridge.heartbeat()
        if dictating, Date().timeIntervalSince(dictationStarted) > Self.maxDictation {
            stopDictation(cancel: false)
        }
        if !dictating, Date().timeIntervalSince(lastActivity) > idleTimeout {
            endSession()
        }
    }

    // MARK: Dictation

    func startDictation() {
        guard !dictating else { return }
        guard let server = SharedStore.server, let client = SharedStore.client,
              let credential = SharedStore.credential else {
            fail("Pair this phone first")
            return
        }
        if !sessionActive {
            do { try startSession() } catch { fail("Microphone unavailable: \(error.localizedDescription)"); return }
        }
        guard var components = URLComponents(string: server + "/v1/stream") else { fail("Invalid server URL"); return }
        components.scheme = components.scheme == "https" ? "wss" : "ws"
        components.queryItems = [URLQueryItem(name: "token", value: credential)]
        guard let url = components.url else { fail("Invalid server URL"); return }

        let task = URLSession.shared.webSocketTask(with: url)
        socket = task
        task.resume()
        // A per-device room keeps phone dictation from also typing on desktop clients.
        let start: [String: Any] = ["type": "start", "language": SharedStore.language, "prompt": "", "model": "",
                                    "live": true, "room": "mobile-\(client)", "client": client]
        send(json: start)
        receive(on: task)
        dictating = true
        dictationStarted = Date()
        lastActivity = Date()
        lastError = nil
        DictationBridge.publish(state: .listening, message: "Listening…")
    }

    func stopDictation(cancel: Bool) {
        guard dictating else { return }
        dictating = false
        lastActivity = Date()
        send(json: ["type": cancel ? "cancel" : "stop"])
        if cancel {
            socket?.cancel(with: .normalClosure, reason: nil)
            socket = nil
            DictationBridge.publish(state: .idle)
        } else {
            // The server flushes the last utterance, sends "final" and closes.
            DictationBridge.publish(state: .finishing, message: "Finishing…")
        }
    }

    private func receive(on task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.socket === task else { return }
                switch result {
                case .failure:
                    let code = task.closeCode.rawValue
                    self.socket = nil
                    if code == 4401 { self.fail("Pairing expired. Pair this phone again.") }
                    else if self.dictating { self.fail("Connection lost") }
                    else { DictationBridge.publish(state: .idle) }
                    self.dictating = false
                case .success(let message):
                    if case .string(let text) = message { self.handle(text) }
                    self.receive(on: task)
                }
            }
        }
    }

    private func handle(_ text: String) {
        guard let data = text.data(using: .utf8),
              let message = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        switch message["type"] as? String {
        case "segment":
            if let piece = message["text"] as? String, !piece.isEmpty { DictationBridge.publish(segment: piece) }
        case "final":
            socket?.cancel(with: .normalClosure, reason: nil)
            socket = nil
            dictating = false
            DictationBridge.publish(state: .idle)
        case "error":
            fail(message["message"] as? String ?? "Transcription failed")
        case "remote_stop":
            stopDictation(cancel: false)
        case "remote_cancel":
            stopDictation(cancel: true)
        default:
            break
        }
    }

    private func fail(_ message: String) {
        lastError = message
        dictating = false
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        DictationBridge.publish(state: .error, message: message)
    }

    private func send(json: [String: Any]) {
        guard let socket, let data = try? JSONSerialization.data(withJSONObject: json),
              let text = String(data: data, encoding: .utf8) else { return }
        socket.send(.string(text)) { _ in }
    }

    private func send(_ chunk: PCMConverter.Chunk) {
        guard dictating, let socket else { return }
        socket.send(.data(chunk.data)) { _ in }
        DictationBridge.publish(level: chunk.level)
    }
}

/// Resamples microphone buffers to 16 kHz mono little-endian Int16.
final class PCMConverter: @unchecked Sendable {
    struct Chunk: Sendable { let data: Data; let level: Double }

    private let converter: AVAudioConverter
    private static let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!
    private var target: AVAudioFormat { Self.target }

    init?(from format: AVAudioFormat) {
        guard let converter = AVAudioConverter(from: format, to: Self.target) else { return nil }
        self.converter = converter
    }

    func convert(_ buffer: AVAudioPCMBuffer) -> Chunk? {
        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 16
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return nil }
        var consumed = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if consumed { status.pointee = .noDataNow; return nil }
            consumed = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, output.frameLength > 0, let samples = output.int16ChannelData?[0] else { return nil }
        let count = Int(output.frameLength)
        var peak: Int32 = 0
        for i in 0..<count { peak = max(peak, abs(Int32(samples[i]))) }
        return Chunk(data: Data(bytes: samples, count: count * 2), level: min(1, Double(peak) / 12_000))
    }
}
