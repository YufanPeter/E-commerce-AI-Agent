# Agent 工作流与记忆问题清单

记录日期：2026-11-09  
代码基线：分支 `feat/amazon-migration`，提交 `4247cb9` 加上阶段 6 尚未提交的改动  
处理时机：Amazon 迁移结束后统一解决

## 使用说明

**证据等级**

- **已复现：** 离线调用真实代码，跑出了错误结果。
- **读代码：** 从代码路径得出，没有实际触发。

本次 review 没有用真实模型评估路由准确率，也没有运行 `evals/` 下的记忆评测。

**开始修复前先复测。** 迁移的阶段 7 和阶段 8 会改动同一批代码，部分问题可能已被解决。下表的“迁移计划覆盖”一列标出了重叠的部分。行号以基线代码为准，之后可能变化，请优先按函数名查找。

**优先级**

- **P0：** 会让用户买错东西，或得到错误结果。
- **P1：** 明显影响推荐质量或记忆可信度。
- **P2：** 架构演进项。

## 结论

当前系统是“路由 → 一个工具 → 解说”的流水线，还不是能根据结果调整下一步的 agent。这个结构的成本和延迟有边界，值得保留，主要缺的是状态可见性、明确的工具结果，以及有上限的重规划。

长期记忆的写入信任设计是好的：按来源分级，模型抽取要有用户原话，会挡商品的内容要确认，并且支持撤销和关闭个性化。问题集中在规则抽取误写、撤销语义、读取语义不一致，以及记住的信息很少被真正使用。

## 优先级总览

| 优先级 | 编号 | 问题 | 证据 | 迁移计划覆盖 |
| --- | --- | --- | --- | --- |
| P0 | STM-1 | 新搜索后焦点商品不失效，会加错商品 | 已复现 | 阶段 7 |
| P0 | STM-2 | 新搜索后对比状态不失效 | 已复现 | 阶段 7 |
| P0 | LTM-5 | 一次评价被写成永久品牌规则 | 已复现 | 无 |
| P0 | LTM-9 | 两套记忆读取语义，Amazon 预算变成硬过滤 | 已复现 | 无 |
| 已修复 | WF-4 | 图谱问答的正则误判 | 已复现，已加回归测试 | 无 |
| P1 | WF-6 | 上下文有两个来源 | 已复现 | 阶段 7 |
| P1 | LTM-2 | 撤销会回滚后来的修改 | 已复现 | 无 |
| P1 | LTM-7 | Amazon 记忆只靠句首正则，品牌不校验 | 已复现 | 阶段 8 部分 |
| P1 | LTM-10 | 鞋码、尺码、肤质被记住但基本没用上 | 读代码 | 无 |
| P1 | WF-2 | 路由器看不到会话状态 | 读代码 | 阶段 7 部分 |
| P1 | WF-7 | 解说拿不到用户上下文 | 读代码 | 阶段 8 部分 |
| P1 | STM-3 | 工作记忆没有统一管理者 | 读代码 | 阶段 7 |
| P1 | STM-4 | 给模型的短期上下文质量低 | 读代码 | 无 |
| P2 | WF-1 | 没有观察和调整环节 | 读代码 | 无 |
| P2 | WF-3 | 路由权分散在五层 | 读代码 | 无 |
| P2 | WF-5 | 工具契约太弱 | 读代码 | 阶段 7 部分 |
| P2 | WF-8 | 一轮对话有三条实现 | 读代码 | 无 |
| P2 | WF-9 | 图谱结果被记成空类目搜索 | 读代码 | 无 |
| P2 | LTM-1 | 偏好 JSON 和条目台账双写 | 读代码 | 无 |
| P2 | LTM-3 | 偏好更新没有并发控制 | 读代码 | 无 |
| P2 | LTM-4 | 元数据未被使用，记忆缺少范围和生命周期 | 读代码 | 无 |
| P2 | LTM-6 | 抽取只看单句，时机偏晚 | 读代码 | 阶段 8 部分 |
| P2 | LTM-8 | 行为推断把 refine 也算作搜索 | 已复现 | 无 |
| P2 | LTM-11 | 风格词追加到每次搜索 | 读代码 | 无 |
| P2 | LTM-12 | 清除不完整，没有保留期 | 读代码 | 无 |

