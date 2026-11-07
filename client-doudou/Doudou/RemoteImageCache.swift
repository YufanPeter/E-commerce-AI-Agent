import SwiftUI
import UIKit

enum RemoteImageCacheBootstrap {
    static func configureSharedURLCache() {
        URLCache.shared = URLCache(
            memoryCapacity: 50 * 1024 * 1024,
            diskCapacity: 200 * 1024 * 1024,
            diskPath: "product_remote_images"
        )
    }
}

actor RemoteImageCache {
    static let shared = RemoteImageCache()

    private let memoryCache = NSCache<NSURL, UIImage>()
    private let session: URLSession
    private var inflightTasks: [URL: Task<UIImage?, Never>] = [:]

    private init() {
        memoryCache.countLimit = 120
        memoryCache.totalCostLimit = 50 * 1024 * 1024

        let config = URLSessionConfiguration.default
        config.requestCachePolicy = .returnCacheDataElseLoad
        config.urlCache = URLCache.shared
        session = URLSession(configuration: config)
    }

    func image(for url: URL) async -> UIImage? {
        let cacheKey = url as NSURL
        if let cached = memoryCache.object(forKey: cacheKey) {
            return cached
        }

        if let existingTask = inflightTasks[url] {
            return await existingTask.value
        }

        let task = Task<UIImage?, Never> {
            await self.loadImage(from: url)
        }
        inflightTasks[url] = task
        let image = await task.value
        inflightTasks[url] = nil
        return image
    }

    private func loadImage(from url: URL) async -> UIImage? {
        let cacheKey = url as NSURL
        var request = URLRequest(url: url)
        request.cachePolicy = .returnCacheDataElseLoad

        if let cachedResponse = session.configuration.urlCache?.cachedResponse(for: request),
           let image = UIImage(data: cachedResponse.data) {
            memoryCache.setObject(image, forKey: cacheKey, cost: cachedResponse.data.count)
            return image
        }

        do {
            let (data, response) = try await session.data(for: request)
            guard let image = UIImage(data: data) else { return nil }
            let cachedResponse = CachedURLResponse(response: response, data: data)
            session.configuration.urlCache?.storeCachedResponse(cachedResponse, for: request)
            memoryCache.setObject(image, forKey: cacheKey, cost: data.count)
            return image
        } catch {
            return nil
        }
    }
}

struct ProductRemoteImage: View {
    let url: URL?
    let cornerRadius: CGFloat
    let placeholderIcon: String
    var contentMode: ContentMode = .fill

    @State private var image: UIImage?
    @State private var loadFailed = false

    var body: some View {
        Group {
            if let image {
                renderedImage(Image(uiImage: image))
                    .transition(.opacity)
            } else if loadFailed {
                placeholderBox { placeholderIconView }
            } else {
                placeholderBox { ProgressView().tint(AppTheme.primary) }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
        .task(id: url) {
            await loadImage()
        }
    }

    @ViewBuilder
    private func renderedImage(_ image: Image) -> some View {
        if contentMode == .fit {
            image
                .resizable()
                .scaledToFit()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.white)
        } else {
            image
                .resizable()
                .scaledToFill()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .clipped()
        }
    }

    @ViewBuilder
    private func placeholderBox<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        ZStack {
            background
            content()
        }
        .modifier(PlaceholderSizing(isFit: contentMode == .fit))
    }

    @ViewBuilder
    private var background: some View {
        if contentMode == .fit {
            Color.white
        } else {
            LinearGradient(colors: [AppTheme.softPurple, AppTheme.softBlue], startPoint: .topLeading, endPoint: .bottomTrailing)
        }
    }

    private var placeholderIconView: some View {
        Image(systemName: placeholderIcon)
            .font(.system(size: 34, weight: .semibold))
            .foregroundStyle(AppTheme.primary)
    }

    private func loadImage() async {
        image = nil
        loadFailed = false

        guard let url else {
            loadFailed = true
            return
        }

        if let cached = await RemoteImageCache.shared.image(for: url) {
            image = cached
        } else {
            loadFailed = true
        }
    }
}

private struct PlaceholderSizing: ViewModifier {
    let isFit: Bool

    func body(content: Content) -> some View {
        if isFit {
            content
                .aspectRatio(1, contentMode: .fit)
                .frame(maxWidth: .infinity)
        } else {
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}
