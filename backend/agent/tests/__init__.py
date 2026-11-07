"""End-to-end and edge-case tests.

分两层：
1. unit 层：mock LLM client / SearchService，验证 orchestrator 在各种异常下的降级行为
2. e2e 层：真实调百炼 + Chroma，验证完整 happy path（需 DASHSCOPE_API_KEY，可用 RUN_E2E=1 控制）

Run:
    cd backend && python -m pytest agent/tests -v
    cd backend && RUN_E2E=1 python -m pytest agent/tests -v
"""
