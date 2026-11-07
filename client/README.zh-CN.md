# AIShoppingGuide iOS 客户端

[English](README.md) | [简体中文](README.zh-CN.md)

<p align="center">
  <img alt="ByteDance 优秀项目" src="https://img.shields.io/badge/ByteDance-优秀项目-F5A623?style=flat&amp;logo=bytedance&amp;logoColor=white">
</p>

<p align="center">
  <img src="../bytedance.svg" height="40">
</p>

<h3 align="center">
  优秀项目奖
</h3>

该客户端是 CartPilot 的原生 SwiftUI 界面，支持流式导购对话、商品卡片、拍照找货、商品对比、购物车管理、偏好设置和本地会话历史。

## 环境要求

- Xcode 15 或更高版本
- iOS 17 或更高版本
- 已启动的 CartPilot 后端

## 运行

在仓库根目录启动后端：

```bash
./scripts/start_backend.sh
```

打开 Xcode 项目：

```bash
open client/AIShoppingGuide.xcodeproj
```

iOS 模拟器默认连接 `http://127.0.0.1:8000`。也可以在 **Product → Scheme → Edit Scheme → Run → Arguments** 中设置：

```text
BACKEND_BASE_URL=http://127.0.0.1:8000
```

选择 iPhone 模拟器并点击 **Run**。

## 主要页面

- **导购：** 流式对话、库存驱动的推荐词、语音输入、相机/相册上传和商品卡片。
- **商品详情：** 商品事实、AI 推荐理由、FAQ、评价和加购操作。
- **商品对比：** 并排对比 2–3 件商品的结构化信息。
- **购物车：** 持久化商品、数量、总价和结算。
- **偏好：** 预算及轻量购物偏好。

完整对话记录保存在客户端本地；商品、SKU、购物车和会话事实来自后端。

## 开发

后端自动重载：

```bash
./scripts/dev.sh
```

调试 UI 时，在 Xcode Canvas 中打开 `GuideView.swift`，使用文件底部的 Preview；保存 SwiftUI 修改后会自动刷新。

项目 deployment target 为 iOS 17，UI 主要使用 SwiftUI 与系统 Material。
