import Foundation

/// 对话历史的本地持久化仓库。
///
/// 历史需要完整 transcript（文本 + 商品卡），只有前端持有完整渲染数据；后端
/// `AgentSession` 仅保留截断的原文滑窗且重启即丢。因此历史落在客户端：序列化为
/// `Documents/conversations.json`，App 重启或后端重启都不丢。
final class ConversationStore: ObservableObject {
    /// 按 `updatedAt` 倒序，最新的对话排在最前。
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

    /// 插入或更新一段对话；空白（无用户消息）对话不入库，避免历史里出现空条目。
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

    func delete(at offsets: IndexSet) {
        conversations.remove(atOffsets: offsets)
        persist()
    }

    /// 清空全部历史对话。
    func clearAll() {
        conversations.removeAll()
        persist()
    }

    /// 手动重命名一段对话；空标题忽略。
    func rename(_ conversation: Conversation, to newTitle: String) {
        let trimmed = newTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              let index = conversations.firstIndex(where: { $0.id == conversation.id }) else { return }
        conversations[index].title = trimmed
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
            // 损坏或不兼容的历史文件不应阻断 App 启动；丢弃即可。
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
            // 持久化失败仅影响下次启动的历史，不应中断当前会话。
        }
    }
}
