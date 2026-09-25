import SwiftUI

@main
struct VoiceKeyboardApp: App {
    @StateObject private var engine = DictationEngine.shared
    @StateObject private var setup = SetupModel()

    var body: some Scene {
        WindowGroup {
            SetupView()
                .environmentObject(engine)
                .environmentObject(setup)
                .onOpenURL { url in handle(url) }
        }
    }

    /// voicekeyboard://pair?…   pairing link from /downloads
    /// voicekeyboard://dictate  opened by the keyboard when no session is running
    private func handle(_ url: URL) {
        if let pairing = PairingClient.parse(url) {
            setup.pair(server: pairing.server, token: pairing.token)
        } else if url.host == "dictate" {
            engine.startDictation()
            setup.showReturnHint = engine.dictating
        }
    }
}
