import Foundation

/// Exchanges the install token from the /downloads "Pair this phone" link
/// (voicekeyboard://pair?server=…&token=…) for a long-lived device credential.
enum PairingClient {
    struct Response: Decodable { let client: String; let credential: String; let server: String }

    enum PairingError: LocalizedError {
        case badServer, revoked, http(Int)
        var errorDescription: String? {
            switch self {
            case .badServer: return "Enter a valid server URL."
            case .revoked: return "This pairing code was replaced. Open /downloads again and tap Pair this phone."
            case .http(let code): return "The server returned HTTP \(code)."
            }
        }
    }

    static func pair(server: String, token: String) async throws {
        let base = server.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: base + "/api/pair"), url.scheme?.hasPrefix("http") == true else {
            throw PairingError.badServer
        }
        let client = SharedStore.client ?? "ios-" + UUID().uuidString.prefix(8).lowercased()
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(["token": token.trimmingCharacters(in: .whitespaces), "client": client])
        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 { throw PairingError.revoked }
        guard status == 200 else { throw PairingError.http(status) }
        let result = try JSONDecoder().decode(Response.self, from: data)
        SharedStore.server = base
        SharedStore.client = result.client
        SharedStore.credential = result.credential
    }

    /// Parses voicekeyboard://pair?server=…&token=…
    static func parse(_ url: URL) -> (server: String, token: String)? {
        guard url.scheme == "voicekeyboard", url.host == "pair",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              let server = items.first(where: { $0.name == "server" })?.value,
              let token = items.first(where: { $0.name == "token" })?.value else { return nil }
        return (server, token)
    }
}
