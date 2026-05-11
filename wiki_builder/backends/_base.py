"""공통 예외 + OpenAI 호환 tool 헬퍼."""

import json as _json


class _RateLimitError(Exception):
    pass


class _RetryableError(Exception):
    pass


def _parse_openai_tool_calls(raw_tool_calls: list) -> list[dict]:
    """OpenAI 호환 tool_calls 배열을 내부 형식으로 변환."""
    tool_calls = []
    for tc in raw_tool_calls:
        fn = tc.get("function", {})
        args = fn.get("arguments", "{}")
        try:
            input_dict = _json.loads(args) if isinstance(args, str) else (args or {})
        except Exception:
            input_dict = {}
        tool_calls.append({
            "id": tc.get("id", f"tool_{fn.get('name', '')}"),
            "name": fn.get("name", ""),
            "input": input_dict,
        })
    return tool_calls


def _build_openai_tool_response(msg: dict, raw_tool_calls: list) -> dict:
    """OpenAI 호환 응답 메시지에서 내부 tool response dict 구성."""
    tool_calls = _parse_openai_tool_calls(raw_tool_calls)
    return {
        "text": msg.get("content") or "",
        "tool_calls": tool_calls,
        "stop_reason": "tool_use" if tool_calls else "end_turn",
        "raw": {
            "role": "assistant",
            "content": msg.get("content"),
            "tool_calls": raw_tool_calls if raw_tool_calls else None,
        },
    }


def _to_openai_tools(tools: list) -> list:
    """내부 tool 정의를 OpenAI function calling 형식으로 변환."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]