## 一、Agent 工作流与架构

### WF-1 没有观察和调整环节

- **位置：** `backend/agent/orchestrator.py` 的 `_turn_events`、`handle_turn`。
- **现象：** 每轮固定执行“路由 → 一个工具 → 解说”。搜不到结果时只能由解说告知用户，不会自动放宽条件。“加第一个，再和第二个比”这样的组合请求只能执行一个工具。
- **建议：** 让工具返回明确状态，增加最多一次的重规划，例如空结果时放宽一个软条件。不要做不设上限的循环。

### WF-2 路由器看不到会话状态

- **位置：** `backend/agent/intent_router.py:238` 的 `_build_user_content`。
- **现象：** 路由器只收到最近 4 条消息和当前问题，不知道当前列表、焦点商品、待办动作和已有筛选条件。schema 里写了“若上下文里看不到 last_hits 提示”，但这个提示实际没有传入。模型输出的 `confidence` 也没有任何地方使用。
- **建议：** 给路由器一份精简的状态快照，包括列表类型、商品数量和标题、焦点、待办、当前条件。`confidence` 要么接入澄清逻辑，要么删除。

### WF-3 路由权分散在五层

- **位置：** `backend/agent/orchestrator.py:495` 的 `_safe_route`、`_apply_post_route_guards`、`_with_knowledge_route`。
- **现象：** 待办抢占、看更多、模型路由、后置规则、图谱正则依次决定工具，工具内部还要再判断子动作（`cart.py` 有 872 行）。后置规则的词表大多是中文，英文模式下基本失效。
- **建议：** 合并成一个有明确顺序的策略层，每条规则按语言提供信号，并在 trace 里记录是哪条规则做的决定。

### WF-4 图谱问答的正则误判（已修复，2026-11-09）

- **位置：** `backend/agent/amazon.py:30` 的 `knowledge_route`，`backend/i18n/en.py` 的 `SIMILAR_RE`、`ACCESSORY_RE`。
- **修复：** 相似和配件问法必须指向屏幕上的商品，指代词要出现在请求末尾。带明确预算的相似请求交回普通路由，因为图谱工具无法应用预算。回归用例在 `backend/graph/tests/test_graph.py`。
- **修复前的复现：** 屏幕上有商品时，下面的句子会走错工具。

| 输入 | 实际路由 | 应该 |
| --- | --- | --- |
| `Find me earbuds similar to AirPods under $50` | `similar` | 普通搜索 |
| `vegan leather alternatives` | `similar` | 普通搜索 |
| `a phone case for travel that will last` | `accessories` | 普通搜索 |
| `covers for my couch that last` | `accessories` | 普通搜索 |

- **后续：** 正则仍是临时方案。完成 WF-2 和 WF-3 后，可以把这三类问题放进路由器的工具枚举，由模型结合状态判断。

### WF-5 工具契约太弱

- **位置：** `backend/agent/tools/base.py`，`backend/agent/orchestrator.py:687` 的 `_safe_run_tool`，`backend/agent/memory_service.py` 的 `events_from_tool_result`。
- **现象：**
  - 工具入参基本只有字符串，`slots` 只有 `more_label`、`base_parsed`、`top_k`，每个工具自己重新解析。
  - compare 和 product_detail 用原句，其他工具用改写句。
  - `ToolResult` 没有成功、空结果、需澄清的状态，下游只能猜 payload。
  - 记忆模块依赖只用于调试的 `payload.debug.parsed`。
- **建议：** 工具输入改为结构化参数，输出增加 `status`，把记忆需要的字段从 `debug` 移到正式契约。

### WF-6 上下文有两个来源（已复现）

- **位置：** `backend/search/catalog_understanding.py:394` 的 `merge_turn`，以及路由器的 `rewritten_query`。
- **复现：**
  - `smartwatches` 之后依次说 `Under $80`、`Under $50`、`with GPS`、`Under $40`，检索词变成 `Under $40 with GPS Under $50 Under $80 smartwatches`。
  - 走 refine 路径时，从 `smartwatches under $80, no Sony` 换成 `coffee machine`，仍然保留 $80 和“不要 Sony”。
