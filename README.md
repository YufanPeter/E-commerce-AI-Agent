# 多模态电商智能导购 AI Agent

本项目旨在通过 AI 技术，将传统的“展示型电商”升级为“交互型导购”。

通过引入大语言模型（LLM）与检索增强生成（RAG）技术，系统能够理解用户的模糊意图与多模态输入（如语音、图片），实现从个性化推荐、商品对比到购物车管理的自然对话闭环，为用户提供更加智能、连贯的购物体验。

技术栈：**SwiftUI（iOS 客户端） + FastAPI（异步网关） + ChromaDB（向量检索） + SQLite（事实库） + 豆包 Ark / 智谱 Rerank（大模型）**。

## ✨ 核心亮点与功能特性

### 📱 1. 原生客户端体验 (SwiftUI)
- **对话式流式渲染**：支持逐字 SSE 流式响应，对话内无缝嵌入交互式商品卡片。
- **多模态交互**：集成原生语音识别，支持拍照或相册选图进行“以图搜货”。
- **流畅动效**：打磨卡片弹入、购物车同步等原生动画，提供商业级使用体验。

### 🧠 2. 智能 Agent 与意图管理
- **意图路由编排**：`router → tool → composer` 三段式流水线，LLM 仅负责意图分类，固定流水线负责执行，控制力强、可观测。
- **渐进式需求收敛**：精准识别模糊意图，通过主动反问（Clarify）引导用户细化需求。
- **排他性过滤**：支持“不要含酒精”等否定语义解析，在检索条件中精准排除。
- **智能对比决策**：自动提取商品关键维度（成分、价格等）生成结构化对比表。
- **购物车全接管**：支持通过自然语言执行加购、改数量、改规格、删除等 CRUD 操作。
- **跨会话记忆**：从对话中抽取用户偏好（预算 / 肤质 / 关注品类等）并支持撤销。

### 🔍 3. 多模态 RAG 检索引擎
- **混合检索与精排**：基于 ChromaDB 向量检索，结合 Zhipu 云端 Rerank 提升召回精度。
- **图文双模态理解**：商品图接入视觉特征提取，完美支持图片搜索场景。
- **事实一致性保障**：价格与库存等关键参数强依赖 SQLite，杜绝大模型“幻觉”。

---

## 🏗 系统架构

```
┌─────────────┐   REST + SSE    ┌──────────────────────────────────────────┐
│  iOS 客户端  │ ───────────────▶│              FastAPI 网关 (api/)            │
│  (SwiftUI)  │ ◀─────────────── │   /chat /chat/stream /cart /compare ...    │
└─────────────┘                 └──────────────────┬─────────────────────────┘
                                                   │
                                  ┌────────────────▼─────────────────┐
                                  │        Agent 编排 (agent/)         │
                                  │  intent_router → tools → composer  │
                                  └───┬─────────────┬──────────────┬──┘
                                      │             │              │
                              ┌───────▼──────┐ ┌────▼─────┐ ┌──────▼───────┐
                              │ search/ + rag/│ │  store/  │ │    llm/      │
                              │ ChromaDB 检索 │ │  SQLite  │ │ 豆包/智谱 API │
                              │  + 智谱 Rerank│ │ 事实/购物车│ │   统一封装    │
                              └──────────────┘ └──────────┘ └──────────────┘
```

- **前端 (Client)**：SwiftUI 原生开发，通过 REST API 与 SSE 长连接接管对话流、状态管理（购物车 / 会话）及多模态输入采集。
- **后端网关 (API)**：FastAPI 构建高并发异步服务，统一封装大模型调用、检索逻辑和事实数据 CRUD。
- **编排层 (Agent)**：意图路由（function calling）分发到 `Recommend / Refine / Compare / ProductDetail / Cart / Clarify / Fallback` 等工具，再由 Composer 生成话术。
- **数据层 (Store & RAG)**：
  - **向量库**：ChromaDB 存储商品语义证据 chunk 与视觉特征索引。
  - **事实库**：SQLite 持久化商品信息、SKU 价格、会话历史与购物车状态。

---

## 🚀 快速启动

有两种方式运行。**只想体验 App，选方式 A 最快**：iOS 直连已部署的火山云后端，无需在本地启动任何服务、也无需配置 API 密钥。需要改后端代码或离线建库时，再用方式 B 在本地自建后端。

