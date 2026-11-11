"""Shared helpers for graph build steps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import os
from pathlib import Path
import time
from typing import Any

import httpx

from catalog_config import AMAZON_DB_PATH
from llm.client import _openai_client, _provider_config, get_provider_name
from search.llm_parser import _loads_arguments


GRAPH_DIR = AMAZON_DB_PATH.parent / "graph"
ONTOLOGY_PATH = Path(__file__).resolve().parents[1] / "db" / "amazon_graph_ontology.json"
_ATTEMPTS = 4
_BEIJING = timezone(timedelta(hours=8))
_PAYMENT_REQUIRED = 402


class BalanceExhausted(RuntimeError):
    """The provider refused a call for lack of funds; retrying cannot help."""


def is_peak(now: datetime | None = None) -> bool:
    """DeepSeek bills double on weekdays 09:00-12:00 and 14:00-18:00 Beijing time."""
    moment = (now or datetime.now(_BEIJING)).astimezone(_BEIJING)
    return moment.weekday() < 5 and (9 <= moment.hour < 12 or 14 <= moment.hour < 18)


def seconds_until_off_peak(now: datetime | None = None) -> float:
    moment = (now or datetime.now(_BEIJING)).astimezone(_BEIJING)
    if not is_peak(moment):
        return 0.0
    end_hour = 12 if moment.hour < 12 else 18
    return (moment.replace(hour=end_hour, minute=0, second=0, microsecond=0) - moment).total_seconds()


def account_balance_cny() -> float:
    """Remaining DeepSeek balance, read from the provider rather than estimated."""
    _primary()  # Loads the environment.
    response = httpx.get(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"},
        timeout=20,
        trust_env=False,
    )
    response.raise_for_status()
    return next(float(info["total_balance"]) for info in response.json()["balance_infos"] if info["currency"] == "CNY")


@lru_cache(maxsize=1)
def _primary() -> tuple[Any, dict[str, Any]]:
    """The primary provider alone, so a bulk job cannot spill onto a fallback account."""
    config = _provider_config(get_provider_name())
    if config is None:
        raise RuntimeError("The primary chat provider is not configured.")
    client = _openai_client(
        api_key=config["api_key"], base_url=config["base_url"], timeout=config["timeout"], max_retries=0,
    )
    return client, config


@dataclass(frozen=True)
class ToolCall:
    arguments: dict[str, Any]
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int


def call_tool(system: str, user: str, tool: dict[str, Any], timeout: float = 90.0) -> ToolCall:
    """Run one forced function call, retrying transient and malformed responses."""
    name = tool["function"]["name"]
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            client, config = _primary()
            response = client.chat.completions.create(
                model=config.get("fast_model") or config["model"],
                extra_body=config["extra_body"],
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": name}},
                temperature=0.0,
                timeout=timeout,
            )
            calls = response.choices[0].message.tool_calls
            if not calls:
                raise ValueError(f"The model did not call {name}")
            usage = response.usage
            return ToolCall(
                arguments=_loads_arguments(calls[0].function.arguments),
                model=str(response.model),
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                cached_tokens=int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0),
            )
        except Exception as exc:  # noqa: BLE001 - provider and JSON errors share one retry path
            if getattr(exc, "status_code", None) == _PAYMENT_REQUIRED:
                raise BalanceExhausted(str(exc)) from exc
            # Malformed output tends to repeat at temperature 0, so it gets one retry only.
            if attempt == (2 if isinstance(exc, ValueError) else _ATTEMPTS):
                raise
            time.sleep(2.0 * 2 ** (attempt - 1))
    raise AssertionError("unreachable")