- **建议：** 结构化条件作为唯一的上下文来源，改写文本不再携带约束。每个条件支持保留、设置、清除，换品类时清掉不适用的条件。

### WF-7 解说拿不到用户上下文

- **位置：** `backend/agent/composer.py:297` 的 `_build_messages`。
- **现象：** 解说只收到工具结果，没有对话历史。英文分支使用 `user_msg_parts[:2]`，丢掉了 `composer_hint`，所以长期记忆和推荐依据都到不了英文解说。这些提示目前也还是中文。
- **建议：** 传入精简的用户上下文和英文提示，并继续要求内容只能来自工具结果。

### WF-8 一轮对话有三条实现

- **位置：** `backend/agent/orchestrator.py` 的 `handle_turn`、`_turn_events`、`_image_turn_events`。
- **现象：** 三条路径各写一遍流程。阻塞路径没有取消回合和中断处理，已经出现行为差异。
- **建议：** 抽出一条统一的回合流程，流式和阻塞只是不同的输出方式。

### WF-9 图谱结果被记成空类目搜索

- **位置：** `backend/agent/tools/knowledge.py` 的 `_cards`，`backend/agent/memory_service.py` 的 `events_from_tool_result`。
- **现象：** 相似和配件结果的 `tool_name` 是 `recommend`，但没有 `debug.parsed`，会写入一条类目为空的搜索事件，活动记录里还会显示 “Something similar but cheaper”。
- **建议：** 随 WF-5 的结构化输出一起修复，这两类结果不计入搜索事件，或记录真实类目。

## 二、短期记忆

当前有三层：最近 10 条消息、工作记忆字典、500 字符的会话摘要。

### STM-1 新搜索后焦点商品不失效（已复现，P0）

- **位置：** `backend/agent/session.py:149` 的 `remember_search`，`backend/agent/tools/cart.py` 的第 4 步焦点兜底（基线约 300 行）。
- **复现：** 设置 `last_focus_product_id = p_digital_001`，然后新搜出两款跑鞋，说“把它加进来”或“加入购物车”，实际选中的是 `p_digital_001`，来源 `focus`。
- **建议：** 出现新列表时清除焦点，或要求焦点必须属于当前列表。没有明确目标时追问用户。

### STM-2 新搜索后对比状态不失效（已复现，P0）

- **位置：** `backend/agent/session.py:149`，`backend/agent/orchestrator.py` 的 `_apply_post_route_guards`。
- **复现：** 有 `last_comparison` 时新搜耳机，然后说“换成 512G 的”，路由被改到 `compare`，原因是 `spec switch on last comparison`。
- **建议：** 对比状态绑定到当前视图，切换视图时失效。

### STM-3 工作记忆没有统一管理者

- **位置：** `backend/agent/session.py:47` 的 `WorkingMemory`。
- **现象：** 实际有 12 个键，类型里只声明了 6 个。`pending_cart`、`pending_add`、`pending_detail`、`pending_compare` 由工具和编排器分别写入和清理，也没有过期机制，下一轮可能被强制路由回旧任务。
- **建议：** 用类型化的 `ShoppingTask` 集中管理条件、当前视图、焦点和待办，状态变化统一经过一个入口，待办带过期轮数。

### STM-4 给模型的短期上下文质量低

- **位置：** `backend/agent/session.py:112` 的 `recent_text`，`_update_session_summary`。
- **现象：**
  - 历史保存完整的 JSON 回复，并不截断地传给路由器。
  - 窗口只有 4 条消息，约两轮。
  - 摘要只是末尾 500 字原文，可能截在 JSON 中间，关键约束没有被提炼。
  - 英文模式下标签仍是“用户”“助手”“会话摘要”。
- **建议：** 历史只存精简摘要，例如“展示了 5 款智能手表，上限 $80”。结构化状态单独提供，标签跟随语言。

## 三、长期记忆

