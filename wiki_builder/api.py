"""
api.py — LLM API 추상화 (Claude / Gemini / gpt-oss / Ollama 전환)

환경변수 WIKI_BACKEND="gemini" | "claude" | "gptoss" | "ollama"

call_simple(system, user, **kwargs) -> str
    실패 시 "[LLM 호출 실패] ..." 형태로 반환. 예외 raise 금지.

컨텍스트 제한 (백엔드별 자동 조정):
    대용량 백엔드 (claude/gemini/gptoss) — 128K tokens 기준:
      MAX_CONTEXT_CHARS  = 300_000  (전체 입력 char 상한)
      MAX_CONTENT_CHARS  =  90_000  (스펙 내용 등 가변 블록 상한)
      MAX_CHUNK_CHARS    =  50_000  (청크 1개 상한, chunk_text.py와 동기)
    ollama — 16K tokens 기준 (OLLAMA_CONTEXT 환경변수로 조정 가능):
      MAX_CONTEXT_CHARS  =  30_000  (~13K tokens 여유)
      MAX_CONTENT_CHARS  =  12_000  (가변 블록 1개 상한)
      MAX_CHUNK_CHARS    =   8_000  (청크 1개 상한)
    호출자가 이 상수를 import해서 truncate 용도로 사용.
"""

import os
import time
import json
import logging

logger = logging.getLogger(__name__)

BACKEND = os.getenv("WIKI_BACKEND", "claude")

# ──────────────────────────────────────────────
# 컨텍스트 크기 상수 (백엔드별 자동 조정)
# ollama: 16K tokens × 2.3 chars/token ≈ 37K chars → 보수적 30K
# 기타:   128K tokens × 2.3 chars/token ≈ 295K chars → 보수적 300K
# ──────────────────────────────────────────────

if BACKEND == "ollama":
    _ollama_ctx_tokens = int(os.getenv("OLLAMA_CONTEXT", "16384"))
    MAX_CONTEXT_CHARS = int(_ollama_ctx_tokens * 2.3 * 0.85)  # ~32K (16K 기준)
    MAX_CONTENT_CHARS = int(_ollama_ctx_tokens * 2.3 * 0.32)  # ~12K
    MAX_CHUNK_CHARS   = int(_ollama_ctx_tokens * 2.3 * 0.21)  # ~8K
else:
    MAX_CONTEXT_CHARS = 300_000   # LLM 1회 호출 전체 입력 char 상한
    MAX_CONTENT_CHARS =  90_000   # 스펙 내용 등 가변 블록 1개 char 상한
    MAX_CHUNK_CHARS   =  50_000   # 청크 1개 char 상한 (chunk_text.py MAX_CHUNK와 동기)


def truncate_content(text: str, max_chars: int = MAX_CONTENT_CHARS, label: str = "") -> str:
    """
    텍스트를 max_chars 이하로 자름. 잘린 경우 경고 로그.
    """
    if len(text) <= max_chars:
        return text
    logger.warning(f"컨텐츠 truncate: {len(text)} -> {max_chars}자{' [' + label + ']' if label else ''}")
    return text[:max_chars]


# ──────────────────────────────────────────────
# 공통 설정
# ──────────────────────────────────────────────

MAX_RETRIES = 3
RETRY_DELAYS = [5, 10, 20]   # exponential backoff (초)
RATE_LIMIT_WAIT = int(os.getenv("WIKI_RATE_LIMIT_WAIT", "65"))  # 429 시 대기 (초)


# ──────────────────────────────────────────────
# gpt-oss 설정 (런타임 주입)
# ──────────────────────────────────────────────

_gptoss_config: dict = {
    "url": "http://apigw-stg.samsungds.net:8000/gpt-oss/1/gpt-oss-120b/v1/chat/completions",
    "api_key": os.getenv("GPTOSS_API_KEY", ""),
    "knox_id": os.getenv("GPTOSS_KNOX_ID", ""),
    "ad_id": os.getenv("GPTOSS_AD_ID", ""),
    "timeout": 300,
}

_gemini_config: dict = {
    "api_key": os.getenv("GEMINI_API_KEY", ""),
    "model": os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview"),  # 무료 RPD 500
    "base_url": "https://generativelanguage.googleapis.com/v1beta/models",
    "timeout": 120,
}

_ollama_config: dict = {
    "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    "model": os.getenv("OLLAMA_MODEL", "gemma4:26b"),
    "context_window": int(os.getenv("OLLAMA_CONTEXT", "16384")),
    "timeout": int(os.getenv("OLLAMA_TIMEOUT", "600")),
}


