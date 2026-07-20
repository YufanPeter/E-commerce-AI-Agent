"""End-to-end and edge-case tests.

The suite has two layers:
1. Unit tests mock the LLM client and SearchService to verify orchestrator fallbacks.
2. End-to-end tests call the configured model API and Chroma when `RUN_E2E=1`.

Run:
    cd backend && python -m pytest agent/tests -v
    cd backend && RUN_E2E=1 python -m pytest agent/tests -v
"""
