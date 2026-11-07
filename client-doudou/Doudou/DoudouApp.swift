import SwiftUI

@main
struct DoudouApp: App {
    init() {
        RemoteImageCacheBootstrap.configureSharedURLCache()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
        }
    }
}
