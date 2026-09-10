# CartPilot 项目交接状态

更新时间：2026-09-11（Asia/Shanghai）
项目根目录：`C:\Users\Admin\Documents\ChatGPT\Cartpilot`
状态口径：以当前 Git 工作树、`audit_and_eval.md` 和 `artifacts/manifests/step0_baseline.json` 为准；未执行项保持 `NOT RUN`。

## 1. 当前目标、阶段及完成情况

### 总目标

依据 `CartPilot_AI_PM_最终方案.md`，完成“独立评测 + 一个重点优化闭环 + 检索消融 + 产品决策”。Intent 是默认主线；RAG 只做离线实验，不接入生产检索链路。

### 当前阶段

用户本轮把范围限定为 **Step 0.1 + Step 0.2**：

1. Step 0.1：获取并固定上游源码版本。
2. Step 0.2：记录本机环境、依赖、配置、数据和索引的初始基线。

### 已完成

- 已将项目根目录变为上游 CartPilot 源码的实际工作目录，没有再套一层嵌套仓库。
- 已固定上游 `main` 的 commit `361184104f40ff0add11315b684729ab76c27b95`。
- 已保留上游 MIT `LICENSE` 和作者信息，并写明个人贡献边界。
- 已生成审计文档 `audit_and_eval.md`。
- 已生成机器可读清单 `artifacts/manifests/step0_baseline.json`。
- 已生成不含真实凭据的 `.env.example`，其中实验预设为 `USE_HYBRID=1`、`USE_RERANK=0`。
- 已实数核对 100 个商品、585 个 SKU，以及五份上游评测文件合计 121 条记录；评测文件 SHA-256 已冻结在 manifest。
- 上述成果已提交为 `7e95ac1af53459a7180d5a1b821b62d68887be65`（`docs: establish CartPilot step 0 baseline`）。

### 尚未完成

- 尚未完成方案阶段 A 的可运行性与文本链路冒烟。
- 尚未创建 `.venv`，尚未安装项目依赖。
- 尚未配置 Chat、Embedding 或 Rerank 模型；没有 `.env`。
- 尚未生成 SQLite 商品库或完整 Chroma 元数据数据库。
- Router、文本向量、FastAPI 健康检查、测试套件和任何评测均未运行。
- 尚未开始覆盖矩阵、自建 DEV/留出/E2E 数据、Intent 基线、错误分析、优化变体、检索四模式对照或最终材料。

## 2. 代码目录、分支、commit 与未提交改动

| 项目 | 当前值 |
|---|---|
| 实际代码目录 | `C:\Users\Admin\Documents\ChatGPT\Cartpilot` |
| Git 目录 | `C:\Users\Admin\Documents\ChatGPT\Cartpilot\.git`，非 linked worktree |
| 当前分支 | `codex/step0` |
| Step 0.1+0.2 提交 | `7e95ac1af53459a7180d5a1b821b62d68887be65` |
| 固定的上游基线 | `361184104f40ff0add11315b684729ab76c27b95` |
| 上游 remote | `upstream = https://github.com/YufanPeter/E-commerce-AI-Agent.git` |
| 主要后端代码 | `backend/agent`、`backend/search`、`backend/rag`、`backend/store`、`backend/eval`、`backend/api` |
| 商品源数据 | `data/` |

写入本文件前，`git status --short --branch` 的结果只有 `## codex/step0`，即工作树干净。本文件应作为交接检查点提交；交接后的准确 HEAD 和未提交状态必须由新任务首先运行 `git status --short --branch` 与 `git log -1 --oneline` 重新确认，不依赖这段文字推测。

## 3. 已运行检查、真实结果与证据位置

没有重新执行任何已有实验；以下均为 Step 0 的源码、环境或文件一致性检查。

