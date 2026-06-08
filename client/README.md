# AIShoppingGuide iOS Client

这是一个 SwiftUI 原型客户端，用于 RAG 多模态电商智能导购 AI Agent。

## 包含页面

- 导购对话页：首屏聊天、示例问题、历史半屏弹窗、底部输入栏、多模态上传浮层、商品推荐卡片。
- 商品详情页：商品图占位、价格、标题、AI 推荐理由、标签和加入购物车。
- 购物车页：商品列表、删除、合计和结算入口。
- Preference 页：预算、肤质、避开酒精、轻量偏好和关注品类。

## 打开方式

用 Xcode 打开：

```bash
open client/AIShoppingGuide.xcodeproj
```

## 开发热刷新

- **后端**：`./scripts/dev.sh`（Python 自动重载）
- **iOS UI**：Xcode Canvas → `GuideView.swift` 底部 `#Preview("推荐消息")`，保存即刷新

项目使用 iOS 17.0 作为 deployment target，UI 主要基于 SwiftUI 与系统 Material 实现 Liquid Glass 风格。
