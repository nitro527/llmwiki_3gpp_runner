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
import logging

from wiki_builder.backends._base import (
    _RateLimitError, _RetryableError,
    _to_openai_tools, _build_openai_tool_response, _parse_openai_tool_calls,
)
from wiki_builder.backends._claude import _call_claude, _call_claude_tools
from wiki_builder.backends._gemini import _gemini_config, _call_gemini, _call_gemini_tools, _schema_to_gemini
from wiki_builder.backends._gptoss import _gptoss_config, _call_gptoss, _call_gptoss_tools
from wiki_builder.backends._ollama import _ollama_config, _call_ollama, _call_ollama_tools

logger = logging.getLogger(__name__)

BACKEND = os.getenv("WIKI_BACKEND", "gemini")

# ──────────────────────────────────────────────
# 컨텍스트 크기 상수 (백엔드별 자동 조정)
# ollama: 16K tokens × 2.3 chars/token ≈ 37K chars → 보수적 30K
# 기타:   128K tokens × 2.3 chars/token ≈ 295K chars → 보수적 300K
# ──────────────────────────────────────────────

if BACKEND == "ollama":
    _ollama_ctx_tokens = int(os.getenv("OLLAMA_CONTEXT", "16384"))
    MAX_CONTEXT_CHARS = int(_ollama_ctx_tokens * 2.3 * 0.85)
    MAX_CONTENT_CHARS = int(_ollama_ctx_tokens * 2.3 * 0.32)
    MAX_CHUNK_CHARS   = int(_ollama_ctx_tokens * 2.3 * 0.21)
else:
    MAX_CONTEXT_CHARS = 300_000
    MAX_CONTENT_CHARS =  90_000
    MAX_CHUNK_CHARS   =  50_000


def truncate_content(text: str, max_chars: int = MAX_CONTENT_CHARS, label: str = "") -> str:
    if len(text) <= max_chars:
        return text
    logger.warning(f"컨텐츠 truncate: {len(text)} -> {max_chars}자{' [' + label + ']' if label else ''}")
    return text[:max_chars]


# ──────────────────────────────────────────────
# 공통 설정
# ──────────────────────────────────────────────

MAX_RETRIES = 3
RETRY_DELAYS = [5, 10, 20]
RATE_LIMIT_WAIT = int(os.getenv("WIKI_RATE_LIMIT_WAIT", "65"))


# ──────────────────────────────────────────────
# 런타임 설정 주입
# ──────────────────────────────────────────────

def configure_gptoss(api_key: str = "", knox_id: str = "", ad_id: str = "") -> None:
    if api_key:
        _gptoss_config["api_key"] = api_key
    if knox_id:
        _gptoss_config["knox_id"] = knox_id
    if ad_id:
        _gptoss_config["ad_id"] = ad_id


def configure_gemini(api_key: str = "", model: str = "") -> None:
    if api_key:
        _gemini_config["api_key"] = api_key
    if model:
        _gemini_config["model"] = model


def configure_ollama(base_url: str = "", model: str = "", context_window: int = 0) -> None:
    """context_window 변경 시 MAX_*_CHARS 상수도 재계산."""
    global MAX_CONTEXT_CHARS, MAX_CONTENT_CHARS, MAX_CHUNK_CHARS, BACKEND
    if base_url:
        _ollama_config["base_url"] = base_url
    if model:
        _ollama_config["model"] = model
    if context_window:
        _ollama_config["context_window"] = context_window
    BACKEND = "ollama"
    ctx = _ollama_config["context_window"]
    MAX_CONTEXT_CHARS = int(ctx * 2.3 * 0.85)
    MAX_CONTENT_CHARS = int(ctx * 2.3 * 0.32)
    MAX_CHUNK_CHARS   = int(ctx * 2.3 * 0.21)
    logger.info(f"Ollama 설정: context={ctx} → MAX_CHUNK={MAX_CHUNK_CHARS}, MAX_CONTENT={MAX_CONTENT_CHARS}")


# ──────────────────────────────────────────────
# 공개 인터페이스
# ──────────────────────────────────────────────

def _validate_backend(backend: str) -> bool:
    return backend in ("claude", "gemini", "gptoss", "ollama")


def _dispatch_simple(backend: str, system: str, user: str, temperature: float, **kwargs) -> str:
    if backend == "claude":
        return _call_claude(system, user, temperature, **kwargs)
    elif backend == "gemini":
        return _call_gemini(system, user, temperature, **kwargs)
    elif backend == "ollama":
        return _call_ollama(system, user, temperature, **kwargs)
    else:
        return _call_gptoss(system, user, temperature, **kwargs)


def _dispatch_tools(backend: str, system: str, messages: list, tools: list, temperature: float) -> dict:
    if backend == "claude":
        return _call_claude_tools(system, messages, tools, temperature)
    elif backend == "gemini":
        return _call_gemini_tools(system, messages, tools, temperature)
    elif backend == "ollama":
        return _call_ollama_tools(system, messages, tools, temperature)
    else:
        return _call_gptoss_tools(system, messages, tools, temperature)


_TOOLS_FAILURE = {"tool_calls": [], "stop_reason": "error", "raw": None}


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
    if not _validate_backend(_backend):
        return {"text": f"[LLM 호출 실패] 알 수 없는 백엔드: {_backend}", **_TOOLS_FAILURE}

    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            return _dispatch_tools(_backend, system, messages, tools, temperature)
        except _RateLimitError:
            logger.warning(f"Rate limit — {RATE_LIMIT_WAIT}초 대기")
            time.sleep(RATE_LIMIT_WAIT)
        except _RetryableError as e:
            attempt += 1
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAYS[attempt - 1])
            else:
                return {"text": f"[LLM 호출 실패] {e}", **_TOOLS_FAILURE}
        except Exception as e:
            logger.error(f"call_with_tools 오류: {e}")
            return {"text": f"[LLM 호출 실패] {e}", **_TOOLS_FAILURE}
    return {"text": "[LLM 호출 실패] 최대 재시도 초과", **_TOOLS_FAILURE}


def call_simple(system: str, user: str, temperature: float = 0.3, **kwargs) -> str:
    """
    LLM에 단일 호출. stateless — messages 배열 누적 없음.

    입력 총량이 MAX_CONTEXT_CHARS를 초과하면 user 메시지를 자동 truncate.

    Returns:
        LLM 응답 문자열. 실패 시 "[LLM 호출 실패] {이유}" 반환.
    """
    backend = kwargs.pop("backend", BACKEND)

    total_chars = len(system) + len(user)
    if total_chars > MAX_CONTEXT_CHARS:
        allowed_user_chars = MAX_CONTEXT_CHARS - len(system)
        trimmed = len(user) - max(0, allowed_user_chars)
        user = user[:max(0, allowed_user_chars)]
        logger.warning(f"입력 총량 초과 — user 메시지 {trimmed}자 truncate (backend={backend})")

    if not _validate_backend(backend):
        return f"[LLM 호출 실패] 알 수 없는 백엔드: {backend}"

    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            return _dispatch_simple(backend, system, user, temperature, **kwargs)
        except _RateLimitError:
            logger.warning(f"Rate limit — {RATE_LIMIT_WAIT}초 대기")
            time.sleep(RATE_LIMIT_WAIT)
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
# 단독 테스트
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
