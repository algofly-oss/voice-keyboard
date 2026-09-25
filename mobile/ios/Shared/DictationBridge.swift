import Foundation

/// Keyboard extensions cannot use the microphone, so the containing app records
/// and streams while the keyboard stays on screen. They talk through Darwin
/// notifications (a wake-up signal with no payload) plus App Group defaults
/// (the payload).
enum DictationBridge {
    enum Command: String { case start, stop, cancel }
    enum State: String { case idle, listening, finishing, error }

    struct Segment: Codable { let seq: Int; let text: String }

    private static let prefix = "ai.algofly.voicekeyboard."
    private static var defaults: UserDefaults { SharedStore.defaults }

    // MARK: Keyboard -> app

    static func send(_ command: Command) {
        post(prefix + "command." + command.rawValue)
    }

    static func observeCommands(_ handler: @escaping (Command) -> Void) -> [DarwinObserver] {
        [Command.start, .stop, .cancel].map { command in
            DarwinObserver(name: prefix + "command." + command.rawValue) { handler(command) }
        }
    }

    // MARK: App -> keyboard

    /// The app refreshes this while its audio session is alive; the keyboard
    /// treats a stale heartbeat as "the app is not running".
    static func heartbeat() { defaults.set(Date().timeIntervalSince1970, forKey: "heartbeat") }
    static var sessionAlive: Bool {
        Date().timeIntervalSince1970 - defaults.double(forKey: "heartbeat") < 3
    }

    static var state: State {
        State(rawValue: defaults.string(forKey: "state") ?? "") ?? .idle
    }
    static var message: String { defaults.string(forKey: "message") ?? "" }
    static var level: Double { defaults.double(forKey: "level") }

    static func publish(state: State, message: String = "") {
        defaults.set(state.rawValue, forKey: "state")
        defaults.set(message, forKey: "message")
        post(prefix + "state")
    }

    static func publish(level: Double) {
        defaults.set(level, forKey: "level")
        post(prefix + "level")
    }

    /// Segments are appended with increasing sequence numbers; the keyboard
    /// inserts everything newer than the last one it consumed.
    static func publish(segment text: String) {
        var list = segments
        let seq = (list.last?.seq ?? defaults.integer(forKey: "segmentSeq")) + 1
        list.append(Segment(seq: seq, text: text))
        defaults.set(seq, forKey: "segmentSeq")
        defaults.set(try? JSONEncoder().encode(Array(list.suffix(50))), forKey: "segments")
        post(prefix + "segment")
    }

    static var segments: [Segment] {
        guard let data = defaults.data(forKey: "segments") else { return [] }
        return (try? JSONDecoder().decode([Segment].self, from: data)) ?? []
    }
    static var latestSeq: Int { defaults.integer(forKey: "segmentSeq") }

    static func observeUpdates(_ handler: @escaping () -> Void) -> [DarwinObserver] {
        ["state", "level", "segment"].map { DarwinObserver(name: prefix + $0, handler: handler) }
    }

    private static func post(_ name: String) {
        CFNotificationCenterPostNotification(CFNotificationCenterGetDarwinNotifyCenter(),
                                             CFNotificationName(name as CFString), nil, nil, true)
    }
}

/// Owns one Darwin notification subscription; removed on deinit.
final class DarwinObserver {
    private let name: String
    private let handler: () -> Void

    init(name: String, handler: @escaping () -> Void) {
        self.name = name
        self.handler = handler
        let observer = Unmanaged.passUnretained(self).toOpaque()
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), observer, { _, observer, _, _, _ in
            guard let observer else { return }
            let me = Unmanaged<DarwinObserver>.fromOpaque(observer).takeUnretainedValue()
            DispatchQueue.main.async { me.handler() }
        }, name as CFString, nil, .deliverImmediately)
    }

    deinit {
        CFNotificationCenterRemoveObserver(CFNotificationCenterGetDarwinNotifyCenter(),
                                           Unmanaged.passUnretained(self).toOpaque(),
                                           CFNotificationName(name as CFString), nil)
    }
}
