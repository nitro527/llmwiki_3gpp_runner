"""Claude (Anthropic) 백엔드."""

import os

from wiki_builder.backends._base import _RateLimitError, _RetryableError


def _call_claude(system: str, user: str, temperature: float, **kwargs) -> str:
    try:
        import anthropic
    except ImportError:
        return "[LLM 호출 실패] anthropic 패키지가 설치되어 있지 않습니다"

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "[LLM 호출 실패] ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다"

    model = kwargs.get("model", "claude-sonnet-4-5")
    max_tokens = kwargs.get("max_tokens", 16384)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text
    except anthropic.RateLimitError as e:
        raise _RateLimitError(str(e))
    except (anthropic.APIConnectionError, anthropic.InternalServerError) as e:
        raise _RetryableError(str(e))
    except anthropic.APIError as e:
        raise _RetryableError(str(e))


def _call_claude_tools(system: str, messages: list, tools: list, temperature: float) -> dict:
    try:
        import anthropic
    except ImportError:
        raise _RetryableError("anthropic 패키지가 설치되어 있지 않습니다")

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise _RetryableError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다")

    claude_tools = [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t["input_schema"],
        }
        for t in tools
    ]

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            max_tokens=16384,
            temperature=temperature,
            system=system,
            messages=messages,
            tools=claude_tools,
        )
    except anthropic.RateLimitError as e:
        raise _RateLimitError(str(e))
    except (anthropic.APIConnectionError, anthropic.InternalServerError) as e:
        raise _RetryableError(str(e))
    except anthropic.APIError as e:
        raise _RetryableError(str(e))

    text = "".join(b.text for b in response.content if b.type == "text")

    tool_calls = []
    raw_content = []
    for b in response.content:
        if b.type == "text":
            raw_content.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            tool_calls.append({"id": b.id, "name": b.name, "input": b.input})
            raw_content.append({
                "type": "tool_use",
                "id": b.id,
                "name": b.name,
                "input": b.input,
            })

    return {
        "text": text,
        "tool_calls": tool_calls,
        "stop_reason": response.stop_reason,
        "raw": raw_content,
    }
