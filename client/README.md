# AIShoppingGuide iOS Client

[English](README.md) | [简体中文](README.zh-CN.md)

<p align="center">
  <img alt="ByteDance Outstanding Project" src="https://img.shields.io/badge/ByteDance-Outstanding_Project-F5A623?style=flat&amp;logo=bytedance&amp;logoColor=white">
</p>

<p align="center">
  <img src="../bytedance.svg" height="40">
</p>

<h3 align="center">
  Outstanding Project Award
</h3>

The client is a native SwiftUI interface for CartPilot. It supports streaming shopping conversations, product cards, visual search, product comparison, cart management, preferences, and local conversation history.

## Requirements

- Xcode 15 or later
- iOS 17 or later
- A running CartPilot backend

## Run

Start the backend from the repository root:

```bash
./scripts/start_backend.sh
```

Open the Xcode project:

```bash
open client/AIShoppingGuide.xcodeproj
```

The iOS Simulator uses `http://127.0.0.1:8000` by default. You can override it in **Product → Scheme → Edit Scheme → Run → Arguments** with:

```text
BACKEND_BASE_URL=http://127.0.0.1:8000
```

Choose an iPhone simulator and press **Run**.

## Main screens

- **Guide:** streaming chat, inventory-backed suggestions, voice input, camera/photo upload, and product cards.
- **Product detail:** structured product facts, AI-generated pitch, FAQs, reviews, and cart actions.
- **Comparison:** side-by-side structured comparison of two or three products.
- **Cart:** persistent cart lines, quantities, totals, and checkout.
- **Preferences:** budget and lightweight shopping preferences.

Conversation transcripts are stored locally. Product, SKU, cart, and session facts come from the backend.

## Development

Run the backend with automatic reload:

```bash
./scripts/dev.sh
```

For UI iteration, open `GuideView.swift` in Xcode Canvas and use the previews at the bottom of the file. Saving SwiftUI changes refreshes the preview.

The project targets iOS 17 and uses SwiftUI plus system materials for its visual style.
