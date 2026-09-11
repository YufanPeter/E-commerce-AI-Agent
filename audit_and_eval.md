# CartPilot 审计与评测记录

本文件记录 `CartPilot_AI_PM_最终方案.md` 的执行证据。所有未真实运行的项目明确标为 `NOT RUN`，不从代码存在或上游说明推断实验效果。

## Step 0.1：固定上游版本

| 项目 | 固定值 |
|---|---|
| 上游仓库 | `https://github.com/YufanPeter/E-commerce-AI-Agent.git` |
| 上游分支 | `main` |
| 固定 commit | `361184104f40ff0add11315b684729ab76c27b95` |
| commit 时间 | `2026-07-20T14:17:27+08:00` |
| commit 作者 | `YufanPeter` |
| commit 标题 | `chore: move demo screenshot to assets/ folder` |
| 本地工作分支 | `codex/step0` |
| 上游许可证 | MIT，原 `LICENSE` 已保留 |
| 固定时间 | 2026-09-10（Asia/Shanghai） |

当前工作区直接基于上述提交检出，不使用浮动的 `main` 作为实验版本。后续若需要吸收上游改动，必须建立新的 manifest，不能静默更新本基线。

### 贡献边界

上游已有 Router → Tool → Composer、混合检索、SQLite 商品事实、评测脚本、100 个商品和 585 个 SKU。截至 Step 0 提交，本项目新增的是中文最终方案、版本/环境审计记录与安全配置模板；当时尚未产生个人评测数据、实验结果或业务优化代码。

## Step 0.2：环境与配置基线

机器可读快照见 `artifacts/manifests/step0_baseline.json`。

### 本机运行环境

| 项目 | 实测值 |
|---|---|
| 操作系统 | Windows 11，build `26200`，AMD64 |
| Python | `3.12.10` |
| Python 路径 | `C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe` |
| pip | `26.2.1` |
| Git | `2.54.0.windows.1` |
| Node / npm | `24.16.0` / `11.13.0`（本轮文本后端不依赖） |

上游声明支持 Python 3.10+，推荐 3.11 或 3.12；本机 Python 版本在其声明范围内。当前没有项目专用 `.venv`。

### 依赖状态

`backend/requirements.txt` 声明：

- `chromadb==1.5.9`
- `openai>=1.40.0`
- `python-dotenv>=1.0.0`
- `fastapi>=0.111.0`
- `uvicorn[standard]>=0.30.0`
- `httpx>=0.27`
- `pytest>=7.0`

本机全局环境仅检测到 `httpx==0.28.1` 与 `pytest==9.1.1`；`chromadb`、`openai`、`python-dotenv`、`fastapi`、`uvicorn` 未安装。全局包不是项目锁定环境，后续必须创建 `.venv` 后再安装并导出实际版本。

### 模型与参数

- 根目录 `.env`：不存在。
- Chat 模型、Embedding 模型、Rerank 模型：`NOT CONFIGURED`。
- 只检查了环境变量是否存在，未读取、保存或打印任何凭据值。
- `.env.example` 已提供安全模板；方案规定的文本实验默认值为 `USE_HYBRID=1`、`USE_RERANK=0`。
- Dockerfile 和 Compose 的上游默认值仍为 `USE_RERANK=1`。后续实际运行必须显式传入 `USE_RERANK=0` 并在日志/manifest 中核对构造参数，不能只假设模板生效。

### 数据版本

| 数据 | 数量 | 固定标识 |
|---|---:|---|
| 商品 JSON | 100 | Git tree `31a9aa580d9cdceaef84445033892f209e2bdc35` |
| SKU | 585 | 从 100 个商品 JSON 的 `skus` 数组实数汇总 |
| 上游评测记录 | 121 | Git tree `83a0a699a0314055dfe3f59048e64ca4641622b0` |

上游评测记录按文件为 Routing 50、Retrieval 40、Semantic Intent 24、Semantic Rerank 5、Negative 2。121 是五个文件的记录数之和，尚未跨文件去重，不能称为 121 个独立场景。各文件 SHA-256 已写入机器 manifest。

