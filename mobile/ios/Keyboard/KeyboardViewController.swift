import UIKit

/// The keyboard cannot record audio itself. It asks the containing app to start
/// and stop dictation and inserts the segments the app publishes.
final class KeyboardViewController: UIInputViewController {
    private let micButton = UIButton(type: .system)
    private let statusLabel = UILabel()
    private let levelBar = UIProgressView(progressViewStyle: .bar)
    private let globeButton = KeyboardViewController.key(symbol: "globe")
    private var observers: [DarwinObserver] = []
    private var consumedSeq = 0
    private var backspaceTimer: Timer?

    private enum Palette {
        static let background = UIColor(red: 0x1e / 255, green: 0x1f / 255, blue: 0x22 / 255, alpha: 1)
        static let key = UIColor(red: 0x38 / 255, green: 0x3a / 255, blue: 0x40 / 255, alpha: 1)
        static let accent = UIColor(red: 0x58 / 255, green: 0x65 / 255, blue: 0xf2 / 255, alpha: 1)
        static let danger = UIColor(red: 0xf2 / 255, green: 0x3f / 255, blue: 0x43 / 255, alpha: 1)
        static let muted = UIColor(red: 0xb5 / 255, green: 0xba / 255, blue: 0xc1 / 255, alpha: 1)
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = Palette.background
        buildLayout()
        consumedSeq = DictationBridge.latestSeq  // never replay text from an earlier session
        observers = DictationBridge.observeUpdates { [weak self] in self?.refresh() }
        refresh()
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        if DictationBridge.state == .listening { DictationBridge.send(.stop) }
    }

    override func viewWillLayoutSubviews() {
        super.viewWillLayoutSubviews()
        globeButton.isHidden = !needsInputModeSwitchKey
    }

    // MARK: Layout

