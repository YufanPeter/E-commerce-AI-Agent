# CartPilot 阶段 A 运行证据

记录日期：2026-09-11（Asia/Shanghai）

本文件保存阶段 A 最终决定所依赖的可复现命令和命令输出。所有命令均从项目根目录运行，除非明确写出工作目录。真实 API Key 只由被 Git 忽略的根目录 .env 加载，不在命令、输出或本文件中出现。

早期探索性请求有部分只留下清理过的摘要而未保存完整命令，因此仍在 phase_a_runtime.json 中明确标为历史/部分捕获；下列最终确认命令均完整保存。输出由本任务的命令结果转录，原始数值不做效果解释。

## 1. 依赖与 SQLite

~~~powershell
.\.venv\Scripts\python.exe -m pip check
~~~

~~~text
No broken requirements found.
~~~

SQLite 构建工作目录：backend

~~~powershell
$env:USE_HYBRID='1'; $env:USE_RERANK='0'; & '..\.venv\Scripts\python.exe' -m store.import_product_data --reset
~~~

~~~text
已导入 100 个商品、585 个 SKU、439 条 FAQ、453 条评价到 backend/storage/ecommerce_agent.sqlite3
~~~

商品读取工作目录：backend

~~~powershell
$env:PYTHONUTF8='1'; $env:USE_HYBRID='1'; $env:USE_RERANK='0'; & '..\.venv\Scripts\python.exe' -c "import json,time; from store.product_store import ProductStore; t=time.perf_counter(); d=ProductStore().get_product_detail('p_beauty_010'); assert d is not None; print(json.dumps({'status':'PASS','product_id':d.product_id,'title':d.title,'sku_count':len(d.skus),'faq_count':len(d.faqs),'review_count':len(d.reviews),'elapsed_seconds':round(time.perf_counter()-t,6),'USE_HYBRID':'1','USE_RERANK':'0'},ensure_ascii=False))"
~~~

~~~json
{"status":"PASS","product_id":"p_beauty_010","title":"安热沙金灿倍护防晒乳高倍防水防汗清爽户外面部身体防晒60ml","sku_count":2,"faq_count":6,"review_count":3,"elapsed_seconds":0.007804,"USE_HYBRID":"1","USE_RERANK":"0"}
~~~

SQLite 最终只读验证：

~~~powershell
.\.venv\Scripts\python.exe -c "import sqlite3; p='backend/storage/ecommerce_agent.sqlite3'; c=sqlite3.connect(p); integrity=c.execute('PRAGMA integrity_check').fetchone()[0]; counts={t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('products','product_skus','product_faqs','product_reviews')}; assert integrity=='ok'; assert counts=={'products':100,'product_skus':585,'product_faqs':439,'product_reviews':453}; print({'SQLITE_INTEGRITY':integrity,'COUNTS':counts})"
~~~

~~~text
{'SQLITE_INTEGRITY': 'ok', 'COUNTS': {'products': 100, 'product_skus': 585, 'product_faqs': 439, 'product_reviews': 453}}
~~~

## 2. Chat Router

工作目录：backend

~~~powershell
$env:PYTHONUTF8='1'; $env:USE_HYBRID='1'; $env:USE_RERANK='0'; & '..\.venv\Scripts\python.exe' -c "import json,time; from agent.intent_router import route; from agent.session import AgentSession; t=time.perf_counter(); d=route('推荐一双500元以内的跑鞋',AgentSession(),timeout=15); print(json.dumps({'status':'PASS','model':'glm-5.2','decision':{'tool':d.tool,'rewritten_query':d.rewritten_query,'confidence':d.confidence,'reasoning':d.reasoning},'elapsed_seconds':round(time.perf_counter()-t,6),'USE_HYBRID':'1','USE_RERANK':'0'},ensure_ascii=False))"
~~~

~~~json
{"status":"PASS","model":"glm-5.2","decision":{"tool":"recommend","rewritten_query":"推荐一双500元以内的跑鞋","confidence":"high","reasoning":"用户明确表达了购买意图，有具体品类（跑鞋）和价格约束（500元以内），属于典型的推荐场景。"},"elapsed_seconds":8.023389,"USE_HYBRID":"1","USE_RERANK":"0"}
~~~

初始候选 glm-5.3-flash 曾返回 HTTP 429 / provider code 1113（余额不足或无可用资源包）。该失败的完整内联脚本没有在执行时保存，只保留了清理后的命令摘要、错误码、错误文本和耗时；不得把它描述为具有完整原始命令日志。

## 3. Embedding 探针与代码路径

根目录直接接口探针：

