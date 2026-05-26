# 待办（按优先级）

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
