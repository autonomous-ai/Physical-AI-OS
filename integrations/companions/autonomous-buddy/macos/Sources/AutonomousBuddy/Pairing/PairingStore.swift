import Foundation

struct PairingRecord: Codable, Equatable {
    let buddyID: String
    let deviceHost: String
    let token: String
    let pairedAt: Date
}

enum PairingStoreError: LocalizedError {
    case io(String)
    case invalidData

    var errorDescription: String? {
        switch self {
        case .io(let msg): return "Pairing store I/O: \(msg)"
        case .invalidData: return "Stored pairing record was unreadable"
        }
    }
}

// File-backed (~/Library/Application Support/AutonomousBuddy/pairing.json, mode 0600).
// Keychain prompted unapprovably for ad-hoc-signed builds on Ventura.
final class PairingStore {
    private let fileURL: URL

    init() {
        let fm = FileManager.default
        let baseDir = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            .appendingPathComponent("AutonomousBuddy", isDirectory: true)
        try? fm.createDirectory(at: baseDir, withIntermediateDirectories: true)
        self.fileURL = baseDir.appendingPathComponent("pairing.json")
    }

    func save(_ record: PairingRecord) throws {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let data: Data
        do { data = try encoder.encode(record) } catch { throw PairingStoreError.io(error.localizedDescription) }
        do {
            try data.write(to: fileURL, options: [.atomic])
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fileURL.path)
        } catch {
            throw PairingStoreError.io(error.localizedDescription)
        }
    }

    func load() throws -> PairingRecord? {
        let fm = FileManager.default
        guard fm.fileExists(atPath: fileURL.path) else { return nil }
        let data: Data
        do { data = try Data(contentsOf: fileURL) } catch { throw PairingStoreError.io(error.localizedDescription) }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        do { return try decoder.decode(PairingRecord.self, from: data) }
        catch { throw PairingStoreError.invalidData }
    }

    func clear() throws {
        let fm = FileManager.default
        guard fm.fileExists(atPath: fileURL.path) else { return }
        do { try fm.removeItem(at: fileURL) }
        catch { throw PairingStoreError.io(error.localizedDescription) }
    }
}