    private func buildLayout() {
        let height = view.heightAnchor.constraint(equalToConstant: 260)
        height.priority = .defaultHigh
        height.isActive = true

        var mic = UIButton.Configuration.filled()
        mic.image = UIImage(systemName: "mic.fill", withConfiguration: UIImage.SymbolConfiguration(pointSize: 30, weight: .semibold))
        mic.baseBackgroundColor = Palette.accent
        mic.cornerStyle = .capsule
        micButton.configuration = mic
        micButton.accessibilityLabel = "Start dictation"
        micButton.addTarget(self, action: #selector(micTapped), for: .touchUpInside)
        micButton.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([micButton.widthAnchor.constraint(equalToConstant: 96),
                                     micButton.heightAnchor.constraint(equalToConstant: 96)])

        statusLabel.textColor = Palette.muted
        statusLabel.font = .preferredFont(forTextStyle: .footnote)
        statusLabel.textAlignment = .center
        statusLabel.numberOfLines = 2
        levelBar.progressTintColor = Palette.accent
        levelBar.trackTintColor = Palette.key
        levelBar.translatesAutoresizingMaskIntoConstraints = false
        levelBar.widthAnchor.constraint(equalToConstant: 140).isActive = true

        let center = UIStackView(arrangedSubviews: [micButton, levelBar, statusLabel])
        center.axis = .vertical
        center.alignment = .center
        center.spacing = 10

        globeButton.addTarget(self, action: #selector(handleInputModeList(from:with:)), for: .allTouchEvents)
        let space = Self.key(title: "space")
        space.addTarget(self, action: #selector(spaceTapped), for: .touchUpInside)
        let backspace = Self.key(symbol: "delete.left")
        backspace.addTarget(self, action: #selector(backspaceDown), for: .touchDown)
        backspace.addTarget(self, action: #selector(backspaceUp), for: [.touchUpInside, .touchUpOutside, .touchCancel])
        let enter = Self.key(symbol: "return")
        enter.addTarget(self, action: #selector(enterTapped), for: .touchUpInside)

        let row = UIStackView(arrangedSubviews: [globeButton, space, backspace, enter])
        row.spacing = 6
        row.distribution = .fill
        for key in [globeButton, backspace, enter] { key.widthAnchor.constraint(equalToConstant: 56).isActive = true }

        let root = UIStackView(arrangedSubviews: [center, row])
        root.axis = .vertical
        root.spacing = 12
        root.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 8),
            root.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -8),
            root.topAnchor.constraint(equalTo: view.topAnchor, constant: 16),
            root.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -6),
            row.heightAnchor.constraint(equalToConstant: 46),
        ])
    }

    private static func key(title: String? = nil, symbol: String? = nil) -> UIButton {
        var config = UIButton.Configuration.filled()
        config.baseBackgroundColor = Palette.key
        config.baseForegroundColor = .white
        config.cornerStyle = .medium
        config.title = title
        if let symbol { config.image = UIImage(systemName: symbol) }
        let button = UIButton(configuration: config)
        button.accessibilityLabel = title ?? symbol
        return button
    }

    // MARK: State

    private func refresh() {
        insertNewSegments()
        let state = DictationBridge.state
        let listening = state == .listening
        var config = micButton.configuration
        config?.image = UIImage(systemName: listening ? "stop.fill" : "mic.fill",
                                withConfiguration: UIImage.SymbolConfiguration(pointSize: 30, weight: .semibold))
        config?.baseBackgroundColor = listening ? Palette.danger : Palette.accent
        micButton.configuration = config
        micButton.accessibilityLabel = listening ? "Stop dictation" : "Start dictation"
        levelBar.isHidden = !listening
        levelBar.progress = Float(DictationBridge.level)

        if !hasFullAccess {
            statusLabel.text = "Turn on Allow Full Access in Settings → Keyboards → Voice Keyboard"
        } else if !SharedStore.isPaired {
            statusLabel.text = "Open the Voice Keyboard app to pair"
        } else if state == .idle || DictationBridge.message.isEmpty {
            statusLabel.text = listening ? "Listening…" : "Tap to dictate"
        } else {
            statusLabel.text = DictationBridge.message
        }
    }

    private func insertNewSegments() {
        for segment in DictationBridge.segments where segment.seq > consumedSeq {
            textDocumentProxy.insertText(segment.text)
            consumedSeq = segment.seq
        }
    }

    // MARK: Actions

    @objc private func micTapped() {
        guard hasFullAccess, SharedStore.isPaired else { openApp(); return }
        switch DictationBridge.state {
        case .listening: DictationBridge.send(.stop)
        case .finishing: break
        default:
            consumedSeq = DictationBridge.latestSeq
            if DictationBridge.sessionAlive { DictationBridge.send(.start) } else { openApp(dictate: true) }
        }
    }

    @objc private func spaceTapped() { textDocumentProxy.insertText(" ") }
    @objc private func enterTapped() { textDocumentProxy.insertText("\n") }

    @objc private func backspaceDown() {
        textDocumentProxy.deleteBackward()
        backspaceTimer = Timer.scheduledTimer(withTimeInterval: 0.45, repeats: false) { [weak self] _ in
            self?.backspaceTimer = Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { _ in
                self?.textDocumentProxy.deleteBackward()
            }
        }
    }

    @objc private func backspaceUp() {
        backspaceTimer?.invalidate()
        backspaceTimer = nil
    }

    /// Keyboard extensions have no supported openURL API; walking the responder
    /// chain to the host UIApplication is the approach used by shipping voice
    /// keyboards. If it stops working, the status text tells the user what to do.
    private func openApp(dictate: Bool = false) {
        let url = URL(string: dictate ? "voicekeyboard://dictate" : "voicekeyboard://open")!
        var responder: UIResponder? = self
        let selector = NSSelectorFromString("openURL:options:completionHandler:")
        while let current = responder {
            if current.responds(to: selector), current !== self {
                _ = current.perform(selector, with: url, with: [:] as NSDictionary)
                return
            }
            responder = current.next
        }
        statusLabel.text = "Open the Voice Keyboard app to start the microphone"
    }
}
