import AVFoundation
import SwiftUI

@MainActor
final class SetupModel: ObservableObject {
    @Published var paired = SharedStore.isPaired
    @Published var server = SharedStore.server ?? ""
    @Published var code = ""
    @Published var pairing = false
    @Published var pairError: String?
    @Published var micGranted = AVAudioApplication.shared.recordPermission == .granted
    @Published var language = SharedStore.language { didSet { SharedStore.language = language } }
    @Published var showReturnHint = false

    func pair(server: String, token: String) {
        self.server = server
        pairing = true
        pairError = nil
        Task {
            do {
                try await PairingClient.pair(server: server, token: token)
                paired = true
                code = ""
            } catch {
                pairError = error.localizedDescription
            }
            pairing = false
        }
    }

    func requestMic() {
        AVAudioApplication.requestRecordPermission { granted in
            Task { @MainActor in self.micGranted = granted }
        }
    }

    func unpair() {
        SharedStore.forget()
        paired = false
    }
}

struct SetupView: View {
    @EnvironmentObject private var engine: DictationEngine
    @EnvironmentObject private var model: SetupModel
    @Environment(\.openURL) private var openURL

    private let languages = [("", "Detect automatically"), ("en", "English"), ("hi", "Hindi"), ("es", "Spanish"),
                             ("fr", "French"), ("de", "German"), ("it", "Italian"), ("pt", "Portuguese"),
                             ("ja", "Japanese"), ("ko", "Korean"), ("zh", "Chinese")]

    var body: some View {
        NavigationStack {
            List {
                if model.showReturnHint && engine.dictating {
                    Section {
                        Label("Listening. Swipe back to your app — text appears where the keyboard is.", systemImage: "waveform")
                            .foregroundStyle(.green)
                    }
                }

                Section {
                    step(1, "Pair with your server", done: model.paired)
                    if model.paired {
                        LabeledContent("Server", value: SharedStore.server ?? "")
                        Button("Unpair", role: .destructive) { model.unpair() }
                    } else {
                        Text("On this phone, open your server's /downloads page and tap **Pair this phone**. Or enter the details:")
                            .font(.footnote).foregroundStyle(.secondary)
                        TextField("https://voice.example.com", text: $model.server)
                            .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        TextField("Pairing code", text: $model.code)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        Button(model.pairing ? "Pairing…" : "Pair") { model.pair(server: model.server, token: model.code) }
                            .disabled(model.pairing || model.server.isEmpty || model.code.isEmpty)
                        if let error = model.pairError { Text(error).font(.footnote).foregroundStyle(.red) }
                    }
                }

                Section {
                    step(2, "Allow the microphone", done: model.micGranted)
                    if !model.micGranted { Button("Allow microphone") { model.requestMic() } }
                }

                Section {
                    step(3, "Add the keyboard", done: false)
                    Text("Settings → General → Keyboard → Keyboards → Add New Keyboard → **Voice Keyboard**, then turn on **Allow Full Access** so it can reach the app and your server.")
                        .font(.footnote).foregroundStyle(.secondary)
                    Button("Open Settings") { openURL(URL(string: UIApplication.openSettingsURLString)!) }
                }

                Section {
                    step(4, "Dictate", done: false)
                    Text("In any app, switch to Voice Keyboard with 🌐 and tap the microphone. The first tap opens this app to start the microphone; swipe back and keep talking. The session stays ready for five minutes.")
                        .font(.footnote).foregroundStyle(.secondary)
                    Toggle("Microphone session", isOn: Binding(
                        get: { engine.sessionActive },
                        set: { on in if on { try? engine.startSession() } else { engine.endSession() } }))
                }

                Section("Language") {
                    Picker("Language", selection: $model.language) {
                        ForEach(languages, id: \.0) { Text($0.1).tag($0.0) }
                    }
                }

                if let error = engine.lastError {
                    Section { Text(error).foregroundStyle(.red) }
                }
            }
            .navigationTitle("Voice Keyboard")
        }
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.willEnterForegroundNotification)) { _ in
            model.micGranted = AVAudioApplication.shared.recordPermission == .granted
        }
    }

    private func step(_ number: Int, _ title: String, done: Bool) -> some View {
        HStack(spacing: 12) {
            Image(systemName: done ? "checkmark.circle.fill" : "\(number).circle")
                .foregroundStyle(done ? .green : .accentColor).font(.title3)
            Text(title).font(.headline)
        }
    }
}
