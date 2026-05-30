# 待办（按优先级）

> 全局备忘录。新增条目按优先级插入；完成后划掉但保留一段时间留痕。

## P0 — 用户偏好（长期记忆）✨ 进行中

> 决策：分两层。V1 只做**通用偏好**（跨品类），V2 做**品类专属**（如美妆肤质）。
> 现 `PreferenceView` 的"油皮 / 避开酒精"只对美妆有效，先撤掉，等 V2 品类抽屉再加。

### V1 · 通用偏好（5 维度）
| 维度 | UI | 后端字段 | 用途 |
|---|---|---|---|
| 常用预算区间 | Range Slider | `budget_min` / `budget_max` | 用户未说价格时默认套用 |
| 价格倾向 | 单选 chips | `price_tier`: `value`/`balanced`/`premium` | composer 话术 + soft_terms 加分 |
| 关注品类 | 多选 chips（4 大类） | `favorite_categories: list[str]` | 模糊 query 时优先这些类 |
| 品牌偏好 | TagField 输入 | `brand_include` / `brand_exclude` | 直接合并到 `ParsedQuery.hard_filters` |
| 购物风格 | 多选 chips | `style_tags: list[str]` | 进 `soft_terms` 加分 |

### V1 执行步骤
1. **后端**：新增 `backend/agent/user_profile.py`（dataclass + JSON 文件持久化，每个 user_id 一个文件）
2. **API**：`GET /preferences/{user_id}` / `PUT /preferences/{user_id}`
3. **链路**：`ChatRequest` 加 `user_id`；`/chat` `/chat/stream` 时加载 profile 注入 `AgentSession.user_profile`
4. **iOS**：`PreferenceStore: ObservableObject`（`@AppStorage` 本地立即生效 + debounce 1s 同步后端）
5. **消费**：`llm_parser` 把 profile 当 system context；query 没指定价格/品牌时回退到 profile

### V2 · 品类专属（留扩展位，先不做）
- 数据结构预留：`category_specific: dict[str, dict]`
- 美妆护肤：肤质、避开成分、关注功效
- 数码电子：OS 偏好、接口偏好、续航优先级
- 服饰运动：尺码、运动场景
- 食品生活：忌口、口味偏好
- UI 形式：勾选某品类后**展开抽屉**填写专属字段

---

## P0 — 零命中降级（最影响 demo 效果）

### 零结果分层 fallback
- 涉及文件：`backend/search/search_service.py`
- 当 `hits == []` 时按顺序放宽：
  1. 去 `price_min/max`
  2. 去 `brand_exclude`
  3. 去 `brand_include`
  4. `sub_category` 上推到 `category`
  5. 全库纯向量召回
- `SearchResult` 加 `fallback_reason: str | None`，让前端/下游 LLM 能解释"放宽了什么"
- **严格匹配 vs fallback 必须前端可区分**（不能合并到同一数组）
- 进阶：让 LLM 输出 `relaxation_priority`，按品类决定放宽顺序
  - 手机 / 笔记本：先放价格
  - 奢侈品 / 护肤：**绝不**放品牌
  - 食品：可直接换 sub_category
- 已知触发例：「5000 以内非苹果笔记本」（最便宜非苹果 6299）、「始祖鸟 800-1500 徒步鞋」（没货）

## P1 — 其他 fallback 与质量

### LLM 解析失败兜底
- 涉及文件：`backend/search/llm_parser.py`
- Ark 超时 / 报错时不抛异常，退化为纯向量召回（跳过 hard_filter，原始 query 直接喂 retriever）
- `ParsedQuery` 加 `source: "llm" | "fallback_raw"` 字段，方便排查

### needs_clarification 兜底
- 涉及文件：`backend/search/search_service.py`
- 模糊 query 不只返 `clarification_question`，同时给一组"热销/高评分"商品兜底
- 依赖：chunk metadata 或 SQLite 增加 `sales` / `rating` 字段

### 结构化成分字段
- 涉及文件：`backend/rag/build_chroma.py`
- 食品 / 美妆类 chunk metadata 加 `ingredients` 字段
- 后置过滤改用 `ingredients` 精确匹配而非全文 substring
- 解决"无糖饮料"过度过滤问题（"糖"字在卖点描述里到处都是）

## P2 — 数据治理

### SQLite brand 字段最终治理
- 当前用 `BRAND_ALIASES`（`search/query_understanding.py`）治标，长期建议清洗
- 加 `brand_origin` 列后可支持"欧美品牌"/"日系"/"国货"等筛选
- 现状重复对：
  - `Apple 苹果` (8) + `苹果` (1)
  - `Nike` (2) + `耐克` (4)
  - `The North Face` (1) + `北面` (1)

## P3 — 工程化

- FastAPI handler 把 `SearchService.search()` 暴露成 HTTP 接口
- 业务重排（销量 / 评分 / 个性化）单独抽 `Recommender` 模块
- 语义缓存：把 LRU 缓存升级为向量相似度匹配，"蓝牙耳机 300 以内" 和 "300 以内的蓝牙耳机" 应共享缓存

## P3 — Agent 层性能 & 体验完善

> 来源：Phase 2 SSE 落地后实测发现

- **TTFB 优化（当前 router 3.6s + tool 20.7s + composer 7s ≈ 31s）**
  - `intent_router` 切到 `doubao-lite`（路由意图不需要旗舰模型）
  - `llm_parser` 同上；或并行 query 解析与 Chroma 召回
  - 评估对准确率的影响（用 `test_e2e.py` 现有用例对照）
- **`tool_result.payload` schema 收敛**
  - 当前 `payload.products` 给前端，`payload.debug` 给开发者；继续观察 iOS 端用到哪些字段，删冗余
  - 缺 `image_url`：需要先建商品图资产管线（`data/*/images/` 暂时没接进来）
- **多轮 refine 兜底**
  - "换便宜点的"/"再来 5 款"：从 `session.get("last_hits")` 复用，避免重检索
  - 涉及 `RecommendTool.run()` 增加 refine 分支判断
- **session 持久化**
  - 当前 `_SessionStore` 是进程内 dict，重启即丢；先加 SQLite 或 JSON 落盘
- **流式事件契约文档化**
  - 在 `docs/` 补一份 `SSE_PROTOCOL.md`，列出 `session/status/meta/tool_result/token/done/error` 七种事件的 schema
  - 给 iOS 端接入方便

## P3 — 数据资产

- **商品图片**：`data/*/images/` 当前是空目录，需要补图 + 在 `payload.products[]` 输出 `image_url`
- **chunk metadata 加 sales/rating**：为 needs_clarification 兜底"热销" 做准备
