# CartPilot 项目交接状态

更新时间：2026-09-11T16:55:33+08:00（Asia/Shanghai）
项目根目录：`C:\Users\Admin\Documents\ChatGPT\Cartpilot`
状态口径：以当前 Git 工作树、`audit_and_eval.md`、`artifacts/manifests/step0_baseline.json` 和 `artifacts/manifests/phase_a_runtime.json` 为准；未执行项保持 `NOT RUN`。

> 2026-09-11 阶段 A 结论：第一阶段止损规则已执行并判定 `GO_FULL`。项目 `.venv`、依赖、SQLite、商品读取、`glm-5.2` Router、`embedding-3` 文本向量、独立 Chroma 完整重建及 FastAPI `/health` 均有真实成功结果。新索引固定智谱端点、`embedding-3`、2048 维，共 1092 个 chunk；端点/模型/维度契约已持久化，任一不匹配都会在查询前失败，默认 `ChromaRetriever` 与 `SearchService` 均指向该索引，未复用仓库内旧 HNSW 片段。后端测试为 `260 passed, 7 skipped, 3 warnings`，跳过项未计作通过。计时内容已执行但早期未建立完整主动工时秒表，因此 90 分钟/6 小时合规记为 `PASS_WITH_MEASUREMENT_LIMITATION`，不把命令耗时下界冒充总工时。最新机器证据见 `artifacts/manifests/phase_a_runtime.json`，逐命令记录见 `artifacts/logs/phase_a_runtime_evidence.md`，完整 pip 解析见 `artifacts/manifests/phase_a_pip_install.json`；本文件后文 Step 0 的 `NOT RUN` 描述是交接 commit `01e3474` 时的历史状态，不得改写成当时已运行。

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
- 后端单元测试结果为 `260 passed, 7 skipped, 3 warnings`；跳过项没有计作通过。
- 已确认智谱 BigModel OpenAI-compatible Base URL、账号可见 Chat 模型列表、可用 Chat 模型 `glm-5.2` 与文本 Embedding 模型接口 `embedding-3`。
- 已真实完成 `glm-5.2` Router 冒烟：输入“推荐一双500元以内的跑鞋”，输出 `recommend` / `high`。
- 用户补充账号资源后，`embedding-3` 直接请求与项目 EmbeddingFunction 均成功，返回 2048 维文本向量。
- 已用测试驱动把上游 Ark 专用 `/embeddings/multimodal` 调用适配为标准 OpenAI-compatible 文本 `embeddings.create`；契约测试为 `4 passed`。
- 审查后补充了端点/模型/维度持久化契约、响应索引校验和 Chroma 环境配置，定向测试增至 `10 passed`。
- 已在被 Git 忽略的 `backend/storage/chroma_phase_a_embedding3/` 完整重建独立 collection `product_knowledge_embedding3_phase_a`，写入 1092 个 chunk；最终契约版重建耗时 51.498 秒，存储向量维度 2048。
- 默认 `ChromaRetriever()` 的真实查询成功，错误模型或错误端点重新打开均被拒绝；默认 `SearchService()` 读取到 1092 个 chunk。
- 已保存独立运行清单 `artifacts/manifests/phase_a_runtime.json`，没有覆盖 Step 0 baseline。
- 阶段 A 最终代码与证据提交为 `3a3a456ac0bd5e5ae550a01ea599faadb927c0c4`；独立只读复核结论为 Merge-ready，没有 Critical 或 Important。
- 已创建公开 fork `https://github.com/zixin0927/E-commerce-AI-Agent`，分支 `codex/step0` 已推送，并向上游创建 PR [#2](https://github.com/YufanPeter/E-commerce-AI-Agent/pull/2)。交接核验时 PR 为 open、非 draft、`mergeable_state=clean`，head 与本地 `3a3a456ac0bd5e5ae550a01ea599faadb927c0c4` 一致；外部 review 与 check run 均为 0。

### 尚未完成

- 尚未开始覆盖矩阵、自建 DEV/留出/E2E 数据、Intent 基线、错误分析、优化变体、检索四模式对照或最终材料。
- 尚未产生任何 Prompt 优化、检索优化或效果结论；这些属于后续阶段，不能由阶段 A 冒烟结果推断。

## 2. 代码目录、分支、commit 与未提交改动

| 项目 | 当前值 |
|---|---|
| 实际代码目录 | `C:\Users\Admin\Documents\ChatGPT\Cartpilot` |
| Git 目录 | `C:\Users\Admin\Documents\ChatGPT\Cartpilot\.git`，非 linked worktree |
| 当前分支 | `codex/step0` |
| 当前阶段 A 实现/证据 tip | `3a3a456ac0bd5e5ae550a01ea599faadb927c0c4`（`fix: enforce embedding index contract`） |
| 阶段 A 基线提交 | `4e068ee`（`fix: complete phase A runtime baseline`） |
| Step 0.1+0.2 提交 | `7e95ac1af53459a7180d5a1b821b62d68887be65` |
| 固定的上游基线 | `361184104f40ff0add11315b684729ab76c27b95` |
| 上游 remote | `upstream = https://github.com/YufanPeter/E-commerce-AI-Agent.git` |
| Fork remote / 跟踪分支 | `fork = https://github.com/zixin0927/E-commerce-AI-Agent.git`；`codex/step0` 跟踪 `fork/codex/step0` |
| 上游 PR | `https://github.com/YufanPeter/E-commerce-AI-Agent/pull/2`；head `zixin0927:codex/step0` → base `YufanPeter:main` |
| Git 工作区形式 | 普通 checkout；`.git` 与 common dir 均为项目根目录 `.git`，不是 linked worktree |
| 交接整理前未提交改动 | 无；`git status --short --branch` 仅显示 `## codex/step0...fork/codex/step0` |
| 主要后端代码 | `backend/agent`、`backend/search`、`backend/rag`、`backend/store`、`backend/eval`、`backend/api` |
| 商品源数据 | `data/` |

阶段 A 工作发生在 `codex/step0`，其开始时 HEAD 为 `01e3474`；中间错误的双侧 No-Go 记录曾提交为 `81753e8`，随后由 `4e068ee` 和 `3a3a456` 依据真实恢复后的模型结果纠正。`PROJECT_STATE.md` 无法自记录包含自身修改的提交哈希；本次交接文档若另行提交，其 commit 应由新任务首先用 `git log -1 --oneline` 获取。准确 HEAD 和未提交状态始终以 Git 输出为准。

## 3. 已运行检查、真实结果与证据位置

本次交接没有重新执行任何已有实验。以下第一张表是 Step 0 当时的历史检查，第二张表是阶段 A 已保存的真实运行结果；两者不得混写。

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

上一句只描述 Step 0 交接 commit `01e3474` 时的历史状态。阶段 A 后续真实运行结果如下：

| 阶段 A 检查 | 真实结果 | 权威证据 |
|---|---|---|
| 项目依赖 | `.venv` 使用 Python 3.12.10；`backend/requirements.txt` 安装成功；`pip check` 无冲突 | `artifacts/manifests/phase_a_pip_install.json`；`artifacts/manifests/phase_a_runtime.json`；运行证据第 1 节 |
| SQLite / 商品读取 | `integrity_check=ok`；100 商品、585 SKU、439 FAQ、453 评价；`p_beauty_010` 读取成功 | 运行 manifest；运行证据第 1 节 |
| Chat Router | 智谱 `glm-5.2` 项目 `route()` 成功，结果 `recommend/high`，请求耗时 8.023389 秒 | 运行 manifest；运行证据第 2 节 |
| 文本 Embedding | 智谱 `embedding-3` 标准接口与项目 EmbeddingFunction 均成功，2048 维 | 运行 manifest；运行证据第 3 节 |
| 独立 Chroma | 授权发送的 1092 个文本块全部写入独立 collection；最终完整重建耗时 51.498336 秒 | 运行 manifest；运行证据第 4 节 |
| Chroma 契约 / 查询 | 落盘端点、provider、模型、维度；错误端点或模型查询前拒绝；默认查询返回 5 条结果 | 运行 manifest；运行证据第 5 节 |
| SearchService 开关 | 默认索引计数 1092；实际构造状态 `use_hybrid=True`、`use_rerank=False` | 运行证据第 5 节 |
| FastAPI `/health` | 实际 Uvicorn 返回 HTTP 200、`{"status":"ok"}`，随后进程终止 | 运行 manifest；运行证据第 6 节 |
| 后端测试 | `260 passed, 7 skipped, 3 warnings in 16.95s`；未运行评测数据集 | 运行 manifest；运行证据第 7 节 |
| 安全 / 冻结保护 | 两个本地真实 Key 在 `.env` 外精确匹配数 0；`.env` 未跟踪；Step 0 baseline 与旧 Chroma 无差异 | 运行 manifest；运行证据第 9 节；提交前检查输出 |
| 独立代码复核 | 上轮 3 个 Important 均解决；无 Critical/Important；Merge-ready | 当前任务的只读复核结果；对应实现为 `3a3a456` |
| GitHub 交付 | fork 分支、本地 HEAD 和 PR head 均为 `3a3a456ac0bd5e5ae550a01ea599faadb927c0c4`；PR #2 open/clean | GitHub PR #2；交接时只读 API 核验 |

## 4. 已确定决策、否定方案及原因

### 已确定

- 保留 CartPilot 作为载体，保留上游署名和许可证。
- Intent 是主研究线；检索仅做离线消融，R3 不接生产链路。
- Windows 环境只处理 Python/FastAPI 文本链路，不要求运行 macOS/Xcode/iOS 客户端。
- 初始实验配置显式使用 `USE_HYBRID=1`、`USE_RERANK=0`；后续运行必须从实际构造参数和日志验证，不能只相信环境变量。
- embedding 一旦更换，文档索引和查询侧必须使用同一模型并完整重建独立索引。
- collection 必须持久化并校验 embedding 端点、provider、模型和维度；不匹配时在查询前失败。
- 任何未实际运行的检查或效果均写 `NOT RUN`，不从源码存在推断效果。
- 真实 Key 只保留在被 Git 忽略的根目录 `.env` 中，可在后续同一项目任务复用；不得打印、写入证据或提交。
- 阶段 A 的计时口径固定为 `PASS_WITH_MEASUREMENT_LIMITATION`：已捕获命令/进程时间只是主动工时下界，没有足够证据还原严格的 90 分钟检查点时间戳或精确累计主动工时。
- Git 未配置用户身份，因此 Step 0 提交使用仅该次命令生效的 `Codex <codex@localhost>`；没有修改用户全局 Git 配置。

### 已否定或暂不采用

- **不使用浮动 `main` 做实验。** 原因：结果必须可复现，已固定具体 commit。
- **不把仓库内 HNSW 二进制片段当成可用 Chroma 基线。** 原因：缺少 `chroma.sqlite3` 元数据数据库，完整性不足且 embedding 来源未确认。
- **不混用旧索引和新 embedding。** 原因：向量空间不同，结果无效。
- **不继续使用 `glm-5.3-flash` 作为阶段 A Router。** 原因：实际返回 HTTP 429 / provider code 1113；账号可用的 `glm-5.2` 已由项目入口验证成功。
- **不安装本地 embedding 替代模型。** 原因：智谱 `embedding-3` 恢复后已完成同空间建库和查询，引入本地模型会制造另一向量空间。
- **不直接推送上游仓库。** 原因：GitHub 身份 `zixin0927` 对 `YufanPeter/E-commerce-AI-Agent` 返回 403；已按标准 fork 流程推送并创建 PR #2，没有尝试绕过权限。
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
| Embedding Base URL | `https://open.bigmodel.cn/api/paas/v4/`；索引 metadata/config 中已持久化，Key 不持久化 |
| 阶段 A Chroma | `backend/storage/chroma_phase_a_embedding3/` / `product_knowledge_embedding3_phase_a`；1092 chunks；智谱端点 / `openai_compatible_text` / `embedding-3` / 2048 维契约；独立、被 Git 忽略 |
| Rerank 模型 | `NOT CONFIGURED`，默认不使用 |
| `USE_HYBRID` | `1` |
| `USE_RERANK` | `0` |
| 超时 / 重试模板 | `ARK_TIMEOUT=30`、`ARK_MAX_RETRIES=1` |
| 真实 Key | 不进入文档、manifest 或 Git |

实际依赖解析完整报告为 `artifacts/manifests/phase_a_pip_install.json`，SHA-256 为 `8e542b16e7e883f15f5ba0884d6f3229372a78e31458045522be6c90d5714784`。阶段 A 命令证据文件 SHA-256 为 `139572cb15dcc67b573f42918d86c1f50585c80841fc9a7fec23aa0996d776f8`；对应字节数和哈希也记录在运行 manifest 中。

本地 `.env` 已建立并被 Git 忽略，真实 Key 未进入文档、manifest 或 Git。模型、接口、依赖、命令与失败结果已写入新的运行 manifest，没有覆盖 Step 0 初始快照。

## 6. 未完成事项、阻塞与下一步具体动作

### 当前阻塞/前置条件

阶段 A 没有运行链路阻塞。Chat 与文本向量两条路径均可用。PR #2 尚未合并，交接核验时没有外部 review，也没有 GitHub check run；这不改变本地阶段 A 结论，但属于后续集成状态。已捕获的命令/进程时间远低于 6 小时，但它只是主动工时下界；早期没有完整区分文档审阅与用户等待的统一秒表，因此计时合规只能记为 `PASS_WITH_MEASUREMENT_LIMITATION`。

独立复核还留下 3 个不阻塞的 Minor，均未在本轮继续修改：非空 collection 缺失 `embedding_dimensions` 时可进一步 fail-closed；以后新增的直接 embedding 探针应与生产客户端保持 `ARK_EMBEDDING_API_KEY` 优先级；若追求最严格审计语义，可评估把计时状态改为 `NOT_INDEPENDENTLY_VERIFIABLE`。这些不是阶段 A 完成条件，是否处理应由新任务结合范围决定。

### 下一任务的具体顺序

1. 新任务首先只运行 `git status --short --branch` 和 `git log -1 --oneline`，再完整读取本文件；同时确认 PR #2 是否出现 review、check 或合并状态变化。
2. 除非源码、`data/` tree、评测数据 tree、manifest、embedding 端点/模型/维度发生变化，不重跑已完成的 Step 0 或阶段 A 实验。
3. 阶段 A 到此完成；下一阶段先按 `CartPilot_AI_PM_最终方案.md` 建立覆盖矩阵并复核/规划自建 DEV、最终留出和 E2E 数据，不直接修改 Prompt 或检索策略。
4. 后续基线固定 `glm-5.2`、智谱 `embedding-3`、独立 1092-chunk Chroma、`USE_HYBRID=1`、`USE_RERANK=0`，并继续从实际构造状态记录开关。
5. 若源码、商品数据树、embedding 端点、模型或维度变化，创建新索引并完整重建，不能增量混用当前向量空间。
6. 若上游 PR review 要求修改，先核验证据和影响范围，再在 `codex/step0` 上修复、复测相关范围并推送 fork；不要直接向上游 `main` 写入。

## 7. 运行中的命令、日志与输出位置

- 2026-09-11T16:55+08:00 的只读检查未发现 `python`、`pythonw`、`uvicorn` 或 `git` 进程；当前 PowerShell 会话没有后台 Job。
- `.git/index.lock` 不存在；没有待轮询的模型、Uvicorn、索引构建、测试或 Git 句柄。
- 工作树内没有需要轮询的模型、Uvicorn 或索引构建进程。
- 当前持久证据位于：
  - `audit_and_eval.md`
  - `artifacts/manifests/step0_baseline.json`
  - `artifacts/manifests/phase_a_runtime.json`
  - `artifacts/logs/phase_a_runtime_evidence.md`
  - `artifacts/manifests/phase_a_pip_install.json`
  - `.env.example`
  - 阶段 A 最终实现/证据 commit `3a3a456ac0bd5e5ae550a01ea599faadb927c0c4`
  - 上游 PR `https://github.com/YufanPeter/E-commerce-AI-Agent/pull/2`
- 尝试通过 `Win32_Process.CommandLine` 枚举所有系统进程时被 Windows 拒绝访问。因此可以确认本任务没有已知活动句柄，并且未发现上述名称的相关进程；不能据此证明系统中绝对不存在由其他会话启动、且名称不同的外部进程。

## 新任务恢复原则

以文件和 Git 输出为准，不把聊天摘要当成实验结果。不要重跑已完成的 Step 0 文件核对，除非当前文件、commit、数据树或 manifest 已变化；不要把 `NOT RUN` 改成完成，直到对应命令真实执行并保存证据。

## 可粘贴到新任务的启动提示

继续 `C:\Users\Admin\Documents\ChatGPT\Cartpilot` 中的 CartPilot 项目。先只运行 `git status --short --branch` 和 `git log -1 --oneline`，然后完整读取项目根目录 `PROJECT_STATE.md`，并结合 `CartPilot_AI_PM_最终方案.md`、`audit_and_eval.md`、`artifacts/manifests/step0_baseline.json`、`artifacts/manifests/phase_a_runtime.json`、`artifacts/logs/phase_a_runtime_evidence.md` 与 `.env.example`；以当前工作树和 GitHub PR #2 的实时状态为权威。阶段 A 与第一阶段止损检查已经完成并判定 `GO_FULL`，阶段 A 实现/证据 tip 为 `3a3a456ac0bd5e5ae550a01ea599faadb927c0c4`，分支 `codex/step0` 跟踪 `fork/codex/step0`，上游基线仍冻结为 `361184104f40ff0add11315b684729ab76c27b95`。不要重新执行已完成的 Step 0 或阶段 A 实验，除非源码、数据树、manifest、embedding 端点/模型/维度已经变化；不要泄露或提交根目录 `.env` 中的 Key。下一阶段先建立覆盖矩阵并复核/规划 DEV、最终留出与 E2E 数据，暂不开始 Prompt 优化、检索优化或效果结论。固定 `glm-5.2`、智谱 `embedding-3`、独立 1092-chunk Chroma、`USE_HYBRID=1`、`USE_RERANK=0`，并保存所有新运行的真实命令、原始结果、耗时和失败原因。PR #2 当前为 open/clean、无 review、无 check run；先确认其是否变化，并对任何 review 逐条核验后再处理。