| 检查 | 真实结果 | 证据 |
|---|---|---|
| 上游 HEAD 查询 | `main`/`HEAD` 为 `361184104f40ff0add11315b684729ab76c27b95` | Git 历史；manifest 的 `source` |
| 固定版本检出 | 当前分支从该上游 commit 派生 | `git log`；manifest 的 `source` |
| Python 环境盘点 | Python `3.12.10`，pip `26.2.1`，Windows 11 build `26200`，AMD64 | `audit_and_eval.md`；manifest 的 `runtime` |
| 项目依赖盘点 | 全局仅发现 `httpx==0.28.1`、`pytest==9.1.1`；核心包未安装；项目 `.venv` 不存在 | manifest 的 `python_dependencies` |
| 配置存在性检查 | `.env` 不存在；相关模型/凭据环境变量未发现；未读取凭据值 | manifest 的 `configuration` |
| 商品数据核对 | 100 个商品 JSON、585 个 SKU | manifest 的 `catalog` |
| 上游评测核对 | Routing 50、Retrieval 40、Semantic Intent 24、Semantic Rerank 5、Negative 2，合计 121 条 | manifest 的 `upstream_eval` |
| 评测哈希复核 | 五个文件的实时 SHA-256 与 manifest 全部匹配 | `artifacts/manifests/step0_baseline.json` |
| 索引完整性检查 | SQLite 商品库缺失；`chroma.sqlite3` 缺失；仅有部分已跟踪 HNSW 片段及 image/compare JSON | `audit_and_eval.md`；manifest 的 `local_generated_state` |
| 安全配置检查 | `.env` 被 Git 忽略；`.env.example` 可跟踪；模板不含真实 Key | `.gitignore`、`.env.example` |
| 提交前格式检查 | `git diff --cached --check` 通过 | Git 提交 `7e95ac1` 的父级差异 |
| Step 0 最终一致性检查 | 上游基线、商品/SKU、121 条记录及全部 SHA-256 匹配；当时工作树干净 | `audit_and_eval.md` 和 manifest；上一任务命令输出未另存日志 |

没有测试通过的结论。`dependency_install`、`product_store_import`、`router_smoke`、`text_embedding_smoke`、`backend_health`、`test_suite` 均明确为 `NOT RUN`。

## 4. 已确定决策、否定方案及原因

### 已确定

- 保留 CartPilot 作为载体，保留上游署名和许可证。
- Intent 是主研究线；检索仅做离线消融，R3 不接生产链路。
- Windows 环境只处理 Python/FastAPI 文本链路，不要求运行 macOS/Xcode/iOS 客户端。
- 初始实验配置显式使用 `USE_HYBRID=1`、`USE_RERANK=0`；后续运行必须从实际构造参数和日志验证，不能只相信环境变量。
- embedding 一旦更换，文档索引和查询侧必须使用同一模型并完整重建独立索引。
- 任何未实际运行的检查或效果均写 `NOT RUN`，不从源码存在推断效果。
- Git 未配置用户身份，因此 Step 0 提交使用仅该次命令生效的 `Codex <codex@localhost>`；没有修改用户全局 Git 配置。

### 已否定或暂不采用

- **不使用浮动 `main` 做实验。** 原因：结果必须可复现，已固定具体 commit。
- **不把仓库内 HNSW 二进制片段当成可用 Chroma 基线。** 原因：缺少 `chroma.sqlite3` 元数据数据库，完整性不足且 embedding 来源未确认。
- **不混用旧索引和新 embedding。** 原因：向量空间不同，结果无效。
- **不在 Step 0.1+0.2 安装依赖或调用模型。** 原因：用户明确只要求这两个子步骤，且模型资源尚未确认。
- **不把五份文件的 121 条记录说成 121 个独立场景。** 原因：尚未跨文件去重。
- **不默认启用 rerank。** 原因：最终方案要求关闭 rerank 以控制变量；上游 Docker/Compose 的 `USE_RERANK=1` 默认值必须在实际运行时覆盖。
- **不预设 H1/H2 成立或官方评测存在泄漏。** 原因：这些都必须由后续真实审计和实验验证。
- **不做前端、图像/语音、爬虫、训练/微调、长期记忆重构、多意图编排、支付订单、线上部署、生产双路召回、reranker 优化或大范围 Top-K 扫描。** 原因：超出最终方案范围。

## 5. 冻结数据版本与实验配置

### 源码和数据

| 对象 | 冻结值 |
|---|---|
| 上游源码 commit | `361184104f40ff0add11315b684729ab76c27b95` |
| `data/` Git tree | `31a9aa580d9cdceaef84445033892f209e2bdc35` |
| `backend/eval/datasets/` Git tree | `83a0a699a0314055dfe3f59048e64ca4641622b0` |
| 商品 / SKU | 100 / 585 |
| 上游评测记录数之和 | 121；未验证跨文件唯一性 |
| 单文件 SHA-256 | 见 `artifacts/manifests/step0_baseline.json` 的 `upstream_eval.files` |

尚无自建 DEV、最终留出或 E2E 数据，因此没有可声明冻结的个人评测集，也没有任何数据 hash 可以补写。