~~~powershell
.\.venv\Scripts\python.exe -c "import os,time,json; from dotenv import load_dotenv; from openai import OpenAI; load_dotenv(); model=os.getenv('ARK_EMBEDDING_MODEL','embedding-3'); base=os.getenv('ARK_EMBEDDING_BASE_URL','https://open.bigmodel.cn/api/paas/v4/').rstrip('/')+'/'; key=os.getenv('ARK_API_KEY') or os.getenv('ARK_EMBEDDING_API_KEY'); assert key, 'No API key configured'; client=OpenAI(api_key=key,base_url=base,timeout=30); t=time.perf_counter(); resp=client.embeddings.create(model=model,input='CartPilot embedding availability probe'); elapsed=time.perf_counter()-t; vec=resp.data[0].embedding; print(json.dumps({'status':'PASS','model_requested':model,'model_returned':getattr(resp,'model',None),'dimensions':len(vec),'usage':getattr(resp,'usage',None).model_dump() if getattr(resp,'usage',None) else None,'elapsed_seconds':round(elapsed,6)},ensure_ascii=False))"
~~~

~~~json
{"status":"PASS","model_requested":"embedding-3","model_returned":"embedding-3","dimensions":2048,"usage":{"prompt_tokens":9,"total_tokens":9,"completion_tokens":0},"elapsed_seconds":2.781496}
~~~

项目 EmbeddingFunction 探针，工作目录：backend

~~~powershell
$env:PYTHONUTF8='1'; $env:USE_HYBRID='1'; $env:USE_RERANK='0'; & '..\.venv\Scripts\python.exe' -c "import json,time; from rag.chroma_store import DoubaoEmbeddingFunction,DEFAULT_EMBEDDING_MODEL; t=time.perf_counter(); vectors=DoubaoEmbeddingFunction()(['CartPilot 项目向量接口验证']); print(json.dumps({'status':'PASS','model':DEFAULT_EMBEDDING_MODEL,'vector_count':len(vectors),'dimensions':len(vectors[0]),'elapsed_seconds':round(time.perf_counter()-t,6),'USE_HYBRID':'1','USE_RERANK':'0'},ensure_ascii=False))"
~~~

~~~json
{"status":"PASS","model":"embedding-3","vector_count":1,"dimensions":2048,"elapsed_seconds":5.170222,"USE_HYBRID":"1","USE_RERANK":"0"}
~~~

TDD 最终定向测试：

~~~powershell
.\.venv\Scripts\python.exe -m pytest backend\rag\tests\test_chroma_store.py -q
~~~

~~~text
..........                                                               [100%]
10 passed in 3.42s
~~~

RED 历史：

- 第一轮：标准批调用尚未实现，4 failed in 0.22s。
- 审查修复第一轮：异常响应索引、模型契约和维度契约尚未实现，4 failed, 4 passed in 3.45s。
- 运行时配置轮：配置函数尚未实现，1 failed in 0.25s。
- 端点契约轮：不同 embedding 端点尚未被拒绝，1 failed；实现后定向测试为 10 passed in 3.42s。

## 4. 独立 Chroma 完整重建

用户明确授权把 1092 个 CartPilot 商品文本块发送到 https://open.bigmodel.cn/api/paas/v4/embeddings。目标目录事先确认位于项目内、与旧索引分离且被 Git 忽略。

工作目录：backend

~~~powershell
$env:PYTHONUTF8='1'; $env:USE_HYBRID='1'; $env:USE_RERANK='0'; $sw=[System.Diagnostics.Stopwatch]::StartNew(); & '..\.venv\Scripts\python.exe' -m rag.build_chroma --reset --persist-dir 'storage\chroma_phase_a_embedding3' --collection 'product_knowledge_embedding3_phase_a' --embedding-model 'embedding-3' --batch-size 64; $code=$LASTEXITCODE; $sw.Stop(); Write-Output ('TOTAL_SECONDS=' + [Math]::Round($sw.Elapsed.TotalSeconds,6)); exit $code
~~~

最终契约版重建输出：

~~~text
Loaded chunks: 1092
Chunk types:
  - marketing_description: 100
  - official_faq: 439
  - product_profile: 100
  - user_review: 453
Categories:
  - 数码电子: 267
  - 服饰运动: 274
  - 美妆护肤: 275
  - 食品饮料: 276
Chroma collection ready: product_knowledge_embedding3_phase_a
Indexed chunks: 1092
Embedding dimensions: 2048
Persist dir: storage\chroma_phase_a_embedding3
TOTAL_SECONDS=51.498336
~~~