### 本地索引状态

- `backend/storage/ecommerce_agent.sqlite3`：缺失。
- `backend/storage/chroma/chroma.sqlite3`：缺失。
- 仓库包含三组 Chroma HNSW 二进制片段，以及 `compare_index.json`、`image_index.json`。

缺少 SQLite 商品库和 Chroma 元数据数据库意味着当前检出不能直接作为完整可用检索基线。选择并固定 embedding 后，必须先生成 SQLite，并使用独立、同一向量空间的 Chroma 索引完整重建；不能把现有零散片段视为已验证可用，也不能混用更换模型前后的向量。

### 当前检查结果

| 检查项 | 状态 | 原因 |
|---|---|---|
| 依赖安装 | `NOT RUN` | Step 0.1+0.2 不安装依赖 |
| SQLite 商品导入 | `NOT RUN` | 项目环境尚未建立 |
| Router 单次调用 | `NOT RUN` | Chat 模型未配置 |
| 文本向量生成 | `NOT RUN` | Embedding 模型未配置 |
| FastAPI `/health` | `NOT RUN` | 后端依赖未安装、服务未启动 |
| 测试套件 | `NOT RUN` | 项目依赖未安装 |

## Step 0.1+0.2 验收结论

已完成上游版本固定、许可证保留、贡献边界声明、本机环境盘点、依赖声明与安装差异、配置变量/默认策略、商品与上游评测数据版本、索引完整性检查，以及未运行项记账。

下一阶段开始前仍需用户提供或确认兼容的 Chat 与文本 Embedding 能力。随后创建项目 `.venv`、安装依赖、构建 SQLite，并按方案的 90 分钟检查点依次做商品读取、Router、文本向量和后端健康检查。模型或 embedding 未实际运行前，不生成任何效果结论。

## 阶段 A：可运行性止损检查（已完成）

更新时间：2026-09-11（Asia/Shanghai）。本节是 Step 0 基线之后的增量运行记录；机器可读详情和原始 pip 解析报告分别见 `artifacts/manifests/phase_a_runtime.json` 与 `artifacts/manifests/phase_a_pip_install.json`。`artifacts/manifests/step0_baseline.json` 未覆盖。

