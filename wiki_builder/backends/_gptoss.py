"""gpt-oss 백엔드."""

import os

from wiki_builder.backends._base import (
    _RateLimitError, _RetryableError,
    _to_openai_tools, _build_openai_tool_response,
)

_gptoss_config: dict = {
    "url": "http://apigw-stg.samsungds.net:8000/gpt-oss/1/gpt-oss-120b/v1/chat/completions",
    "api_key": os.getenv("GPTOSS_API_KEY", ""),
    "knox_id": os.getenv("GPTOSS_KNOX_ID", ""),
    "ad_id": os.getenv("GPTOSS_AD_ID", ""),
    "timeout": 300,
}


def _call_gptoss(system: str, user: str, temperature: float, **kwargs) -> str:
    import requests

    cfg = _gptoss_config
    if not cfg["api_key"]:
        return "[LLM 호출 실패] gpt-oss api_key가 설정되지 않았습니다"

    headers = {
        "Content-Type": "application/json",
        "x-dep-ticket": cfg["api_key"],
        "Send-System-Name": "Tracer",
        "User-Id": cfg["knox_id"],
        "User-Type": cfg["ad_id"],
    }

    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": kwargs.get("max_tokens", 16384),
    }

    try:
        resp = requests.post(
            cfg["url"],
            headers=headers,
            json=payload,
            proxies={"http": None, "https": None},
            timeout=cfg["timeout"],
        )
        if resp.status_code == 429:
            raise _RateLimitError(f"HTTP 429: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise _RetryableError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (_RateLimitError, _RetryableError):
        raise
    except Exception as e:
        raise _RetryableError(str(e))


def _call_gptoss_tools(system: str, messages: list, tools: list, temperature: float) -> dict:
    import requests

    cfg = _gptoss_config
    if not cfg["api_key"]:
        raise _RetryableError("gpt-oss api_key가 설정되지 않았습니다")

    headers = {
        "Content-Type": "application/json",
        "x-dep-ticket": cfg["api_key"],
        "Send-System-Name": "Tracer",
        "User-Id": cfg["knox_id"],
        "User-Type": cfg["ad_id"],
    }

    full_messages = [{"role": "system", "content": system}] + messages

    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": full_messages,
        "tools": _to_openai_tools(tools),
        "temperature": temperature,
        "max_tokens": 16384,
    }

    try:
        resp = requests.post(
            cfg["url"],
            headers=headers,
            json=payload,
            proxies={"http": None, "https": None},
            timeout=cfg["timeout"],
        )
        if resp.status_code == 429:
            raise _RateLimitError(f"HTTP 429: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise _RetryableError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
    except (_RateLimitError, _RetryableError):
        raise
    except Exception as e:
        raise _RetryableError(str(e))

    msg = resp.json()["choices"][0]["message"]
    return _build_openai_tool_response(msg, msg.get("tool_calls") or [])