def configure_gptoss(api_key: str = "", knox_id: str = "", ad_id: str = "") -> None:
    """CLI 인자로 gpt-oss 인증 정보를 주입."""
    if api_key:
        _gptoss_config["api_key"] = api_key
    if knox_id:
        _gptoss_config["knox_id"] = knox_id
    if ad_id:
        _gptoss_config["ad_id"] = ad_id


def configure_gemini(api_key: str = "", model: str = "") -> None:
    """CLI 인자로 Gemini 인증 정보를 주입."""
    if api_key:
        _gemini_config["api_key"] = api_key
    if model:
        _gemini_config["model"] = model


def configure_ollama(base_url: str = "", model: str = "", context_window: int = 0) -> None:
    """CLI 인자로 Ollama 설정을 주입. context_window 변경 시 MAX_*_CHARS 상수도 재계산."""
    global MAX_CONTEXT_CHARS, MAX_CONTENT_CHARS, MAX_CHUNK_CHARS, BACKEND
    if base_url:
        _ollama_config["base_url"] = base_url
    if model:
        _ollama_config["model"] = model
    if context_window:
        _ollama_config["context_window"] = context_window
    # ollama 백엔드로 전환 시 컨텍스트 크기 상수 재계산
    BACKEND = "ollama"
    ctx = _ollama_config["context_window"]
    MAX_CONTEXT_CHARS = int(ctx * 2.3 * 0.85)
    MAX_CONTENT_CHARS = int(ctx * 2.3 * 0.32)
    MAX_CHUNK_CHARS   = int(ctx * 2.3 * 0.21)
    logger.info(f"Ollama 설정: context={ctx} → MAX_CHUNK={MAX_CHUNK_CHARS}, MAX_CONTENT={MAX_CONTENT_CHARS}")


# ──────────────────────────────────────────────
# 공개 인터페이스
# ──────────────────────────────────────────────

def call_with_tools(
    system: str,
    messages: list,
    tools: list,
    temperature: float = 0.1,
    backend: str = None,
) -> dict:
    """
    Tool use 지원 LLM 호출 (orchestrator 전용).
    messages는 누적 가능 — orchestrator만 사용할 것.

    Returns:
        {
            "text": str,
            "tool_calls": [{"id": str, "name": str, "input": dict}],
            "stop_reason": "end_turn" | "tool_use",
            "raw": ...  # messages 배열에 append할 원본 content
        }
    실패 시: {"text": "[LLM 호출 실패] ...", "tool_calls": [], "stop_reason": "error", "raw": None}
    """
    _backend = backend or BACKEND
    if _backend not in ("claude", "gemini", "gptoss", "ollama"):
        return {"text": f"[LLM 호출 실패] 알 수 없는 백엔드: {_backend}", "tool_calls": [],
                "stop_reason": "error", "raw": None}

    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            if _backend == "claude":
                return _call_claude_tools(system, messages, tools, temperature)
            elif _backend == "gemini":
                return _call_gemini_tools(system, messages, tools, temperature)
            elif _backend == "ollama":
                return _call_ollama_tools(system, messages, tools, temperature)
            else:
                return _call_gptoss_tools(system, messages, tools, temperature)
        except _RateLimitError:
            logger.warning(f"Rate limit — {RATE_LIMIT_WAIT}초 대기")
            time.sleep(RATE_LIMIT_WAIT)
            # attempt 증가 없음 — rate limit은 재시도 횟수 카운트 안 함
        except _RetryableError as e:
            attempt += 1
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAYS[attempt - 1])
            else:
                return {"text": f"[LLM 호출 실패] {e}", "tool_calls": [],
                        "stop_reason": "error", "raw": None}
        except Exception as e:
            logger.error(f"call_with_tools 오류: {e}")
            return {"text": f"[LLM 호출 실패] {e}", "tool_calls": [],
                    "stop_reason": "error", "raw": None}
    return {"text": "[LLM 호출 실패] 최대 재시도 초과", "tool_calls": [],
            "stop_reason": "error", "raw": None}


