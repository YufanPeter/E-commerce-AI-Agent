# 多模态电商智能导购 AI Agent

RAG 多模态电商导购系统：Python 后端（FastAPI + ChromaDB + 豆包）+ SwiftUI iOS 客户端。

## 启动 Demo

### 1. 配置密钥

在仓库根目录创建 `.env`：

```bash
ARK_API_KEY=你的豆包API_Key
ARK_MODEL=你的endpoint_id
```

### 2. 启动后端

```bash
./scripts/start_backend.sh
```

脚本自动建虚拟环境、装依赖并启动。访问 <http://127.0.0.1:8000/health> 返回 `{"status":"ok"}` 即成功。

> 首次启动需后台预热模型约 30–60 秒，首条对话稍等即可。

### 3. 启动客户端

```bash
open client/AIShoppingGuide.xcodeproj
```

在 Xcode 选一个 iOS 模拟器点 ▶︎ Run。模拟器与 Mac 共享 localhost，默认连 `http://127.0.0.1:8000`，无需额外配置。

进入「导购」页输入例如「不要含酒精的防晒」，看到流式回复和商品卡片即联调成功。

> 环境要求：Python 3.10+、Xcode 15+。
