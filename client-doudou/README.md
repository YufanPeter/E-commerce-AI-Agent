# 兜兜 Doudou iOS 客户端

从 `client/AIShoppingGuide` 复制出来的独立工程，换成了兜兜的名字、图标和配色，功能与接口不变。品牌素材见 `docs/brand/doudou`。

## 打开

```bash
open client-doudou/Doudou.xcodeproj
```

- 显示名：兜兜
- Bundle ID：`com.example.Doudou`，可与原 App 同时装在一台设备上
- 配色集中在 `Doudou/AppTheme.swift`
- 后端地址：环境变量 `BACKEND_BASE_URL`，未设置时模拟器默认 `http://127.0.0.1:8000`
