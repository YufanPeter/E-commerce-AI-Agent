# CartPilot 项目交接状态

更新时间：2026-09-11（Asia/Shanghai）
项目根目录：`C:\Users\Admin\Documents\ChatGPT\Cartpilot`
状态口径：以当前 Git 工作树、`audit_and_eval.md`、`artifacts/manifests/step0_baseline.json` 和 `artifacts/manifests/phase_a_runtime.json` 为准；未执行项保持 `NOT RUN`。

> 2026-09-11 阶段 A 结论：第一阶段止损规则已完整执行并判定 `GO_FULL`。项目 `.venv`、依赖、SQLite、商品读取、`glm-5.2` Router、`embedding-3` 文本向量、独立 Chroma 完整重建及 FastAPI `/health` 均有真实成功结果。新索引固定 `embedding-3`、2048 维，共 1092 个 chunk；文档侧与查询侧使用同一接口和模型，未复用仓库内旧 HNSW 片段。后端测试为 `254 passed, 7 skipped, 3 warnings`，跳过项未计作通过。最新机器证据见 `artifacts/manifests/phase_a_runtime.json`，完整 pip 解析见 `artifacts/manifests/phase_a_pip_install.json`；本文件后文 Step 0 的 `NOT RUN` 描述是交接 commit `01e3474` 时的历史状态，不得改写成当时已运行。

## 1. 当前目标、阶段及完成情况

### 总目标

依据 `CartPilot_AI_PM_最终方案.md`，完成“独立评测 + 一个重点优化闭环 + 检索消融 + 产品决策”。Intent 是默认主线；RAG 只做离线实验，不接入生产检索链路。

### 当前阶段

当前已完成**阶段 A 的可运行性与第一阶段止损检查**。依赖、商品读取、Router、文本向量、独立索引、真实查询与 `/health` 均已验证；根据“Router、向量均可用”的规则判定 `GO_FULL`。本轮到此收口，不提前进入 Prompt 优化、检索优化或评测结论。

### 已完成

- 已将项目根目录变为上游 CartPilot 源码的实际工作目录，没有再套一层嵌套仓库。
- 已固定上游 `main` 的 commit `361184104f40ff0add11315b684729ab76c27b95`。
- 已保留上游 MIT `LICENSE` 和作者信息，并写明个人贡献边界。
- 已生成审计文档 `audit_and_eval.md`。
- 已生成机器可读清单 `artifacts/manifests/step0_baseline.json`。
- 已生成不含真实凭据的 `.env.example`，其中实验预设为 `USE_HYBRID=1`、`USE_RERANK=0`。
- 已实数核对 100 个商品、585 个 SKU，以及五份上游评测文件合计 121 条记录；评测文件 SHA-256 已冻结在 manifest。
- 上述成果已提交为 `7e95ac1af53459a7180d5a1b821b62d68887be65`（`docs: establish CartPilot step 0 baseline`）。
- 已创建项目 `.venv`，安装 `backend/requirements.txt` 并保存完整 pip 解析报告；`pip check` 通过。
- 已生成 SQLite 商品库并验证 `PRAGMA integrity_check=ok`；商品读取通过。
- 已以 `USE_HYBRID=1`、`USE_RERANK=0` 完成 FastAPI `/health` 冒烟，返回 HTTP 200。
- 后端单元测试结果为 `254 passed, 7 skipped, 3 warnings`；跳过项没有计作通过。
- 已确认智谱 BigModel OpenAI-compatible Base URL、账号可见 Chat 模型列表、可用 Chat 模型 `glm-5.2` 与文本 Embedding 模型接口 `embedding-3`。
- 已真实完成 `glm-5.2` Router 冒烟：输入“推荐一双500元以内的跑鞋”，输出 `recommend` / `high`。
- 用户补充账号资源后，`embedding-3` 直接请求与项目 EmbeddingFunction 均成功，返回 2048 维文本向量。
- 已用测试驱动把上游 Ark 专用 `/embeddings/multimodal` 调用适配为标准 OpenAI-compatible 文本 `embeddings.create`；契约测试为 `4 passed`。
- 已在被 Git 忽略的 `backend/storage/chroma_phase_a_embedding3/` 完整重建独立 collection `product_knowledge_embedding3_phase_a`，写入 1092 个 chunk；存储向量维度 2048，真实查询成功。
- 已保存独立运行清单 `artifacts/manifests/phase_a_runtime.json`，没有覆盖 Step 0 baseline。

