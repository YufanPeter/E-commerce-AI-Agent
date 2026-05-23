# RAG 检索流程与构建思路

## 1. 核心目标

先构建一个可靠的 RAG 数据层，让 Agent 的推荐基于商品库事实和可追溯证据，而不是只依赖 Prompt 生成。

第一版重点不是复杂 Agent，而是跑通：

```text
用户需求 -> 需求解析 -> 混合检索 -> 商品证据聚合 -> LLM 生成 -> 商品卡片返回
```

## 2. 数据层设计

RAG 数据层建议分成两部分：

- `SQLite Product Store`：存确定性商品事实。
- `Chroma Vector Store`：存非结构化商品知识的向量。

SQLite 保存：

- `product_id`
- 商品标题、品牌、类目、子类目
- 基础价格、图片路径
- SKU、规格属性、SKU 价格

Chroma 保存：

- `marketing_description`
- `official_faq`
- `user_reviews`

示例 chunk metadata：

```json
{
  "chunk_id": "p_beauty_001:faq:0",
  "product_id": "p_beauty_001",
  "chunk_type": "official_faq",
  "category": "美妆护肤",
  "sub_category": "精华",
  "brand": "雅诗兰黛",
  "base_price": 720.0
}
```

## 3. 需求解析：硬约束与软偏好

用户输入不要全部拿去数据库硬筛，先拆成两类：

硬约束适合 SQL 过滤：

- 预算：`200 元以内`
- 明确类目：`蓝牙耳机`、`跑鞋`
- 品牌包含或排除：`不要耐克`
- SKU 属性：`黑色`、`42 码`、`256GB`
- 明确否定：`不要酒精`、`不含糖`

软偏好适合向量召回和排序：

- `油皮友好`
- `轻量`
- `通勤`
- `熬夜修护`
- `抗初老`
- `性价比高`
- `不闷脚`

核心原则：**硬约束保证不出错，软偏好保证理解能力。**

## 4. 混合检索流程

常规推荐场景：

```text
1. 解析用户需求
   得到 intent、hard_filters、soft_query

2. 用 SQLite 做保守硬过滤
   例如价格、大类、明确排除品牌

3. 候选足够时
   在候选 product_id 范围内做 Chroma 向量召回

4. 候选太少时
   逐级放宽子类目、品牌等非关键条件

5. 聚合向量命中的 chunks
   按 product_id 汇总描述、FAQ、评价证据

6. 规则重排
   综合价格匹配、类目匹配、证据数量、评价风险

7. 返回 top 3 商品
   同时返回 product_cards、evidence、risk_tips
```

模糊场景可以反过来：

```text
用户说“下周去三亚需要买什么”
-> 先全库向量广召回
-> 按类目聚合
-> 再用结构化字段校验价格、品牌、SKU
```

## 5. 如何避免漏召回

- 不把软偏好当硬条件，例如不要用 `WHERE skin_type = '油皮'`。
- 大类可以硬筛，子类目要谨慎；字段不稳定时交给向量检索。
- 如果硬筛后候选少于阈值，例如 5 个，自动放宽条件。
- 向量检索可以先召回较多 chunks，例如 top 20，再按商品聚合。
- 明确告诉用户放宽了什么条件，避免误导。

示例：

```text
用户：500 元以内轻量跑鞋

硬约束：
- category = 服饰运动
- max_price = 500

软查询：
- 轻量 跑鞋 透气 缓震 慢跑
```

## 6. 第一版工具接口

优先实现 `search_products`：

```python
search_products(
    query: str,
    filters: dict,
    top_k: int = 3
) -> {
    "products": [...],
    "evidence": [...],
    "relaxed_filters": [...]
}
```

后续再增加：

- `get_product_detail(product_id)`
- `compare_products(product_ids, focus)`
- `update_preferences(session_id, preferences)`

## 7. 推荐落地顺序

1. 编写数据导入脚本，读取 `data/**/*.json`。
2. 建 SQLite 表：`products`、`skus`、`rag_chunks`。
3. 将描述、FAQ、评价切成 chunks，写入 Chroma。
4. 实现常驻 `ChromaRetriever`，后端启动时加载 collection 和 embedding model。
5. 在 Retriever 之上实现 `search_products`，完成按商品聚合、规则重排和证据返回。
6. 跑通标准问题集：油皮洗面奶、200 元耳机、无酒精防晒、轻量跑鞋。
7. 再接 FastAPI `/chat/stream` 和 iOS 客户端。

## 8. 检索性能原则

- Chroma 建库是离线任务，不在用户请求时执行。
- 在线服务启动时加载 embedding 模型和 Chroma collection，并在进程内复用。
- 首次冷启动可能较慢，本地测试约 60 秒；同进程热查询通常在毫秒到百毫秒级。
- 客户端体验上应先返回 `status` 事件，例如“正在理解需求”“正在检索商品库”。
- 后续可加入热门 query 缓存、候选商品缓存和 metadata 过滤，进一步降低延迟。

实际交付时，`build_chroma` 只作为离线索引构建任务；在线请求只调用常驻 Retriever，不在用户请求中重建 Chroma。