| 检查项 | 状态 | 真实结果 |
|---|---|---|
| 项目 `.venv` | `PASS` | Python 3.12.10；创建命令退出码 0 |
| 依赖安装 | `PASS` | `backend/requirements.txt` 安装成功；`pip check` 输出 `No broken requirements found.` |
| 实际依赖版本 | `PASS` | chromadb 1.5.9、openai 3.12.0、python-dotenv 1.2.3、fastapi 0.141.1、uvicorn 0.52.4、httpx 0.28.1、pytest 9.1.1；全部解析版本见 pip report |
| 显式实验开关 | `PASS` | 运行命令使用 `USE_HYBRID=1`、`USE_RERANK=0` |
| SQLite 商品库 | `PASS` | `PRAGMA integrity_check=ok`；100 商品、585 SKU、439 FAQ、453 评价 |
| 商品读取 | `PASS_WITH_ENVIRONMENT_NOTE` | 成功读取 `p_beauty_010`；Windows 默认 GBK 无法打印 `¥`，设置 `PYTHONUTF8=1` 后退出码 0，未修改业务代码 |
| Chat 模型/接口 | `PASS` | 已确认智谱 OpenAI-compatible Chat Completions/Function Calling 接口；`glm-5.3-flash` 返回 HTTP 429 / code 1113 后，用户确认有 `glm-5.2` 资源；`glm-5.2` 强制 Function Calling 与项目 `route()` 入口均成功 |
| 文本 Embedding 模型/接口 | `PASS` | 用户补充账号资源后，智谱标准 `/embeddings` 与 `embedding-3` 直接请求成功；返回模型 `embedding-3`、2048 维、最小探针 9 tokens、请求耗时 2.781 秒 |
| Embedding 接口适配 | `PASS` | 以测试驱动将 Ark 专用 `/embeddings/multimodal` 改为 OpenAI-compatible `embeddings.create` 文本批调用；首轮 RED 为 `4 failed`，审查修复后最终定向测试 `10 passed`，批次硬上限 64 |
| Router 单次调用 | `PASS` | 使用项目 `route()`、`glm-5.2`、实际 tool schema 与强制 function choice；输入“推荐一双500元以内的跑鞋”，输出 `recommend` / `high`，最终复测耗时 8.023 秒 |
| 文本向量生成 | `PASS` | 项目 OpenAI-compatible EmbeddingFunction 真实调用成功，1 个向量、2048 维、耗时 5.170 秒；文档与查询共享同一实现 |
| 独立 Chroma 完整重建 | `PASS` | 在 `backend/storage/chroma_phase_a_embedding3/` 新建 `product_knowledge_embedding3_phase_a`；`embedding-3`、2048 维、1092/1092 chunks；最终端点/模型/维度契约版完整重建耗时 51.498 秒，未复用旧 HNSW 片段 |
| Chroma 契约/运行时 | `PASS` | collection 持久化智谱端点 / `openai_compatible_text` / `embedding-3` / 2048 维；错误模型或错误端点重新打开均在查询前被拒绝；`.env` 默认 `ChromaRetriever` 与 `SearchService` 均读取 1092 条索引 |
| Chroma 查询侧验证 | `PASS` | 默认 `ChromaRetriever()` 用同一模型查询“适合敏感肌的保湿面霜”，返回 5 条结果；查询命令总耗时 9.239 秒 |
| FastAPI `/health` | `PASS` | 实际 Uvicorn `127.0.0.1:18765` 返回 HTTP 200 与 `{"status":"ok"}`，随后进程已终止 |
| 后端单元测试 | `PASS_WITH_SKIPS` | `260 passed, 7 skipped, 3 warnings in 16.95s`；跳过项未计作通过，未运行评测数据集 |

### 90 分钟检查点与 Go / No-Go

检查点已完成并得到明确结论：依赖可安装、商品可读取、SQLite 与 `/health` 可用；智谱凭据可鉴权且模型列表可读取。`glm-5.3-flash` 无资源不能代表全部 Chat 模型不可用，`glm-5.2` 的实际 Router Function Calling 已成功。`embedding-3` 曾因余额/资源包返回 HTTP 429 / code 1113；用户补充资源并明确授权商品文本向该接口发送后，直接向量、项目适配、独立完整建库和同空间查询均成功。

Go / No-Go 最终判定为 `GO_FULL`。按照最终方案“Router、向量均可用”的规则，可以执行完整后续方案。第一阶段所要求的运行检查均已完成，但早期没有为主动文档审阅与用户等待分别启停统一秒表：manifest 的命令/进程时间只是主动环境工时下界，不能独立证明精确总工时。因此 90 分钟检查点和 6 小时止损合规均记为 `PASS_WITH_MEASUREMENT_LIMITATION`，不作无证据的精确时间主张。此处只证明链路可运行，不构成任何检索效果或产品结论。

本机额外检查没有发现可直接复用的文本 embedding 缓存：Chroma MiniLM 与 sentence-transformers 缓存均不存在；Hugging Face 中仅有 faster-whisper 语音识别模型。智谱 `embedding-3` 恢复后无需安装本地替代模型；这避免了引入另一向量空间。

没有开始 Prompt 优化、检索优化、评测运行或效果结论。

阶段 A 的代码变更限于文本 Embedding/Chroma 可运行性：`backend/rag/chroma_store.py`、`backend/rag/build_chroma.py`、`.env.example` 与对应契约测试 `backend/rag/tests/test_chroma_store.py`。结构化结果记录在 `artifacts/manifests/phase_a_runtime.json`，完整最终命令和转录输出记录在 `artifacts/logs/phase_a_runtime_evidence.md`；未在早期完整捕获的探索命令被明确标为部分记录。真实 API Key 没有写入证据或版本库。
