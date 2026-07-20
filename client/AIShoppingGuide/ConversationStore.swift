import Foundation

/// Local persistence store for conversation history.
///
/// Conversation history needs the complete transcript, including text and product cards, which only the client retains.
/// The backend `AgentSession` keeps only a truncated text window and loses it on restart, so history is serialized to
/// `Documents/conversations.json` on the client and survives both app and backend restarts.
final class ConversationStore: ObservableObject {
    /// Sorted by `updatedAt` in descending order, with the newest conversation first.
    @Published private(set) var conversations: [Conversation] = []

    private let fileURL: URL
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    init(filename: String = "conversations.json") {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        self.fileURL = documents.appendingPathComponent(filename)

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        self.encoder = encoder

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        self.decoder = decoder

        load()
    }

    /// Inserts or updates a conversation. Conversations without user messages are not stored.
    func upsert(_ conversation: Conversation) {
        guard conversation.hasUserMessage else { return }
        var updated = conversation
        updated.updatedAt = Date()
        if let index = conversations.firstIndex(where: { $0.id == updated.id }) {
            conversations[index] = updated
        } else {
            conversations.append(updated)
        }
        sortAndPersist()
    }

    func delete(_ conversation: Conversation) {
        conversations.removeAll { $0.id == conversation.id }
        persist()
    }

    func rename(_ conversation: Conversation, to title: String) {
        let trimmedTitle = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedTitle.isEmpty,
              let index = conversations.firstIndex(where: { $0.id == conversation.id })
        else { return }
        conversations[index].title = trimmedTitle
        conversations[index].updatedAt = Date()
        sortAndPersist()
    }

    func clearAll() {
        conversations.removeAll()
        persist()
    }

    func delete(at offsets: IndexSet) {
        conversations.remove(atOffsets: offsets)
        persist()
    }

    func conversation(by id: UUID) -> Conversation? {
        conversations.first { $0.id == id }
    }

    // MARK: - Persistence

    private func load() {
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return }
        do {
            let data = try Data(contentsOf: fileURL)
            let decoded = try decoder.decode([Conversation].self, from: data)
            conversations = decoded.sorted { $0.updatedAt > $1.updatedAt }
        } catch {
            // A corrupt or incompatible history file must not prevent app startup; discard it.
            conversations = []
        }
    }

    private func sortAndPersist() {
        conversations.sort { $0.updatedAt > $1.updatedAt }
        persist()
    }

    private func persist() {
        do {
            let data = try encoder.encode(conversations)
            try data.write(to: fileURL, options: .atomic)
        } catch {
            // Persistence failures affect only the next launch and must not interrupt the current conversation.
        }
    }
}