### 方式 A：iOS 直连火山云服务器

云服务器已部署后端并配好全部密钥，本地**无需启动后端、无需 `.env`**。

**环境要求：Xcode 15+（iOS 17.0 deployment target）**

1. 打开项目：

   ```bash
   open client/AIShoppingGuide.xcodeproj
   ```

2. 指定后端地址：Xcode 菜单 `Product` → `Scheme` → `Edit Scheme...` → `Run` → `Arguments` → `Environment Variables`，新增并勾选：

   ```text
   BACKEND_BASE_URL=http://118.196.64.197:8000
   ```

   该配置会写入共享 Scheme 文件 `client/AIShoppingGuide.xcodeproj/xcshareddata/xcschemes/AIShoppingGuide.xcscheme`，也可直接编辑该文件，在 `LaunchAction` 下加入：

   ```xml
   <EnvironmentVariables>
      <EnvironmentVariable
         key = "BACKEND_BASE_URL"
         value = "http://118.196.64.197:8000"
         isEnabled = "YES">
      </EnvironmentVariable>
   </EnvironmentVariables>
   ```

3. 选择一个 iOS 模拟器点击 ▶︎ Run。进入「导购」页输入例如「不要含酒精的防晒」，看到流式回复和商品卡片即说明联调成功。

> 改动环境变量后需停止旧的 App 进程并重新 Run 才会生效。若云服务器未运行，请改用方式 B。

### 方式 B：本地自建后端

需要修改后端、调试或离线建库时使用。

**1. 配置环境变量** —— 在仓库根目录创建 `.env` 文件，填入相关 API 密钥（仅本地运行后端时需要；连云服务器走方式 A 无需此步）：

```bash
# 豆包大模型（对话与意图识别）
ARK_API_KEY=你的豆包API_Key
ARK_MODEL=你的endpoint_id
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3

# 向量库 embedding（豆包多模态 embedding 接入点）
ARK_EMBEDDING_API_KEY=你的embedding_API_Key   # 不填则回退复用 ARK_API_KEY
ARK_EMBEDDING_MODEL=你的embedding_endpoint_id

# 智谱云端 rerank (用于高精度二次重排)
ZHIPU_API_KEY=你的智谱APIKey
RERANK_MODEL=rerank
RERANK_BASE_URL=https://open.bigmodel.cn/api/paas/v4/rerank
```

**2. 启动后端服务**（环境要求：Python 3.10+）：

```bash
./scripts/start_backend.sh
```

> 脚本会自动创建虚拟环境 `.venv`、安装依赖并启动服务。访问 `http://127.0.0.1:8000/health` 返回 `{"status":"ok"}` 即成功。首次启动需后台预热模型约 30–60 秒，首条对话稍等即可。

| 脚本 | 作用 |
| --- | --- |
| `./scripts/start_backend.sh` | 建虚拟环境、装依赖并启动后端 |
| `./scripts/stop_backend.sh` | 停止后端服务 |
| `./scripts/restart_backend.sh` | 重启后端服务 |
| `./scripts/dev.sh` | 开发模式（uvicorn 自动重载） |

**3. 启动 iOS 客户端**：用 Xcode 打开 `client/AIShoppingGuide.xcodeproj`，选择 iOS 模拟器点击 ▶︎ Run。模拟器与 Mac 共享 localhost，默认连接 `http://127.0.0.1:8000`，**无需配置 `BACKEND_BASE_URL`**。

---

## 🔌 主要 API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/chat` | 非流式对话，一次性返回 JSON |
| `POST` | `/chat/stream` | SSE 流式对话（事件：`meta` / `tool_result` / `token` / `done` / `error`） |
| `GET`  | `/health` | 健康检查 |
| `GET`  | `/warmup` | 预热模型与检索器 |
| `POST` | `/compare` | 多商品结构化对比 |
| `GET`  | `/cart` · `POST /cart/mutate` · `POST /cart/reset` | 购物车查询 / 变更 / 重置 |
| `GET/PUT` | `/preferences/{user_id}` · `POST /preferences/{user_id}/undo` | 用户偏好读写与撤销 |
| `POST` | `/sessions/{session_id}/reset` | 重置会话 |
| `GET`  | `/suggestions` · `POST /title` | 推荐问法 / 会话标题生成 |
| `GET`  | `/products` · `GET /products/{id}` · `POST /products/{id}/pitch` | 商品批量 / 详情 / 卖点生成 |