### 配置

| 配置项 | 当前冻结口径 |
|---|---|
| Python 目标版本 | 本机 `3.12.10`；后续在项目 `.venv` 中复核实际依赖兼容性 |
| Chat 模型 | `NOT CONFIGURED` |
| Embedding 模型 | `NOT CONFIGURED` |
| Rerank 模型 | `NOT CONFIGURED`，默认不使用 |
| `USE_HYBRID` | `1` |
| `USE_RERANK` | `0` |
| 超时 / 重试模板 | `ARK_TIMEOUT=30`、`ARK_MAX_RETRIES=1` |
| 真实 Key | 不进入文档、manifest 或 Git |

`.env.example` 是模板，不代表 `.env` 已建立或变量已生效。模型、版本、接口与实际依赖解析结果应在首次成功运行时写入新的运行 manifest，不能覆盖 Step 0 初始快照。

## 6. 未完成事项、阻塞与下一步具体动作

### 当前阻塞/前置条件

1. 用户需要确认并在本地 `.env` 中配置兼容的 Chat 模型和文本 Embedding 模型/端点/凭据。
2. 项目核心依赖尚未安装，`.venv` 不存在。
3. SQLite 商品库与完整 Chroma 索引尚未生成。
4. Embedding 模型未固定前不能重建索引，也不能开展可归因的 Retrieval 实验。

这些是下一阶段的前置条件，不影响 Step 0.1+0.2 已有证据；但它们阻止“可运行实验基线”完成。

### 下一任务的具体顺序

1. 先读本文件、`CartPilot_AI_PM_最终方案.md`、`audit_and_eval.md` 和 `artifacts/manifests/step0_baseline.json`。
2. 运行只读状态确认：`git status --short --branch`、`git log -1 --oneline`、`git rev-parse HEAD^`；不要先拉取浮动上游。
3. 与用户确认 Chat/Embedding 的兼容接口和资源预算；真实 Key 只写入被忽略的 `.env`。
4. 在根目录创建 `.venv`，用 `.venv\Scripts\python.exe -m pip install -r backend\requirements.txt` 安装依赖，并保存实际解析版本；不要污染或以全局 Python 包作为项目基线。
5. 显式设置 `USE_RERANK=0`，核对运行时实际构造参数；不要依赖 Docker/Compose 的上游默认值。
6. 从 `backend` 运行商品数据导入，生成 `backend/storage/ecommerce_agent.sqlite3`，先验证可以读取一个商品。
7. 先做一次限时文本 embedding 诊断。只有模型与接口确认可用后，使用该 embedding 完整重建独立 Chroma 索引；不要复用缺失元数据的旧片段。
8. 分别执行一次 Router 调用、一次文本向量生成和 FastAPI `/health` 检查，记录输入、配置、耗时、失败类型和原始输出位置。
9. 更新审计文档并新建运行 manifest；不要改写 `step0_baseline.json`。达到方案的 90 分钟检查点时做 Go/No-Go 判断，环境处理累计最多 6 小时。
10. 可运行性确认后再进入覆盖矩阵和评测数据设计，不提前开始 Prompt 或检索优化。

## 7. 运行中的命令、日志与输出位置

- 当前 Codex 任务没有已知的运行中命令或待轮询句柄。
- 名称级进程检查未发现 `python`、`pythonw`、`uvicorn` 或 `git` 进程；当前 PowerShell 会话也没有后台 Job。
- `.git/index.lock` 不存在。
- 工作树内没有 `*.log`、`*.out` 或 `*.err` 文件。
- 本轮 Step 0 检查输出没有单独写日志；持久证据位于：
  - `audit_and_eval.md`
  - `artifacts/manifests/step0_baseline.json`
  - `.env.example`
  - Git commit `7e95ac1af53459a7180d5a1b821b62d68887be65`
- 尝试通过 `Win32_Process.CommandLine` 枚举所有系统进程时被 Windows 拒绝访问。因此可以确认本任务没有已知活动句柄，并且未发现上述名称的相关进程；不能据此证明系统中绝对不存在由其他会话启动、且名称不同的外部进程。

## 新任务恢复原则

以文件和 Git 输出为准，不把聊天摘要当成实验结果。不要重跑已完成的 Step 0 文件核对，除非当前文件、commit、数据树或 manifest 已变化；不要把 `NOT RUN` 改成完成，直到对应命令真实执行并保存证据。