当前结构：`user_preferences` 保存偏好 JSON，`memory_items` 保存条目台账，`user_events` 保存行为事件，`preference_undo_tokens` 保存撤销快照。

### 存储

#### LTM-1 偏好 JSON 和条目台账双写

- **位置：** `backend/store/user_memory_store.py:452` 的 `_sync_items`，`list_items`。
- **现象：** JSON 是事实来源，台账由它同步而来。两边不在同一事务里，同步失败会被吞掉，读取时也会写入。
- **建议：** 以 `memory_items` 为唯一事实来源，偏好视图由它生成。

#### LTM-2 撤销会回滚后来的修改（已复现）

- **位置：** `backend/store/user_memory_store.py:348` 的 `undo_update`。
- **复现：** 先记品牌 Nike，再记预算 500，然后撤销品牌，结果品牌和预算都被清空。原因是撤销时恢复了整份快照。
- **建议：** 按条目撤销，只反转对应的那一项。

#### LTM-3 偏好更新没有并发控制

- **位置：** `backend/store/user_memory_store.py` 的 `apply_update`、`put_preference`，`backend/agent/memory_service.py` 的 `extract_in_background`。
- **现象：** 整份 JSON 先读后写，后台抽取、前台更新和页面保存可能互相覆盖。
- **建议：** 改成按条目写入后自然解决，LTM-1 完成时一并验证。

#### LTM-4 元数据未被使用，记忆缺少范围和生命周期

- **现象：** `confidence` 只用于筛选行为推断的品牌，`use_count`、`last_used_at` 只用于展示。品牌偏好没有类目范围，用户陈述的记忆不会过期，备注会无限追加。品牌喜欢和排除的冲突处理只比较完全相同的字符串，Nike 和耐克不算同一个。
- **建议：** 增加品类范围、更新时间和有效策略。品牌比较先做归一。

### 提取

#### LTM-5 一次评价被写成永久规则（已复现，P0）

- **位置：** `backend/agent/memory_extractor.py:251` 的 `_extract_brand_exclude`、`_extract_brand_include`。
- **复现：**

| 用户的话 | 写入结果 |
| --- | --- |
| 这款苹果手机的颜色我不喜欢 | 永久排除 Apple |
| 我不喜欢华为这个配色 | 永久排除华为 |
| 上次买的小米手环不喜欢 | 永久排除小米 |
| 我一般不买太贵的苹果 | 永久排除 Apple |
| 我喜欢苹果味的牛奶 | 喜欢 Apple |

- **不一致：** 同样的内容走模型抽取时要用户确认，走规则却立即生效。
- **建议：** 抽取时先判断针对的是商品、品牌还是属性，以及是否长期有效。任何会挡掉商品的记忆，无论来源，都要满足同一个确认标准。

#### LTM-6 抽取只看单句，时机偏晚

- **位置：** `backend/api/main.py` 的 `_apply_realtime_preference_updates`，`backend/agent/memory_service.py` 的 `extract_in_background`。
- **现象：** 抽取只看当前一句话，缺少上下文。流式回合到 `done` 事件才抽取，所以本轮用不上，被中断的回合也不会抽取。模型抽取只在规则什么都没抽到时运行，提示词和判断词也都是中文。
- **建议：** 显式的长期陈述在工具执行前处理，模型抽取带上最近一两轮，并按语言提供提示词。

#### LTM-7 Amazon 记忆只靠句首正则，品牌不校验（已复现）

- **位置：** `backend/agent/memory_service.py:311` 的 `_english_memory_update`，`process_utterance`，`backend/catalog_config.py` 的 `llm_memory`、`inferred_memory`。
- **复现：**

| 用户的话 | 写入结果 | 实际效果 |
| --- | --- | --- |
| Remember I never buy Sony products | 品牌排除 `Sony products` | 页面显示已排除，搜索结果 10/10 仍是 Sony |
| Remember I avoid alcohol | 品牌排除 `alcohol` | 没有进入成分忌讳 |
| I usually prefer cheap stuff | 喜欢品牌 `cheap stuff` | 无意义条目 |

