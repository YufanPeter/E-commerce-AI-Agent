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

上游已有 Router → Tool → Composer、混合检索、SQLite 商品事实、评测脚本、100 个商品和 585 个 SKU。本项目当前新增的是中文最终方案、版本/环境审计记录与安全配置模板；尚未产生个人评测数据、实验结果或业务优化代码。

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

## 阶段 A：可运行性止损检查（进行中）

更新时间：2026-09-11（Asia/Shanghai）。本节是 Step 0 基线之后的增量运行记录；机器可读详情和原始 pip 解析报告分别见 `artifacts/manifests/phase_a_runtime.json` 与 `artifacts/manifests/phase_a_pip_install.json`。`artifacts/manifests/step0_baseline.json` 未覆盖。

| 检查项 | 状态 | 真实结果 |
|---|---|---|
| 项目 `.venv` | `PASS` | Python 3.12.10；创建命令退出码 0 |
| 依赖安装 | `PASS` | `backend/requirements.txt` 安装成功；`pip check` 输出 `No broken requirements found.` |
| 实际依赖版本 | `PASS` | chromadb 1.5.9、openai 3.12.0、python-dotenv 1.2.3、fastapi 0.141.1、uvicorn 0.52.4、httpx 0.28.1、pytest 9.1.1；全部解析版本见 pip report |
| 显式实验开关 | `PASS` | 运行命令使用 `USE_HYBRID=1`、`USE_RERANK=0` |
| SQLite 商品库 | `PASS` | `PRAGMA integrity_check=ok`；100 商品、585 SKU、439 FAQ、453 评价 |
| 商品读取 | `PASS_WITH_ENVIRONMENT_NOTE` | 成功读取 `p_beauty_010`；Windows 默认 GBK 无法打印 `¥`，设置 `PYTHONUTF8=1` 后退出码 0，未修改业务代码 |
| Chat 模型/接口 | `AUTHENTICATED_BUT_UNAVAILABLE` | 已确认智谱 OpenAI-compatible Chat Completions/Function Calling 接口；账号可见十个 Chat 模型，固定最小验证模型为 `glm-5.3-flash`，实际推理返回 HTTP 429 / provider code 1113（余额不足或无可用资源包） |
| 文本 Embedding 模型/接口 | `AUTHENTICATED_BUT_UNAVAILABLE` | 已确认智谱标准 `/embeddings` 与 `embedding-3`；实际请求通过鉴权后返回同一 HTTP 429 / code 1113，未生成向量。上游当前 `/embeddings/multimodal` 实现与该标准文本接口不同，但因资源不可用没有开始适配 |
| Router 单次调用 | `BLOCKED_RESOURCE_UNAVAILABLE` | 使用 `glm-5.3-flash`、实际 Router tool schema 与强制 function choice 发起请求；服务端在推理前返回 HTTP 429 / code 1113，未产生 Router 决策 |
| 文本向量生成 | `BLOCKED_RESOURCE_UNAVAILABLE` | 使用 `embedding-3` 发起最小文本向量请求；服务端返回 HTTP 429 / code 1113，`vector_generated=false` |
| 独立 Chroma 完整重建 | `NOT RUN` | 没有验证成功的 embedding，按同一向量空间规则禁止建库或复用旧索引 |
| FastAPI `/health` | `PASS` | Uvicorn `127.0.0.1:8765` 返回 HTTP 200 与 `{"status":"ok"}`，随后进程已终止 |
| 后端单元测试 | `PASS_WITH_SKIPS` | `250 passed, 7 skipped, 3 warnings in 20.37s`；跳过项未计作通过，未运行评测数据集 |

### 90 分钟检查点与 Go / No-Go

检查点已完成并得到明确结论：依赖可安装、商品可读取、SQLite 与 `/health` 可用；智谱凭据可鉴权且模型列表可读取，但 Chat 与文本 Embedding 的真实推理均被账号资源错误阻止。失败属于**账号余额/资源包**，不是 Key 格式、网络、依赖、SQLite 或本地代码初始化问题。

Go / No-Go 为 `NO_GO_BOTH_MODEL_INFERENCE_PATHS_UNAVAILABLE`。按照最终方案“二者都不可用”的规则，本轮止于基础设施验证和失败分析，不启动实验。第一阶段的止损流程至此完成，但**完整阶段 A 可运行实验基线没有完成**，Router 决策、文本向量和独立 Chroma 索引均不能宣称成功。累计环境处理未达到 6 小时上限，是因为更早出现了明确 No-Go 条件。

本机额外检查没有发现可直接复用的文本 embedding 缓存：Chroma MiniLM 与 sentence-transformers 缓存均不存在；Hugging Face 中仅有 faster-whisper 语音识别模型。继续下载并适配本地多语言 embedding 会新增下载、内存、依赖和代码成本，同时仍无法恢复 Router，不满足本阶段的受限适配条件，因此未尝试。

没有开始 Prompt 优化、检索优化、评测运行或效果结论。
