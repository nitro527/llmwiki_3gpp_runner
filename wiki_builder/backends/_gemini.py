"""Gemini 백엔드 (REST, requests 사용)."""

import os

from wiki_builder.backends._base import _RateLimitError, _RetryableError

_gemini_config: dict = {
    "api_key": os.getenv("GEMINI_API_KEY", ""),
    "model": os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview"),
    "base_url": "https://generativelanguage.googleapis.com/v1beta/models",
    "timeout": 120,
}


def _schema_to_gemini(schema: dict) -> dict:
    """JSON Schema 타입명을 Gemini REST API 형식(대문자)으로 변환."""
    result = {}
    t = schema.get("type", "")
    if t:
        result["type"] = t.upper()
    if "properties" in schema:
        result["properties"] = {
            k: _schema_to_gemini(v) for k, v in schema["properties"].items()
        }
    if "items" in schema:
        result["items"] = _schema_to_gemini(schema["items"])
    if "description" in schema:
        result["description"] = schema["description"]
    if "required" in schema:
        result["required"] = schema["required"]
    return result


def _call_gemini(system: str, user: str, temperature: float, **kwargs) -> str:
    import requests

    api_key = _gemini_config["api_key"] or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return "[LLM 호출 실패] GEMINI_API_KEY 환경변수가 설정되지 않았습니다"

    model = kwargs.get("model", _gemini_config["model"])
    max_tokens = kwargs.get("max_tokens", 16384)
    url = f"{_gemini_config['base_url']}/{model}:generateContent"

    payload = {
        "system_instruction": {
            "parts": [{"text": system}]
        },
        "contents": [
            {"role": "user", "parts": [{"text": user}]}
        ],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }

    try:
        resp = requests.post(
            url,
            params={"key": api_key},
            json=payload,
            timeout=_gemini_config["timeout"],
        )

        if resp.status_code == 429:
            raise _RateLimitError(f"HTTP 429: {resp.text[:300]}")
        if resp.status_code >= 500:
            raise _RetryableError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()

        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            finish = data.get("promptFeedback", {}).get("blockReason", "UNKNOWN")
            raise _RetryableError(f"candidates 없음 (blockReason={finish})")

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            finish = candidates[0].get("finishReason", "UNKNOWN")
            if finish == "MAX_TOKENS":
                raise _RetryableError("MAX_TOKENS 초과 — max_tokens 값을 늘리거나 입력을 줄이세요")
            raise _RetryableError(f"응답 parts 없음 (finishReason={finish})")

        texts = [p.get("text", "") for p in parts if "text" in p]
        return "".join(texts)

    except (_RateLimitError, _RetryableError):
        raise
    except requests.exceptions.Timeout:
        raise _RetryableError("Gemini API timeout")
    except requests.exceptions.ConnectionError as e:
        raise _RetryableError(f"연결 오류: {e}")
    except Exception as e:
        raise _RetryableError(str(e))


def _call_gemini_tools(system: str, messages: list, tools: list, temperature: float) -> dict:
    import requests

    api_key = _gemini_config["api_key"] or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise _RetryableError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다")

    model = _gemini_config["model"]
    url = f"{_gemini_config['base_url']}/{model}:generateContent"

    gemini_tools = [{
        "function_declarations": [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": _schema_to_gemini(t["input_schema"]),
            }
            for t in tools
        ]
    }]

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": messages,
        "tools": gemini_tools,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 4096,
        },
    }

    try:
        resp = requests.post(
            url,
            params={"key": api_key},
            json=payload,
            timeout=_gemini_config["timeout"],
        )
        if resp.status_code == 429:
            raise _RateLimitError(f"HTTP 429: {resp.text[:300]}")
        if resp.status_code >= 500:
            raise _RetryableError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    except (_RateLimitError, _RetryableError):
        raise
    except requests.exceptions.Timeout:
        raise _RetryableError("Gemini API timeout")
    except requests.exceptions.ConnectionError as e:
        raise _RetryableError(f"연결 오류: {e}")
    except Exception as e:
        raise _RetryableError(str(e))

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        block = data.get("promptFeedback", {}).get("blockReason", "UNKNOWN")
        raise _RetryableError(f"candidates 없음 (blockReason={block})")

    candidate = candidates[0]
    parts = candidate.get("content", {}).get("parts", [])

    text_parts = [p["text"] for p in parts if "text" in p]
    text = "".join(text_parts)

    tool_calls = []
    for i, p in enumerate(parts):
        if "functionCall" in p:
            fc = p["functionCall"]
            fake_id = f"gemini_{fc['name']}_{i}"
            tool_calls.append({"id": fake_id, "name": fc["name"], "input": fc.get("args", {})})

    stop_reason = "tool_use" if tool_calls else "end_turn"

    return {
        "text": text,
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "raw": {"role": "model", "parts": parts},
    }
