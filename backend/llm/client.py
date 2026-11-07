from __future__ import annotations

"""大模型、向量与 rerank 客户端的统一入口。

Chat：DeepSeek / 百炼 / 豆包 三家都兼容 OpenAI SDK，由 ``LLM_PROVIDER``
选主 provider，其余已配置的自动成为 fallback，并带熔断（见下方
``_ChainChatCompletions``）。推荐 ``deepseek-flash``：文本、图片输入、
function calling 一个模型全包，首字快且便宜。

Embedding / Rerank：DeepSeek 没有向量与精排接口，这两条链路仍走百炼
（``DASHSCOPE_API_KEY``），豆包向量 / 智谱 Rerank 保留为 fallback。

本模块只做两件事：
1. 从环境变量加载各 provider 配置；
2. 提供进程内单例客户端和统一的多模态向量调用入口。
"""

import os
import logging
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, TYPE_CHECKING

import httpx
from dotenv import load_dotenv

if TYPE_CHECKING:
    from openai import OpenAI

DEFAULT_ZHIPU_RERANK_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/rerank"
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_DASHSCOPE_RERANK_BASE_URL = (
    "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
)
MULTIMODAL_EMBEDDING_PATH = (
    "/services/embeddings/multimodal-embedding/multimodal-embedding"
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_env_once() -> None:
    """从仓库根的 .env 加载配置，已存在的环境变量不会被覆盖。"""
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def _openai_client(
    *,
    api_key: str,
    base_url: str,
    timeout: float,
    max_retries: int,
) -> OpenAI:
    from openai import OpenAI

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


# ---------------------------------------------------------------------------
# Chat：多 provider 链 + 熔断
# ---------------------------------------------------------------------------
#
# 支持三家 OpenAI 兼容的 Chat provider：
#   - deepseek : DeepSeek（deepseek-flash 同时支持文本 + 图片输入 + function calling）
#   - dashscope: 阿里云百炼（qwen3-vl-*）
#   - ark      : 火山方舟 / 豆包
# ``LLM_PROVIDER`` 指定主 provider；其余已配置的 provider 自动成为 fallback。
# 每个 provider 带一个简单熔断器：调用失败后在冷却期内直接跳过它，避免每条
# 请求都先在挂掉的主服务上白等一次（这正是"首字慢"的一个来源）。

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3/"

_PROVIDER_ORDER: tuple[str, ...] = ("deepseek", "dashscope", "ark")


def _fallback_model_kwargs(kwargs: dict[str, Any], model: str | None) -> dict[str, Any]:
    fallback_kwargs = dict(kwargs)
    if model:
        fallback_kwargs["model"] = model
    return fallback_kwargs


def _is_hard_failure(exc: Exception) -> bool:
    """鉴权失败 / 模型不存在：短时间内重试没有意义，熔断更久。"""
    status = getattr(exc, "status_code", None)
    if status in (401, 403, 404):
        return True
    text = str(exc)
    return "AuthenticationError" in text or "model_not_found" in text or "Model not exist" in text


def _is_tool_choice_rejection(exc: Exception) -> bool:
    """个别 provider 不接受指定函数名的 tool_choice，这类 400 可换成 required 重试。"""
    status = getattr(exc, "status_code", None)
    return status == 400 and "tool_choice" in str(exc)


def close_stream(stream: Any) -> None:
    """关闭 LLM 流，释放上游 HTTP 连接。用户中断时靠它让模型停止生成，避免继续计费。"""
    close = getattr(stream, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception as exc:  # noqa: BLE001 - 收尾失败不能影响主流程
        logger.debug("closing LLM stream failed: %r", exc)


class _CircuitBreaker:
    def __init__(self, soft_cooldown: float, hard_cooldown: float, probe_interval: float = 5.0) -> None:
        self._soft = soft_cooldown
        self._hard = hard_cooldown
        self._probe_interval = probe_interval
        self._open_until = 0.0
        self._last_failure_at = 0.0
        self.last_error: str | None = None
        self.last_exc: Exception | None = None

    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def may_probe(self) -> bool:
        """全部后端都熔断时，只允许每隔 probe_interval 探一次主后端；其余调用直接快速失败。

        没有它，所有 key 都失效时每条请求里的 3~4 次 LLM 调用仍会各白等一次。"""
        return time.monotonic() - self._last_failure_at >= self._probe_interval

    def record_failure(self, exc: Exception) -> None:
        cooldown = self._hard if _is_hard_failure(exc) else self._soft
        now = time.monotonic()
        self._open_until = now + cooldown
        self._last_failure_at = now
        self.last_error = f"{type(exc).__name__}: {exc}"[:200]
        self.last_exc = exc

    def record_success(self) -> None:
        self._open_until = 0.0
        self.last_error = None
        self.last_exc = None


def _cooldowns() -> tuple[float, float, float]:
    _load_env_once()
    return (
        float(os.getenv("LLM_FAIL_COOLDOWN", "30")),
        float(os.getenv("LLM_HARD_FAIL_COOLDOWN", "300")),
        float(os.getenv("LLM_PROBE_INTERVAL", "5")),
    )


@dataclass
class ChatBackend:
    """一个可调用的 chat.completions 后端 + 其模型名 / 额外参数。"""

    name: str
    completions: Any
    model: str | None = None            # None 表示沿用调用方传入的 model
    extra_body: dict[str, Any] | None = None
    # 该后端也认得的其它模型名（如分级用的 fast / vision 模型）：调用方传这些时不改写。
    models: frozenset[str] = frozenset()
    breaker: _CircuitBreaker = field(default_factory=lambda: _CircuitBreaker(*_cooldowns()))

    def adapt_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        requested = kwargs.get("model")
        keep = requested is not None and (requested == self.model or requested in self.models)
        out = _fallback_model_kwargs(kwargs, None if keep else self.model)
        if self.extra_body:
            merged = dict(self.extra_body)
            merged.update(out.get("extra_body") or {})
            out["extra_body"] = merged
        return out


class _ChainChatCompletions:
    """按顺序尝试多个后端；流式调用只在首个 chunk 之前允许切换。"""

    def __init__(self, backends: list[ChatBackend]) -> None:
        if not backends:
            raise RuntimeError("至少需要配置一个 Chat provider。")
        self._backends = backends

    @property
    def backends(self) -> list[ChatBackend]:
        return list(self._backends)

    def _candidates(self) -> list[ChatBackend]:
        live = [b for b in self._backends if not b.breaker.is_open()]
        if live:
            return live
        # 全部熔断：按探测间隔放一次主后端去试，其余调用直接快速失败（抛最近的错误），
        # 让上层立刻走确定性降级，而不是每次都白等一个超时。
        primary = self._backends[0]
        if primary.breaker.may_probe():
            return [primary]
        raise primary.breaker.last_exc or RuntimeError(f"{primary.name} 熔断中：{primary.breaker.last_error}")

    def _call(self, backend: ChatBackend, kwargs: dict[str, Any]):
        call_kwargs = backend.adapt_kwargs(kwargs)
        try:
            return backend.completions.create(**call_kwargs)
        except Exception as exc:
            if _is_tool_choice_rejection(exc) and isinstance(call_kwargs.get("tool_choice"), dict):
                logger.warning("%s 不接受指定函数的 tool_choice，改用 required 重试。", backend.name)
                call_kwargs["tool_choice"] = "required"
                return backend.completions.create(**call_kwargs)
            raise

    def create(self, **kwargs: Any):
        candidates = self._candidates()
        last_exc: Exception | None = None
        for index, backend in enumerate(candidates):
            try:
                result = self._call(backend, kwargs)
            except Exception as exc:  # noqa: BLE001 - 逐个后端兜底
                backend.breaker.record_failure(exc)
                last_exc = exc
                if index + 1 < len(candidates):
                    logger.warning(
                        "%s Chat 调用失败（%s），自动切换到 %s。",
                        backend.name, type(exc).__name__, candidates[index + 1].name,
                    )
                continue
            if not kwargs.get("stream"):
                backend.breaker.record_success()
                return result
            return self._stream_with_fallback(result, backend, candidates[index + 1:], kwargs)
        assert last_exc is not None
        raise last_exc

    def _stream_with_fallback(
        self,
        stream: Any,
        backend: ChatBackend,
        rest: list[ChatBackend],
        kwargs: dict[str, Any],
    ):
        def gen():
            emitted = False
            try:
                for chunk in stream:
                    emitted = True
                    yield chunk
                backend.breaker.record_success()
            except Exception as exc:  # noqa: BLE001
                if emitted or not rest:
                    raise
                backend.breaker.record_failure(exc)
                logger.warning(
                    "%s 流式调用在首个 token 前失败（%s），切换到 %s。",
                    backend.name, type(exc).__name__, rest[0].name,
                )
                yield from _ChainChatCompletions(rest).create(**kwargs)
            finally:
                close_stream(stream)

        return gen()


class _FallbackChatCompletions(_ChainChatCompletions):
    """兼容旧接口：primary + 单个 fallback。"""

    def __init__(self, primary: Any, fallback: Any | None, fallback_model: str | None) -> None:
        backends = [ChatBackend(name="primary", completions=primary)]
        if fallback is not None and fallback_model:
            backends.append(ChatBackend(name="fallback", completions=fallback, model=fallback_model))
        super().__init__(backends)


class _ChatNamespace:
    def __init__(self, completions: _ChainChatCompletions) -> None:
        self.completions = completions


class _FallbackOpenAI:
    """只暴露项目使用的 ``chat.completions`` 兼容表面。"""

    def __init__(self, backends: list[ChatBackend]) -> None:
        self.chat = _ChatNamespace(_ChainChatCompletions(backends))

    @property
    def backends(self) -> list[ChatBackend]:
        return self.chat.completions.backends


def _provider_config(name: str) -> dict[str, Any] | None:
    """读取某 provider 的 key/base_url/model；缺 key 或缺 model 返回 None。"""
    _load_env_once()
    timeout = float(os.getenv("LLM_TIMEOUT", os.getenv("DASHSCOPE_TIMEOUT", "30")))
    if name == "deepseek":
        key = os.getenv("DEEPSEEK_API_KEY")
        if not key:
            return None
        extra: dict[str, Any] = {}
        # deepseek-flash 默认可能走思考模式，首字会慢几秒；导购场景全部关掉。
        if os.getenv("DEEPSEEK_THINKING", "0").strip().lower() not in {"1", "true", "yes", "on"}:
            extra["thinking"] = {"type": "disabled"}
        return {
            "api_key": key,
            "base_url": os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL),
            "model": os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
            "fast_model": os.getenv("DEEPSEEK_FAST_MODEL"),
            "vision_model": os.getenv("DEEPSEEK_VISION_MODEL"),
            "composer_model": os.getenv("DEEPSEEK_COMPOSER_MODEL"),
            "timeout": float(os.getenv("DEEPSEEK_TIMEOUT", str(timeout))),
            "max_retries": int(os.getenv("DEEPSEEK_MAX_RETRIES", "0")),
            "extra_body": extra or None,
        }
    if name == "dashscope":
        key = os.getenv("DASHSCOPE_API_KEY")
        model = os.getenv("DASHSCOPE_MODEL")
        if not key or not model:
            return None
        extra = None
        # 百炼托管的 DeepSeek / Qwen3 混合推理模型默认开思考：首个正文 token 会被
        # 思考过程吃掉好几秒（实测 deepseek-v4.1-flash 不关就迟迟不出字）。导购场景一律关掉。
        if _dashscope_model_is_hybrid(model) and os.getenv("DASHSCOPE_THINKING", "0").strip().lower() not in {"1", "true", "yes", "on"}:
            extra = {"enable_thinking": False}
        return {
            "api_key": key,
            "base_url": os.getenv("DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL),
            "model": model,
            "fast_model": os.getenv("DASHSCOPE_FAST_MODEL"),
            "vision_model": os.getenv("DASHSCOPE_VISION_MODEL"),
            "composer_model": os.getenv("DASHSCOPE_COMPOSER_MODEL"),
            "timeout": float(os.getenv("DASHSCOPE_TIMEOUT", str(timeout))),
            "max_retries": int(os.getenv("DASHSCOPE_MAX_RETRIES", "1")),
            "extra_body": extra,
        }
    if name == "ark":
        key = os.getenv("ARK_API_KEY")
        model = os.getenv("ARK_MODEL")
        if not key or not model:
            return None
        return {
            "api_key": key,
            "base_url": os.getenv("ARK_BASE_URL", DEFAULT_ARK_BASE_URL),
            "model": model,
            "fast_model": os.getenv("ARK_FAST_MODEL"),
            "vision_model": os.getenv("ARK_VISION_MODEL"),
            "composer_model": os.getenv("ARK_COMPOSER_MODEL"),
            "timeout": float(os.getenv("ARK_TIMEOUT", str(timeout))),
            "max_retries": int(os.getenv("ARK_MAX_RETRIES", "1")),
            "extra_body": None,
        }
    raise ValueError(f"未知的 LLM provider：{name!r}（可选：{', '.join(_PROVIDER_ORDER)}）")


def _dashscope_model_is_hybrid(model: str) -> bool:
    """百炼上带思考开关的模型：deepseek-v3.1+ / deepseek-v4 / qwen3 系列。"""
    m = (model or "").lower()
    return m.startswith("deepseek") or m.startswith("qwen3")


def get_provider_name() -> str:
    """当前主 Chat provider：``LLM_PROVIDER`` 显式指定，否则按 deepseek → 百炼 → 豆包 自动挑第一个配了 key 的。"""
    _load_env_once()
    explicit = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if explicit:
        if explicit not in _PROVIDER_ORDER:
            raise RuntimeError(f"LLM_PROVIDER={explicit!r} 不支持，可选：{', '.join(_PROVIDER_ORDER)}")
        return explicit
    for name in _PROVIDER_ORDER:
        if _provider_config(name) is not None:
            return name
    raise RuntimeError(
        "没有可用的 Chat provider。请在 .env 里至少配置一组：\n"
        "  DEEPSEEK_API_KEY（推荐，deepseek-flash 文本+图片）\n"
        "  或 DASHSCOPE_API_KEY + DASHSCOPE_MODEL\n"
        "  或 ARK_API_KEY + ARK_MODEL"
    )


def _provider_chain() -> list[str]:
    primary = get_provider_name()
    raw = os.getenv("LLM_FALLBACK_PROVIDERS")
    if raw is not None:
        extras = [x.strip().lower() for x in raw.split(",") if x.strip()]
    else:
        extras = [n for n in _PROVIDER_ORDER if n != primary]
    chain = [primary]
    for name in extras:
        if name not in chain and _provider_config(name) is not None:
            chain.append(name)
    return chain


@lru_cache(maxsize=1)
def get_client() -> Any:
    """主 Chat 客户端；其余已配置 provider 自动作为 fallback 链。"""
    backends: list[ChatBackend] = []
    for name in _provider_chain():
        cfg = _provider_config(name)
        if cfg is None:
            if name == get_provider_name():
                raise RuntimeError(f"LLM_PROVIDER={name} 但缺少对应的 API Key / 模型名，请检查 .env。")
            continue
        client = _openai_client(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            timeout=cfg["timeout"],
            max_retries=cfg["max_retries"],
        )
        backends.append(
            ChatBackend(
                name=name,
                completions=client.chat.completions,
                model=cfg["model"],
                extra_body=cfg["extra_body"],
                models=frozenset(
                    m for m in (cfg.get("fast_model"), cfg.get("vision_model"), cfg.get("composer_model")) if m
                ),
            )
        )
    logger.info("Chat provider 链：%s", " → ".join(b.name for b in backends))
    return _FallbackOpenAI(backends)


def get_model_id(role: str = "default") -> str:
    """返回主 provider 的模型名（fallback 后端会自动替换成各自的模型）。

    role="fast"：分级模型——路由 / query 解析 / 拆解 / 动作分发这类"只要结构化输出"
    的小任务，可用更快的模型（``<PROVIDER>_FAST_MODEL``，如百炼 qwen-flash），
    composer 话术仍用默认模型。未配置时退回默认模型，行为不变。
    """
    cfg = _provider_config(get_provider_name())
    if cfg is None:
        raise RuntimeError("主 Chat provider 缺少 API Key 或模型名，请检查 .env。")
    if role == "fast" and cfg.get("fast_model"):
        return cfg["fast_model"]
    if role == "vision" and cfg.get("vision_model"):
        return cfg["vision_model"]
    if role == "composer" and cfg.get("composer_model"):
        # 话术 / 解说可以换更强的模型（如百炼 deepseek-v4-pro），代价是首字慢约 2s
        return cfg["composer_model"]
    return cfg["model"]


def get_vision_model_id() -> str:
    """看图用的模型名：``<PROVIDER>_VISION_MODEL``，未设置则与对话模型相同。"""
    return get_model_id(role="vision")


def provider_status() -> list[dict[str, Any]]:
    """给 /health 或日志用：各后端熔断状态。"""
    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001
        return [{"name": "none", "error": str(exc)}]
    return [
        {
            "name": b.name,
            "model": b.model,
            "circuit_open": b.breaker.is_open(),
            "last_error": b.breaker.last_error,
        }
        for b in client.backends
    ]


@lru_cache(maxsize=1)
def get_embedding_client() -> OpenAI:
    """百炼原生多模态 embedding 专用客户端单例。"""
    _load_env_once()
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "缺少 DASHSCOPE_API_KEY。对话与 embedding 共用同一个百炼 API Key。"
        )
    base_url = os.getenv(
        "DASHSCOPE_EMBEDDING_BASE_URL",
        DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL,
    )
    timeout = float(os.getenv("DASHSCOPE_TIMEOUT", "30"))
    max_retries = int(os.getenv("DASHSCOPE_MAX_RETRIES", "1"))
    return _openai_client(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


@lru_cache(maxsize=1)
def _get_ark_embedding_client() -> OpenAI | None:
    """返回原豆包多模态向量客户端；未配置时返回 None。"""
    _load_env_once()
    api_key = os.getenv("ARK_EMBEDDING_API_KEY") or os.getenv("ARK_API_KEY")
    model = os.getenv("ARK_EMBEDDING_MODEL")
    if not api_key or not model:
        return None
    return _openai_client(
        api_key=api_key,
        base_url=(
            os.getenv("ARK_EMBEDDING_BASE_URL")
            or os.getenv("ARK_BASE_URL")
            or "https://ark.cn-beijing.volces.com/api/v3/"
        ),
        timeout=float(
            os.getenv("ARK_TIMEOUT", os.getenv("DASHSCOPE_TIMEOUT", "30"))
        ),
        max_retries=int(os.getenv("ARK_MAX_RETRIES", "1")),
    )


def get_embedding_model_id() -> str:
    """返回文本与图片共用的百炼多模态向量模型名。"""
    _load_env_once()
    return os.getenv("DASHSCOPE_EMBEDDING_MODEL", "qwen3-vl-embedding")


def get_embedding_dimensions() -> int:
    """返回向量维度；文本索引与图片索引必须保持一致。"""
    _load_env_once()
    return int(os.getenv("DASHSCOPE_EMBEDDING_DIMENSIONS", "2048"))


def _parse_dashscope_embeddings(
    response: Any,
    expected: int,
) -> list[list[float]]:
    """解析百炼向量响应并校验数量。"""
    try:
        items = response["output"]["embeddings"]
        ordered = sorted(items, key=lambda item: int(item["index"]))
        vectors = [list(item["embedding"]) for item in ordered]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("百炼 embedding 返回格式异常。") from exc

    if len(vectors) != expected:
        raise RuntimeError(
            f"百炼 embedding 返回数量异常：请求 {expected} 条，返回 {len(vectors)} 条。"
        )
    return vectors


def embed_multimodal_primary(
    contents: list[dict[str, str]],
    *,
    model: str | None = None,
) -> list[list[float]]:
    """只调用百炼多模态向量接口，不跨向量空间 fallback。"""
    if not contents:
        return []

    response = get_embedding_client().post(
        MULTIMODAL_EMBEDDING_PATH,
        body={
            "model": model or get_embedding_model_id(),
            "input": {"contents": contents},
            "parameters": {"dimension": get_embedding_dimensions()},
        },
        cast_to=object,
    )
    return _parse_dashscope_embeddings(response, len(contents))


def embed_multimodal_ark(
    contents: list[dict[str, str]],
) -> list[list[float]]:
    """只调用原豆包多模态向量接口，供备用索引使用。"""
    if not contents:
        return []
    fallback = _get_ark_embedding_client()
    fallback_model = os.getenv("ARK_EMBEDDING_MODEL")
    if fallback is None or not fallback_model:
        raise RuntimeError("豆包 Embedding fallback 未配置。")
    return _embed_with_ark(fallback, fallback_model, contents)


def embed_multimodal_with_provider(
    contents: list[dict[str, str]],
    *,
    model: str | None = None,
) -> tuple[str, list[list[float]]]:
    """百炼失败时改用豆包，并返回实际 provider 供调用方选择匹配的索引。"""
    try:
        return "dashscope", embed_multimodal_primary(contents, model=model)
    except Exception as primary_exc:
        logger.warning("百炼 Embedding 调用失败，自动切换到豆包 fallback。")
        try:
            return "ark", embed_multimodal_ark(contents)
        except Exception as fallback_exc:
            raise fallback_exc from primary_exc


def embed_multimodal(
    contents: list[dict[str, str]],
    *,
    model: str | None = None,
) -> list[list[float]]:
    """兼容入口；调用方不关心 provider 时只返回向量。"""
    _, vectors = embed_multimodal_with_provider(contents, model=model)
    return vectors


def _embed_with_ark(
    client: Any,
    model: str,
    contents: list[dict[str, str]],
) -> list[list[float]]:
    """把百炼 content 格式映射到原豆包多模态向量接口。"""
    vectors: list[list[float]] = []
    for content in contents:
        if "text" in content:
            ark_input = {"type": "text", "text": content["text"]}
        elif "image" in content:
            ark_input = {
                "type": "image_url",
                "image_url": {"url": content["image"]},
            }
        else:
            raise ValueError(f"豆包 fallback 不支持的 embedding 输入：{content!r}")
        response = client.post(
            "/embeddings/multimodal",
            body={"model": model, "input": [ark_input]},
            cast_to=object,
        )
        vectors.append(list(response["data"]["embedding"]))
    return vectors


class _FallbackRerankClient:
    """百炼 qwen3-rerank 主用，智谱 Rerank 自动兜底。"""

    def __init__(
        self,
        primary: httpx.Client,
        fallback: httpx.Client | None,
        fallback_url: str | None,
        fallback_model: str | None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_url = fallback_url
        self._fallback_model = fallback_model

    def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        try:
            response = self._primary.post(url, json=json)
            response.raise_for_status()
            return response
        except Exception as primary_exc:
            if (
                self._fallback is None
                or not self._fallback_url
                or not self._fallback_model
            ):
                raise
            logger.warning("百炼 Rerank 调用失败，自动切换到智谱 fallback。")
            fallback_payload = dict(json)
            fallback_payload["model"] = self._fallback_model
            try:
                return self._fallback.post(
                    self._fallback_url,
                    json=fallback_payload,
                )
            except Exception as fallback_exc:
                raise fallback_exc from primary_exc


@lru_cache(maxsize=1)
def get_rerank_client() -> Any:
    """构建百炼主用、智谱兜底的 rerank HTTP 客户端。"""
    _load_env_once()
    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    if not dashscope_key:
        raise RuntimeError(
            "缺少 DASHSCOPE_API_KEY，无法调用百炼 qwen3-rerank；"
            "不需要 rerank 时请设置 USE_RERANK=0。"
        )
    timeout = float(
        os.getenv("RERANK_TIMEOUT", os.getenv("DASHSCOPE_TIMEOUT", "30"))
    )
    primary = httpx.Client(
        headers={
            "Authorization": f"Bearer {dashscope_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    zhipu_key = os.getenv("ZHIPU_API_KEY")
    fallback = (
        httpx.Client(
            headers={
                "Authorization": f"Bearer {zhipu_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        if zhipu_key
        else None
    )
    return _FallbackRerankClient(
        primary,
        fallback,
        os.getenv("RERANK_BASE_URL", DEFAULT_ZHIPU_RERANK_BASE_URL),
        os.getenv("RERANK_MODEL"),
    )


def get_rerank_base_url() -> str:
    """返回百炼 qwen3-rerank 主接口地址。"""
    _load_env_once()
    return os.getenv(
        "DASHSCOPE_RERANK_BASE_URL",
        DEFAULT_DASHSCOPE_RERANK_BASE_URL,
    )


def get_rerank_model_id() -> str:
    """返回百炼主 rerank 模型名。"""
    _load_env_once()
    return os.getenv("DASHSCOPE_RERANK_MODEL", "qwen3-rerank")
