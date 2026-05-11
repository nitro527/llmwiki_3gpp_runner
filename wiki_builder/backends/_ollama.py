"""Ollama 백엔드 (OpenAI 호환 REST, 로컬 실행)."""

import os

from wiki_builder.backends._base import (
    _RateLimitError, _RetryableError,
    _to_openai_tools, _build_openai_tool_response,
)

_ollama_config: dict = {
    "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    "model": os.getenv("OLLAMA_MODEL", "gemma4:26b"),
    "context_window": int(os.getenv("OLLAMA_CONTEXT", "16384")),
    "timeout": int(os.getenv("OLLAMA_TIMEOUT", "600")),
}


def _call_ollama(system: str, user: str, temperature: float, **kwargs) -> str:
    import requests

    cfg = _ollama_config
    model = kwargs.get("model", cfg["model"])
    url = f"{cfg['base_url']}/v1/chat/completions"

    # thinking 모델(Gemma4 등)은 reasoning 토큰이 max_tokens에 포함됨.
    # context_window 절반을 하한선으로 설정해 reasoning 후 content 출력 여지 확보.
    requested = kwargs.get("max_tokens", 2048)
    max_tokens = max(requested, cfg["context_window"] // 2)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "options": {
            "num_ctx": cfg["context_window"],
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=cfg["timeout"])
        if resp.status_code == 429:
            raise _RateLimitError(f"HTTP 429: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise _RetryableError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (_RateLimitError, _RetryableError):
        raise
    except requests.exceptions.Timeout:
        raise _RetryableError("Ollama API timeout")
    except requests.exceptions.ConnectionError as e:
        raise _RetryableError(f"Ollama 연결 오류 (ollama serve 실행 중인지 확인): {e}")
    except Exception as e:
        raise _RetryableError(str(e))


def _call_ollama_tools(system: str, messages: list, tools: list, temperature: float) -> dict:
    import requests

    cfg = _ollama_config
    url = f"{cfg['base_url']}/v1/chat/completions"

    full_messages = [{"role": "system", "content": system}] + messages

    payload = {
        "model": cfg["model"],
        "messages": full_messages,
        "tools": _to_openai_tools(tools),
        "temperature": temperature,
        "max_tokens": 2048,
        "stream": False,
        "options": {
            "num_ctx": cfg["context_window"],
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=cfg["timeout"])
        if resp.status_code == 429:
            raise _RateLimitError(f"HTTP 429: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise _RetryableError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
    except (_RateLimitError, _RetryableError):
        raise
    except requests.exceptions.Timeout:
        raise _RetryableError("Ollama API timeout")
    except requests.exceptions.ConnectionError as e:
        raise _RetryableError(f"Ollama 연결 오류 (ollama serve 실행 중인지 확인): {e}")
    except Exception as e:
        raise _RetryableError(str(e))

    msg = resp.json()["choices"][0]["message"]
    return _build_openai_tool_response(msg, msg.get("tool_calls") or [])
