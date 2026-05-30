# 多模态电商智能导购 AI Agent

RAG 多模态电商导购系统：Python 后端（FastAPI + ChromaDB + 豆包）+ SwiftUI iOS 客户端。

## 启动 Demo

### 1. 配置密钥

在仓库根目录创建 `.env`：

```bash
ARK_API_KEY=你的豆包API_Key
ARK_MODEL=你的endpoint_id

# 向量库 embedding（豆包多模态 embedding 接入点）
ARK_EMBEDDING_API_KEY=你的embedding_API_Key   # 不填则回退复用 ARK_API_KEY
ARK_EMBEDDING_MODEL=你的embedding_endpoint_id
# 图片存储网址
ARK_BASE_URL=https://ark.cn-*
```

### 2. 启动后端

```bash
./scripts/start_backend.sh
```

脚本自动建虚拟环境、装依赖并启动。访问 <http://127.0.0.1:8000/health> 返回 `{"status":"ok"}` 即成功。

> 首次启动需后台预热模型约 30–60 秒，首条对话稍等即可。

常用启动选项（环境变量）：

```bash
# 监听局域网，供 iOS 真机连接（手机与 Mac 同一 Wi-Fi）
HOST=0.0.0.0 ./scripts/start_backend.sh

# 禁用 CrossEncoder 精排（机器繁忙 / 模型加载慢时用，链路只走向量召回排序）
USE_RERANK=0 ./scripts/start_backend.sh

# 自定义端口
PORT=8000 ./scripts/start_backend.sh
```

> `USE_RERANK=0` 会跳过 reranker（约 280MB 模型）的加载与精排，召回质量略降但启动更快、低配机器更稳；默认启用精排。

也可以用 Docker 固定后端运行环境，便于换机器和后续云部署：

```bash
docker compose up --build
```

详细镜像构建、火山云镜像仓库推送和云服务器运行步骤见 [Backend Docker 部署说明](docs/backend_docker_deploy.md)。

### 3. 启动客户端

```bash
open client/AIShoppingGuide.xcodeproj
```

在 Xcode 选一个 iOS 模拟器点 ▶︎ Run。模拟器与 Mac 共享 localhost，默认连 `http://127.0.0.1:8000`，无需额外配置。

进入「导购」页输入例如「不要含酒精的防晒」，看到流式回复和商品卡片即联调成功。

> 环境要求：Python 3.10+、Xcode 15+。