### 尚未完成

- 尚未开始覆盖矩阵、自建 DEV/留出/E2E 数据、Intent 基线、错误分析、优化变体、检索四模式对照或最终材料。
- 尚未产生任何 Prompt 优化、检索优化或效果结论；这些属于后续阶段，不能由阶段 A 冒烟结果推断。

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

阶段 A 工作发生在 `codex/step0`，其开始时 HEAD 为 `01e3474`；中间错误的双侧 No-Go 记录曾提交为 `81753e8`，后续证据已在当前工作树中纠正。准确 HEAD 和未提交状态必须始终由 `git status --short --branch` 与 `git log -1 --oneline` 重新确认，不依赖本文推测。

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
| Python 目标版本 | 项目 `.venv` 使用 `3.12.10`；`pip check` 通过 |
| Chat 模型 | 智谱 `glm-5.2`；强制 Function Calling 与项目 Router 入口均已成功 |
| Embedding 模型 | 智谱 `embedding-3`；标准文本接口可用，文档与查询均为 2048 维 |
| 阶段 A Chroma | `backend/storage/chroma_phase_a_embedding3/` / `product_knowledge_embedding3_phase_a`；1092 chunks；独立、被 Git 忽略 |
| Rerank 模型 | `NOT CONFIGURED`，默认不使用 |
| `USE_HYBRID` | `1` |
| `USE_RERANK` | `0` |
| 超时 / 重试模板 | `ARK_TIMEOUT=30`、`ARK_MAX_RETRIES=1` |
| 真实 Key | 不进入文档、manifest 或 Git |

本地 `.env` 已建立并被 Git 忽略，真实 Key 未进入文档、manifest 或 Git。模型、接口、依赖、命令与失败结果已写入新的运行 manifest，没有覆盖 Step 0 初始快照。

## 6. 未完成事项、阻塞与下一步具体动作

### 当前阻塞/前置条件

阶段 A 没有未解决阻塞。Chat 与文本向量两条路径均可用，环境累计计时低于 6 小时止损上限。后续实验仍须固定当前模型、索引和开关，并在任何数据或模型变化后重新建立可归因基线。

### 下一任务的具体顺序

1. 阶段 A 到此完成；下一阶段应先建立覆盖矩阵并复核数据，不直接修改 Prompt 或检索策略。
2. 后续基线固定 `glm-5.2`、`embedding-3`、独立 1092-chunk Chroma、`USE_HYBRID=1`、`USE_RERANK=0`。
3. 若源码、商品数据树、embedding 模型或维度变化，必须创建新索引并完整重建，不能增量混用当前向量空间。

## 7. 运行中的命令、日志与输出位置

- 当前 Codex 任务没有已知的运行中命令或待轮询句柄。
- 名称级进程检查未发现 `python`、`pythonw`、`uvicorn` 或 `git` 进程；当前 PowerShell 会话也没有后台 Job。
- `.git/index.lock` 不存在。
- 工作树内没有需要轮询的模型、Uvicorn 或索引构建进程。
- 当前持久证据位于：
  - `audit_and_eval.md`
  - `artifacts/manifests/step0_baseline.json`
  - `artifacts/manifests/phase_a_runtime.json`
  - `artifacts/manifests/phase_a_pip_install.json`
  - `.env.example`
  - Git commit `7e95ac1af53459a7180d5a1b821b62d68887be65`
- 尝试通过 `Win32_Process.CommandLine` 枚举所有系统进程时被 Windows 拒绝访问。因此可以确认本任务没有已知活动句柄，并且未发现上述名称的相关进程；不能据此证明系统中绝对不存在由其他会话启动、且名称不同的外部进程。

## 新任务恢复原则

以文件和 Git 输出为准，不把聊天摘要当成实验结果。不要重跑已完成的 Step 0 文件核对，除非当前文件、commit、数据树或 manifest 已变化；不要把 `NOT RUN` 改成完成，直到对应命令真实执行并保存证据。
