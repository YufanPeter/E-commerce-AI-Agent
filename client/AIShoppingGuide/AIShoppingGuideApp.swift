import SwiftUI

@main
struct AIShoppingGuideApp: App {
    init() {
        RemoteImageCacheBootstrap.configureSharedURLCache()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
        }
    }
}
