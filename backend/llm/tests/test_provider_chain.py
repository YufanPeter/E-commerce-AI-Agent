from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm.client import (
    ChatBackend,
    _ChainChatCompletions,
    _provider_chain,
    get_client,
    get_model_id,
    get_provider_name,
)


class _Fake:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class _HTTPError(Exception):
    def __init__(self, status_code, msg):
        super().__init__(msg)
        self.status_code = status_code


def test_extra_body_thinking_is_merged_without_overriding_caller():
    be = ChatBackend(name="ds", completions=_Fake("ok"), model="deepseek-flash",
                     extra_body={"thinking": {"type": "disabled"}})
    chain = _ChainChatCompletions([be])
    chain.create(model="x", messages=[], extra_body={"foo": 1})
    sent = be.completions.calls[0]
    assert sent["model"] == "deepseek-flash"
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}, "foo": 1}


def test_circuit_breaker_skips_failed_backend_until_cooldown():
    bad = ChatBackend(name="bad", completions=_Fake(error=_HTTPError(401, "AuthenticationError")), model="m1")
    good = ChatBackend(name="good", completions=_Fake("ok"), model="m2")
    chain = _ChainChatCompletions([bad, good])
    assert chain.create(model="x", messages=[]) == "ok"
    assert len(bad.completions.calls) == 1
    assert bad.breaker.is_open()
    # 第二次直接跳过 bad，不再白等一次
    assert chain.create(model="x", messages=[]) == "ok"
    assert len(bad.completions.calls) == 1
    assert len(good.completions.calls) == 2


def test_all_backends_open_fails_fast_then_probes_primary(monkeypatch):
    bad = ChatBackend(name="bad", completions=_Fake(error=RuntimeError("down")), model="m1")
    chain = _ChainChatCompletions([bad])
    with pytest.raises(RuntimeError, match="down"):
        chain.create(model="x", messages=[])
    assert bad.breaker.is_open()
    # 探测间隔内：不再打后端，直接抛最近的错误
    bad.completions.error = None
    bad.completions.result = "back"
    with pytest.raises(RuntimeError, match="down"):
        chain.create(model="x", messages=[])
    assert len(bad.completions.calls) == 1
    # 过了探测间隔：放一次主后端去试，成功即关闭熔断
    monkeypatch.setattr(bad.breaker, "_probe_interval", 0.0)
    assert chain.create(model="x", messages=[]) == "back"
    assert not bad.breaker.is_open()


def test_named_tool_choice_rejection_retries_with_required():
    fake = _Fake()
    attempts = []

    def create(**kwargs):
        attempts.append(kwargs["tool_choice"])
        if isinstance(kwargs["tool_choice"], dict):
            raise _HTTPError(400, "invalid tool_choice")
        return "ok"

    fake.create = create
    chain = _ChainChatCompletions([ChatBackend(name="x", completions=fake, model="m")])
    out = chain.create(model="m", messages=[], tools=[], tool_choice={"type": "function", "function": {"name": "f"}})
    assert out == "ok"
    assert attempts == [{"type": "function", "function": {"name": "f"}}, "required"]


def test_stream_falls_back_before_first_chunk_and_not_after():
    def broken():
        raise ConnectionError("boom")
        yield  # pragma: no cover

    primary = ChatBackend(name="p", completions=_Fake(result=broken()), model="m1")
    fallback = ChatBackend(name="f", completions=_Fake(result=iter(["a", "b"])), model="m2")
    chain = _ChainChatCompletions([primary, fallback])
    assert list(chain.create(model="x", messages=[], stream=True)) == ["a", "b"]
    assert fallback.completions.calls[0]["model"] == "m2"

    def half():
        yield "first"
        raise ConnectionError("mid")

    p2 = ChatBackend(name="p", completions=_Fake(result=half()), model="m1")
    f2 = ChatBackend(name="f", completions=_Fake(result=iter(["x"])), model="m2")
    gen = _ChainChatCompletions([p2, f2]).create(model="x", messages=[], stream=True)
    assert next(gen) == "first"
    with pytest.raises(ConnectionError):
        next(gen)


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("LLM_PROVIDER", "LLM_FALLBACK_PROVIDERS", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL",
                "DASHSCOPE_API_KEY", "DASHSCOPE_MODEL", "ARK_API_KEY", "ARK_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("llm.client._load_env_once", lambda: None)
    get_client.cache_clear()
    yield
    get_client.cache_clear()


def test_provider_auto_picks_deepseek_first(clean_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k2")
    monkeypatch.setenv("DASHSCOPE_MODEL", "qwen3-vl-flash")
    assert get_provider_name() == "deepseek"
    assert get_model_id() == "deepseek-flash"
    assert _provider_chain() == ["deepseek", "dashscope"]
    client = get_client()
    names = [b.name for b in client.backends]
    assert names == ["deepseek", "dashscope"]
    assert client.backends[0].extra_body == {"thinking": {"type": "disabled"}}


def test_explicit_provider_and_fallback_list(clean_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("ARK_API_KEY", "k3")
    monkeypatch.setenv("ARK_MODEL", "ep-1")
    monkeypatch.setenv("LLM_PROVIDER", "ark")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDERS", "")
    assert get_provider_name() == "ark"
    assert get_model_id() == "ep-1"
    assert [b.name for b in get_client().backends] == ["ark"]


def test_missing_all_providers_raises(clean_env):
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        get_provider_name()


def test_fast_and_vision_roles_keep_requested_model_on_primary(clean_env, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setenv("DASHSCOPE_MODEL", "deepseek-v4.1-flash")
    monkeypatch.setenv("DASHSCOPE_FAST_MODEL", "qwen-flash")
    monkeypatch.setenv("DASHSCOPE_VISION_MODEL", "qwen3-vl-plus")
    monkeypatch.setenv("ARK_API_KEY", "k2")
    monkeypatch.setenv("ARK_MODEL", "ep-1")
    assert get_model_id() == "deepseek-v4.1-flash"
    assert get_model_id(role="fast") == "qwen-flash"
    assert get_model_id(role="vision") == "qwen3-vl-plus"
    client = get_client()
    primary, fallback = client.backends
    assert primary.extra_body == {"enable_thinking": False}
    assert primary.adapt_kwargs({"model": "qwen-flash"})["model"] == "qwen-flash"
    assert primary.adapt_kwargs({"model": "something-else"})["model"] == "deepseek-v4.1-flash"
    # fallback 后端不认识 qwen-flash，改写成自己的模型
    assert fallback.adapt_kwargs({"model": "qwen-flash"})["model"] == "ep-1"


def test_fast_role_falls_back_to_default_when_unset(clean_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    assert get_model_id(role="fast") == "deepseek-flash"