def call_simple(system: str, user: str, temperature: float = 0.3, **kwargs) -> str:
    """
    LLM에 단일 호출. stateless — messages 배열 누적 없음.

    입력 총량이 MAX_CONTEXT_CHARS를 초과하면 user 메시지를 자동 truncate.

    Returns:
        LLM 응답 문자열. 실패 시 "[LLM 호출 실패] {이유}" 반환.
    """
    backend = kwargs.pop("backend", BACKEND)

    # 전체 입력 크기 가드
    total_chars = len(system) + len(user)
    if total_chars > MAX_CONTEXT_CHARS:
        allowed_user_chars = MAX_CONTEXT_CHARS - len(system)
        trimmed = len(user) - max(0, allowed_user_chars)
        user = user[:max(0, allowed_user_chars)]
        logger.warning(f"입력 총량 초과 — user 메시지 {trimmed}자 truncate (backend={backend})")

    if backend not in ("claude", "gemini", "gptoss", "ollama"):
        return f"[LLM 호출 실패] 알 수 없는 백엔드: {backend}"

    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            if backend == "claude":
                return _call_claude(system, user, temperature, **kwargs)
            elif backend == "gemini":
                return _call_gemini(system, user, temperature, **kwargs)
            elif backend == "ollama":
                return _call_ollama(system, user, temperature, **kwargs)
            else:
                return _call_gptoss(system, user, temperature, **kwargs)
        except _RateLimitError:
            logger.warning(f"Rate limit — {RATE_LIMIT_WAIT}초 대기")
            time.sleep(RATE_LIMIT_WAIT)
            # attempt 증가 없음 — rate limit은 재시도 횟수 카운트 안 함
        except _RetryableError as e:
            attempt += 1
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAYS[attempt - 1]
                logger.warning(f"재시도 — {delay}초 후 (시도 {attempt}/{MAX_RETRIES}): {e}")
                time.sleep(delay)
            else:
                logger.error(f"최대 재시도 초과: {e}")
                return f"[LLM 호출 실패] {e}"
        except Exception as e:
            logger.error(f"예상치 못한 오류: {e}")
            return f"[LLM 호출 실패] {e}"
    return f"[LLM 호출 실패] 최대 재시도 초과"


# ──────────────────────────────────────────────
# 내부 예외
# ──────────────────────────────────────────────

class _RateLimitError(Exception):
    pass

class _RetryableError(Exception):
    pass


# ──────────────────────────────────────────────
# Tool use 내부 구현
# ──────────────────────────────────────────────

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
            max_tokens=4096,
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
        "stop_reason": response.stop_reason,  # "end_turn" | "tool_use"
        "raw": raw_content,  # {"role": "assistant", "content": raw_content} 로 append
    }


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