---

## 📂 代码结构与规范

### 后端 (Python + FastAPI + ChromaDB)
```text
backend/
├── api/                # FastAPI 路由接入层
│   ├── main.py         # 核心启动入口、SSE 流式下发、会话/购物车/偏好端点
│   └── products.py     # 商品详情、批量查询与卖点生成接口
├── agent/              # AI Agent 智能体核心编排层
│   ├── orchestrator.py # 调度器 (router → tool → composer 三段式流水线)
│   ├── intent_router.py# 意图识别 (LLM function calling 路由至特定工具)
│   ├── composer.py     # LLM 话术生成与流式输出
│   ├── memory_extractor.py # 从对话抽取用户偏好(跨会话记忆)
│   └── tools/          # 工具库 (recommend/refine/compare/product_detail/cart/clarify/fallback 等)
├── search/             # 查询理解与条件构建引擎
│   ├── search_service.py      # 混合检索服务入口
│   ├── query_understanding.py # 自然语言 → 结构化查询(类目/预算/属性)
│   ├── query_decomposer.py    # 复合查询拆解
│   ├── where_builder.py       # ChromaDB where 过滤条件构建
│   └── visual_index.py        # 以图搜货视觉索引
├── rag/                # 向量数据库与检索增强模块
│   ├── retriever.py    # ChromaDB 向量召回执行器
│   ├── reranker.py     # 智谱云端精排接入层
│   ├── chroma_store.py # Chroma 集合封装
│   └── build_*.py      # 离线数据向量化建库脚本(文本/图像/对比索引)
├── store/              # 事实数据与持久化层 (SQLite)
│   ├── product_store.py     # 商品信息与 SKU 价格强一致性查询
│   ├── cart_store.py        # 购物车状态管理
│   ├── session_store.py     # 多轮会话历史留存
│   ├── user_memory_store.py # 用户偏好(核心记忆)存储
│   └── import_*.py          # 商品数据 / 图片清单导入脚本
└── llm/                # 大模型客户端统一封装
    ├── client.py       # 豆包 Ark / embedding / 智谱 rerank 客户端单例
    └── vision.py       # 多模态视觉理解
```

### 客户端 (SwiftUI)
```text
client/AIShoppingGuide/
├── AIShoppingGuideApp.swift # iOS 应用入口
├── RootView.swift           # 全局 Tab 导航与状态分发
├── GuideView.swift          # AI 导购主聊天流界面 (核心页)
├── CartView.swift           # 购物车管理页
├── ProductDetailView.swift  # 商品图文详情页
├── ComparisonView.swift     # 智能对比决策页
├── PreferenceView.swift     # 偏好设置页 (预算/肤质/避开酒精/关注品类)
├── FrontendServices.swift   # SSE 长连接解析与 REST API 客户端
├── ConversationStore.swift  # 本地聊天记录与离线数据恢复
├── RemoteImageCache.swift   # 远程商品图缓存
├── Models.swift / APIModels.swift # 数据实体(与后端 JSON 严格映射)
├── AppTheme.swift           # Liquid Glass 风格主题
└── MockFrontendServices.swift / PreviewFixtures.swift # 预览与 Mock 数据
```

---

## 🗂 数据与文档

- **商品数据**：`data/` 下按四大类目组织（`1_美妆护肤` / `2_数码电子` / `3_服饰运动` / `4_食品生活`），通过 `store/import_*.py` 与 `rag/build_*.py` 导入并建库。
- **离线建库**（商品数据变更后执行）：

  ```bash
  cd backend
  source ../.venv/bin/activate
  python -m store.import_product_data --reset
  python -m store.import_image_manifest
  python -m rag.build_chroma --reset
  ```

- **设计文档**：详见 [docs/系统设计文档.md](docs/系统设计文档.md)，涵盖系统架构、Agent 编排、多模态 RAG 检索、数据存储、API 接口与客户端设计。

更多后端数据层细节见 [backend/README.md](backend/README.md)，客户端细节见 [client/README.md](client/README.md)。
