import Foundation
import Security

/// Settings and state shared between the containing app and the keyboard
/// extension through the App Group. The credential lives in the shared keychain.
enum SharedStore {
    static let defaultAppGroup = "group.ai.algofly.voicekeyboard"

    /// Sideloading tools (SideStore, AltStore) re-sign with the user's team and
    /// usually rename bundle IDs and App Groups, e.g. by appending the team ID.
    /// Pick whichever candidate this build is actually entitled to.
    static let appGroup: String = {
        var appID = Bundle.main.bundleIdentifier ?? ""
        if Bundle.main.bundlePath.hasSuffix(".appex") {
            appID = appID.components(separatedBy: ".").dropLast().joined(separator: ".")  // drop ".keyboard"
        }
        let candidates = [Bundle.main.object(forInfoDictionaryKey: "VKAppGroup") as? String,
                          "group." + appID, defaultAppGroup].compactMap { $0 }
        return candidates.first {
            FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: $0) != nil
        } ?? defaultAppGroup
    }()
    static let defaults = UserDefaults(suiteName: appGroup) ?? .standard

    static var server: String? {
        get { defaults.string(forKey: "server") }
        set { defaults.set(newValue, forKey: "server") }
    }
    static var client: String? {
        get { defaults.string(forKey: "client") }
        set { defaults.set(newValue, forKey: "client") }
    }
    static var language: String {
        get { defaults.string(forKey: "language") ?? "" }
        set { defaults.set(newValue, forKey: "language") }
    }
    static var isPaired: Bool { server != nil && client != nil && credential != nil }

    // MARK: Keychain

    private static let service = "ai.algofly.voicekeyboard.credential"

    static var credential: String? {
        get {
            var query = baseQuery
            query[kSecReturnData as String] = true
            query[kSecMatchLimit as String] = kSecMatchLimitOne
            var item: CFTypeRef?
            guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
                  let data = item as? Data else { return nil }
            return String(data: data, encoding: .utf8)
        }
        set {
            SecItemDelete(baseQuery as CFDictionary)
            guard let value = newValue else { return }
            var item = baseQuery
            item[kSecValueData as String] = Data(value.utf8)
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
            SecItemAdd(item as CFDictionary, nil)
        }
    }

    private static var baseQuery: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccessGroup as String: appGroup]
    }

    static func forget() {
        credential = nil
        client = nil
        server = nil
    }
}