def _call_gptoss_tools(system: str, messages: list, tools: list, temperature: float) -> dict:
    import requests
    import json as _json

    cfg = _gptoss_config
    if not cfg["api_key"]:
        raise _RetryableError("gpt-oss api_key가 설정되지 않았습니다")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    }
    if cfg["knox_id"]:
        headers["X-Knox-ID"] = cfg["knox_id"]
    if cfg["ad_id"]:
        headers["X-AD-ID"] = cfg["ad_id"]

    openai_tools = [
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

    # gpt-oss: system을 messages 앞에 prepend
    full_messages = [{"role": "system", "content": system}] + messages

    payload = {
        "model": "gpt-oss-120b",
        "messages": full_messages,
        "tools": openai_tools,
        "temperature": temperature,
        "max_tokens": 4096,
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

    data = resp.json()
    choice = data["choices"][0]
    msg = choice["message"]

    text = msg.get("content") or ""
    raw_tool_calls = msg.get("tool_calls") or []

    tool_calls = []
    for tc in raw_tool_calls:
        fn = tc.get("function", {})
        try:
            input_dict = _json.loads(fn.get("arguments", "{}"))
        except Exception:
            input_dict = {}
        tool_calls.append({"id": tc["id"], "name": fn["name"], "input": input_dict})

    stop_reason = "tool_use" if tool_calls else "end_turn"

    raw = {
        "role": "assistant",
        "content": msg.get("content"),
        "tool_calls": raw_tool_calls if raw_tool_calls else None,
    }

    return {
        "text": text,
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "raw": raw,
    }


# ──────────────────────────────────────────────
# Claude 백엔드
# ──────────────────────────────────────────────

def _call_claude(system: str, user: str, temperature: float, **kwargs) -> str:
    try:
        import anthropic
    except ImportError:
        return "[LLM 호출 실패] anthropic 패키지가 설치되어 있지 않습니다"

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "[LLM 호출 실패] ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다"

    model = kwargs.get("model", "claude-sonnet-4-5")
    max_tokens = kwargs.get("max_tokens", 4096)

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


# ──────────────────────────────────────────────
# Gemini 백엔드 (REST, requests 사용 — 추가 패키지 불필요)
# ──────────────────────────────────────────────

def _call_gemini(system: str, user: str, temperature: float, **kwargs) -> str:
    import requests

    api_key = _gemini_config["api_key"] or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return "[LLM 호출 실패] GEMINI_API_KEY 환경변수가 설정되지 않았습니다"

    model = kwargs.get("model", _gemini_config["model"])
    max_tokens = kwargs.get("max_tokens", 4096)
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
            # safety: Gemini 기본 safety filter는 기술 문서에서 오탐 가능
            # threshold는 기본값 유지 (BLOCK_MEDIUM_AND_ABOVE)
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

        # 응답 파싱
        candidates = data.get("candidates", [])
        if not candidates:
            # safety block 등
            finish = data.get("promptFeedback", {}).get("blockReason", "UNKNOWN")
            raise _RetryableError(f"candidates 없음 (blockReason={finish})")

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            # thinking 모델이 MAX_TOKENS 초과 시 parts 없이 반환하는 경우
            finish = candidates[0].get("finishReason", "UNKNOWN")
            if finish == "MAX_TOKENS":
                raise _RetryableError("MAX_TOKENS 초과 — max_tokens 값을 늘리거나 입력을 줄이세요")
            raise _RetryableError(f"응답 parts 없음 (finishReason={finish})")

        # thoughtSignature 등 부가 필드 무시, text만 추출
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


# ──────────────────────────────────────────────
# gpt-oss 백엔드
# ──────────────────────────────────────────────

def _call_gptoss(system: str, user: str, temperature: float, **kwargs) -> str:
    import requests

    cfg = _gptoss_config
    if not cfg["api_key"]:
        return "[LLM 호출 실패] gpt-oss api_key가 설정되지 않았습니다"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    }
    if cfg["knox_id"]:
        headers["X-Knox-ID"] = cfg["knox_id"]
    if cfg["ad_id"]:
        headers["X-AD-ID"] = cfg["ad_id"]

    payload = {
        "model": "gpt-oss-120b",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": kwargs.get("max_tokens", 4096),
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


# ──────────────────────────────────────────────
# Ollama 백엔드 (OpenAI 호환 REST, 로컬 실행)
# ──────────────────────────────────────────────

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
        # Ollama(Gemma 등)는 json_format 강제 옵션 없이 프롬프트 지시로만 JSON 출력
        "options": {
            "num_ctx": cfg["context_window"],
        },
    }

    try:
        resp = requests.post(
            url,
            json=payload,
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
    except requests.exceptions.Timeout:
        raise _RetryableError("Ollama API timeout")
    except requests.exceptions.ConnectionError as e:
        raise _RetryableError(f"Ollama 연결 오류 (ollama serve 실행 중인지 확인): {e}")
    except Exception as e:
        raise _RetryableError(str(e))


def _call_ollama_tools(system: str, messages: list, tools: list, temperature: float) -> dict:
    import requests
    import json as _json

    cfg = _ollama_config
    url = f"{cfg['base_url']}/v1/chat/completions"

    openai_tools = [
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

    full_messages = [{"role": "system", "content": system}] + messages

    payload = {
        "model": cfg["model"],
        "messages": full_messages,
        "tools": openai_tools,
        "temperature": temperature,
        "max_tokens": 2048,
        "stream": False,
        "options": {
            "num_ctx": cfg["context_window"],
        },
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            timeout=cfg["timeout"],
        )
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

    data = resp.json()
    choice = data["choices"][0]
    msg = choice["message"]

    text = msg.get("content") or ""
    raw_tool_calls = msg.get("tool_calls") or []

    tool_calls = []
    for tc in raw_tool_calls:
        fn = tc.get("function", {})
        try:
            input_dict = _json.loads(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})
        except Exception:
            input_dict = {}
        tool_calls.append({"id": tc.get("id", f"ollama_{fn.get('name','')}"), "name": fn.get("name", ""), "input": input_dict})

    stop_reason = "tool_use" if tool_calls else "end_turn"

    raw = {
        "role": "assistant",
        "content": msg.get("content"),
        "tool_calls": raw_tool_calls if raw_tool_calls else None,
    }

    return {
        "text": text,
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "raw": raw,
    }


# ──────────────────────────────────────────────
# 단독 테스트
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    backend = os.getenv("WIKI_BACKEND", "gemini")
    print(f"백엔드: {backend}")
    print(f"MAX_CONTEXT_CHARS: {MAX_CONTEXT_CHARS:,}")
    print(f"MAX_CONTENT_CHARS: {MAX_CONTENT_CHARS:,}")
    print(f"MAX_CHUNK_CHARS:   {MAX_CHUNK_CHARS:,}")

    result = call_simple(
        system="당신은 5G NR PHY 전문가입니다. 한국어로 간결하게 답하세요.",
        user="PUSCH Scrambling이란 무엇인지 한 문장으로 설명하세요.",
        temperature=0.1,
        backend=backend,
    )
    print(f"\n응답:\n{result}")

    if result.startswith("[LLM 호출 실패]"):
        sys.exit(1)