该命令之前曾以同一模型和独立目录成功构建两次，耗时 32.914598 秒和 31.518013 秒；因为审查后依次新增持久化模型/维度契约和端点契约，最终证据以上述第三次完整 --reset 重建为准。构建命令没有返回 provider token 用量，因此该字段保持 NOT RETURNED。

## 5. 持久化契约与默认运行时

落盘契约只读验证，工作目录：backend

~~~powershell
$env:PYTHONUTF8='1'; $env:USE_HYBRID='1'; $env:USE_RERANK='0'; & '..\.venv\Scripts\python.exe' -c "import json; from rag.chroma_store import create_collection; c=create_collection(); print(json.dumps({'count':c.count(),'metadata':c.metadata,'configuration':c.configuration_json},ensure_ascii=False,sort_keys=True))"
~~~

~~~json
{"configuration": {"embedding_function": {"config": {"batch_size": 64, "endpoint": "https://open.bigmodel.cn/api/paas/v4/", "model": "embedding-3"}, "name": "openai_compatible_text", "type": "known"}, "hnsw": {"ef_construction": 100, "ef_search": 100, "max_neighbors": 16, "resize_factor": 1.2, "space": "cosine", "sync_threshold": 1000}, "spann": null}, "count": 1092, "metadata": {"embedding_dimensions": 2048, "embedding_endpoint": "https://open.bigmodel.cn/api/paas/v4/", "embedding_model": "embedding-3", "embedding_provider": "openai_compatible_text"}}
~~~

默认运行时真实查询，工作目录：backend

~~~powershell
$env:PYTHONUTF8='1'; $env:USE_HYBRID='1'; $env:USE_RERANK='0'; $sw=[System.Diagnostics.Stopwatch]::StartNew(); & '..\.venv\Scripts\python.exe' -m rag.query_chroma '适合敏感肌的保湿面霜' --top-k 5 --repeat 1 --preview-chars 120; $code=$LASTEXITCODE; $sw.Stop(); Write-Output ('TOTAL_SECONDS=' + [Math]::Round($sw.Elapsed.TotalSeconds,6)); exit $code
~~~

~~~text
collection=product_knowledge_embedding3_phase_a count=1092
init_seconds=2.032
query_run_1_seconds=6.191
1. p_beauty_007:review:4 | p_beauty_007 | user_review | 薇诺娜舒敏保湿特护霜敏感肌修护屏障舒缓干痒保湿面霜50g distance=0.4046
2. p_beauty_012:review:1 | p_beauty_012 | user_review | 理肤泉特安舒缓修复霜滋润型敏感肌保湿修护屏障面霜40ml distance=0.4067
3. p_beauty_007:review:1 | p_beauty_007 | user_review | 薇诺娜舒敏保湿特护霜敏感肌修护屏障舒缓干痒保湿面霜50g distance=0.4081
4. p_beauty_007:marketing | p_beauty_007 | marketing_description | 薇诺娜舒敏保湿特护霜敏感肌修护屏障舒缓干痒保湿面霜50g distance=0.4192
5. p_beauty_007:review:2 | p_beauty_007 | user_review | 薇诺娜舒敏保湿特护霜敏感肌修护屏障舒缓干痒保湿面霜50g distance=0.4283
TOTAL_SECONDS=9.239467
~~~

错误模型拒绝验证，工作目录：backend

~~~powershell
$env:PYTHONUTF8='1'; & '..\.venv\Scripts\python.exe' -c "from pathlib import Path; from rag.chroma_store import create_collection; p=Path('storage/chroma_phase_a_embedding3'); import sys;
try: create_collection(p,'product_knowledge_embedding3_phase_a','different-model')
except RuntimeError as e: print('MODEL_MISMATCH_REJECTED=PASS'); print(type(e).__name__ + ': ' + str(e))
else: raise SystemExit('model mismatch was not rejected')"
~~~

~~~text
MODEL_MISMATCH_REJECTED=PASS
RuntimeError: Chroma collection 'product_knowledge_embedding3_phase_a' embedding model mismatch: stored='embedding-3', requested='different-model'
~~~

错误端点拒绝验证，工作目录：backend。该验证只打开本地 collection，不向占位端点发送请求；预期命令退出码为 1。

~~~powershell
$env:PYTHONUTF8='1'; $env:ARK_EMBEDDING_BASE_URL='https://different.example/v1/'; & '..\.venv\Scripts\python.exe' -c "from rag.chroma_store import create_collection; create_collection()"
~~~