- **其他：** 模型抽取和行为推断在 Amazon 下关闭，`CatalogSearchService` 也没有读取 `avoid_terms`。
- **建议：** 品牌对照 `brand_canonical` 校验，无法识别时不要声称已排除。接入英文模型抽取，并让 `avoid_terms` 对接 `excluded_attributes`。

#### LTM-8 行为推断把 refine 也算作搜索（已复现）

- **位置：** `backend/agent/memory_inference.py:81` 的 `_infer_budgets`，`events_from_tool_result`。
- **复现：** 同一会话里 3 次筛选（500、450、400）就会生成“常用预算 450”的建议。
- **其他：** 品牌推断有时间衰减，预算和类目推断没有。
- **建议：** 按会话去重，要求跨多个会话才算习惯，并统一衰减策略。

### 使用

#### LTM-9 两套记忆读取语义（已复现，P0）

- **位置：** `backend/search/search_service.py:408` 的 `_apply_user_profile`，`backend/search/catalog_understanding.py:394` 的 `merge_turn`。
- **复现：** 保存 `my budget is usually 20` 后搜 `coffee machine`，候选从 100 降到 7，最高价从 $799.99 降到 $19.99，`personalization` 信号为空，用户不会被告知。
- **矛盾：** 演示目录的注释明确写着平时预算不能让搜索一条结果都没有，只作软信号。
- **建议：** 用一张策略表和一个统一函数决定每类记忆是硬约束、软信号还是只用于解说，以及适用哪些品类。两种目录共用。

#### LTM-10 鞋码、尺码、肤质被记住但基本没用上

- **位置：** `backend/agent/tools/recommend.py` 的 `_personalization_hint`，`backend/store/sku_select.py`，`backend/agent/tools/cart.py`。
- **现象：** 这些属性只作为中文推荐解说的提示。选规格、购物车、详情、对比都没有读取，用户加购时仍要重新选尺码。
- **建议：** 选规格时默认使用已记住的尺码并告知用户，详情和对比的回答结合肤质等属性。

#### LTM-11 风格词追加到每次搜索

- **位置：** `backend/search/search_service.py:408` 的 `_apply_user_profile`。
- **现象：** `preference_keywords` 和 `style_tags` 不看品类，直接拼进检索词，“轻量”也会加到食品搜索上。
- **建议：** 按品类范围和相关性选择要使用的记忆。

#### LTM-12 清除不完整，没有保留期

- **位置：** `backend/store/user_memory_store.py:635` 的 `clear_user`，`backend/store/session_store.py`。
- **现象：** “彻底忘掉”只删偏好、条目、事件和撤销记录，`agent_sessions` 没有 `user_id` 列，对话无法一并删除。会话和行为事件也没有过期清理，后台抽取还可能在清除后写回。
- **建议：** 会话关联用户，明确保留期，清除时取消或忽略尚未完成的抽取。

## 目标形态

**工作流**

```text
状态快照 → 规划器输出结构化动作 → 策略校验 → 执行工具 → 明确的工具状态
                                                    ↓
                                         最多重规划一次
                                                    ↓
                                    解说 → 统一提交状态和记忆
```

**短期记忆：** 用 `ShoppingTask` 统一管理。一旦出现新列表，焦点、对比和待办随之失效。历史只存精简摘要。

**长期记忆：**

- `memory_items` 作为唯一事实来源，按条目撤销。
- 一张策略表决定每类记忆的作用方式和适用品类，所有目录共用。
- 抽取结果先判断目标类型和是否长期有效，再决定写入还是待确认。

**暂不建议：**

- 自由 ReAct 循环：成本和延迟难以控制，导购场景有上限的重规划就够了。
- 向量记忆：每位用户只有几十条偏好，结构化查询已经足够。

## 建议的处理顺序

1. 复测本文所有问题，标记迁移期间已解决的项。
2. 修复 STM-1、STM-2，避免加错商品。
3. 修复 LTM-5、LTM-9、LTM-7，并建立统一的记忆策略表。
4. 修复 LTM-2、LTM-10，让撤销可靠，让记住的尺码真正发挥作用。
5. 完成 WF-2、WF-5、WF-6，再加入 WF-1 的有限重规划。
6. 处理其余 P2 项。