~~~text
RuntimeError: Chroma collection 'product_knowledge_embedding3_phase_a' embedding endpoint mismatch: stored='https://open.bigmodel.cn/api/paas/v4/', requested='https://different.example/v1/'
~~~

SearchService 默认索引验证，工作目录：backend

~~~powershell
$env:PYTHONUTF8='1'; $env:USE_HYBRID='1'; $env:USE_RERANK='0'; & '..\.venv\Scripts\python.exe' -c "from search.search_service import SearchService; s=SearchService(); print({'count':s._retriever.count(),'use_hybrid':s._use_hybrid,'use_rerank':s._use_rerank})"
~~~

~~~text
{'count': 1092, 'use_hybrid': True, 'use_rerank': False}
~~~

## 6. FastAPI /health

工作目录：backend

~~~powershell
$port=18765; if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "Port $port is already in use" }; $env:PYTHONUTF8='1'; $env:USE_HYBRID='1'; $env:USE_RERANK='0'; $env:BACKEND_WARMUP='0'; $args=@('-m','uvicorn','api.main:app','--host','127.0.0.1','--port',[string]$port,'--log-level','warning'); $p=Start-Process -FilePath '..\.venv\Scripts\python.exe' -ArgumentList $args -WorkingDirectory (Get-Location) -PassThru -WindowStyle Hidden; $sw=[System.Diagnostics.Stopwatch]::StartNew(); try { $response=$null; for ($i=0; $i -lt 100; $i++) { try { $response=Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/health" -TimeoutSec 2; break } catch { Start-Sleep -Milliseconds 200 } }; if ($null -eq $response) { throw 'Health endpoint did not become ready' }; $sw.Stop(); Write-Output ('HTTP_STATUS=' + $response.StatusCode); Write-Output ('BODY=' + $response.Content); Write-Output ('READY_SECONDS=' + [Math]::Round($sw.Elapsed.TotalSeconds,6)); Write-Output 'USE_HYBRID=1'; Write-Output 'USE_RERANK=0' } finally { if (-not $p.HasExited) { Stop-Process -Id $p.Id; $p.WaitForExit() }; Write-Output ('PROCESS_EXITED=' + $p.HasExited) }
~~~

~~~text
HTTP_STATUS=200
BODY={"status":"ok"}
READY_SECONDS=2.455023
USE_HYBRID=1
USE_RERANK=0
PROCESS_EXITED=True
~~~

## 7. 完整测试

~~~powershell
$env:PYTHONUTF8='1'; $sw=[System.Diagnostics.Stopwatch]::StartNew(); & '.\.venv\Scripts\python.exe' -m pytest backend -q; $code=$LASTEXITCODE; $sw.Stop(); Write-Output ('TOTAL_SECONDS=' + [Math]::Round($sw.Elapsed.TotalSeconds,6)); exit $code
~~~

~~~text
260 passed, 7 skipped, 3 warnings in 16.95s
TOTAL_SECONDS=19.05547
~~~

三个 warning 均为依赖/API 生命周期弃用告警：Starlette BlockingPortal 别名一项、FastAPI on_event 两项。跳过项未计作通过；没有运行评测数据集。

## 8. 止损计时边界

- captured_at：2026-09-11T02:49:13.8913870+08:00。
- 阶段 A 包含等待用户提供/确认 Key、充值资源、批准商品文本外发以及等待只读代码审查的长暂停；这些暂停不是环境处理时间。
- 早期没有为每段主动文档审阅和用户等待分别启停统一秒表，因此严格的累计主动工作秒数不可独立重建。
- manifest 的 recorded_timed_command_and_process_wait_seconds 只汇总已捕获的命令/进程耗时，是主动环境处理时间的下界，不是完整主动工作总时长。
- 90 分钟检查点的依赖、商品读取、Router、文本向量与失败分类均已实际完成，但缺少可审计的单一检查点时间戳，计时合规应写为 PASS_WITH_MEASUREMENT_LIMITATION。
- 没有观察到连续或累计 6 小时的环境处理；记录的命令时间远低于 6 小时，但因未完整计时，6 小时规则同样写为 PASS_WITH_MEASUREMENT_LIMITATION，不能把墙钟跨度或命令时间下界冒充精确主动工时。

## 9. 安全与范围

- .env 被 Git 忽略且未跟踪。
- 证据文件和 staged diff 使用 .env 中真实 Key 做精确匹配扫描，匹配数为 0。
- artifacts/manifests/step0_baseline.json 未修改。
- 仓库已有 backend/storage/chroma/ HNSW 片段未修改。
- 未运行 Prompt 优化、检索优化或评测数据集；没有效果结论。
