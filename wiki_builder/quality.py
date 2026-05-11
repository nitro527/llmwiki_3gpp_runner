"""
quality.py — wiki 페이지 품질 검사 (LLM + 구조 기반)

check_quality(content, spec_content, call_llm) -> dict
    8점 만점, 7점 이상 pass.
"""

import logging
import re

import wiki_builder.api
from wiki_builder.prompt_loader import load_prompt
from wiki_builder.utils import extract_json_from_llm

logger = logging.getLogger(__name__)

PASS_SCORE = 8


def check_quality(
    content: str,
    spec_content: str,
    call_llm,
    *,
    backend: str | None = None,
    feature_hint: str = "",
) -> dict:
    """
    wiki 페이지 품질 평가.

    Returns:
        {"score": int, "pass": bool, "issues": list, "details": dict}
    """
    backend = backend or wiki_builder.api.BACKEND

    CHECKER_SYSTEM, CHECKER_USER = load_prompt("checker")

    quick = _quick_check(content)

    user_msg = CHECKER_USER.format(
        page_content=content,
        spec_content=spec_content,
        feature_hint=feature_hint,
    )

    for attempt in range(3):
        raw = call_llm(CHECKER_SYSTEM, user_msg, temperature=0.1, backend=backend, json_format=True)

        if raw.startswith("[LLM 호출 실패]"):
            logger.error(f"Checker LLM 실패: {raw}")
            return quick

        result = _parse_checker_response(raw)
        if result is not None:
            return result

        logger.warning(f"Checker 파싱 실패 (시도 {attempt + 1}/3)")

    logger.error("Checker LLM 파싱 3회 실패 — 구조 검사 결과 사용")
    return {**quick, "llm_check_failed": True}


def _quick_check(content: str) -> dict:
    """구조 기반 빠른 품질 검사 (LLM 없이)."""
    required_sections = [
        "## 정의", "## 요약", "## 상세 설명",
        "## 인과 관계", "## 관련 개념", "## 스펙 근거", "## 소스",
    ]
    missing = [s for s in required_sections if s not in content]
    structure_score = 2 if not missing else (1 if len(missing) <= 2 else 0)

    related_section = re.search(r'## 관련 개념\n(.*?)(?=\n##|\Z)', content, re.DOTALL)
    has_relation_types = False
    if related_section:
        links = re.findall(r'\[\[([^\]]+)\]\]', related_section.group(1))
        has_relation_types = all(
            re.search(r'\([^)]+\)', link) or '(' in related_section.group(1)
            for link in links
        ) if links else True

    has_bold = bool(re.search(r'\*\*[^*]+\*\*', content))

    score = structure_score
    score += 1 if not has_bold else 0
    score += 1 if has_relation_types else 0
    score += 1 if "## 스펙 근거" in content else 0
    score += 2  # hallucination: 구조로 판단 불가, 기본 2점
    score += 1 if "## 상세 설명" in content else 0

    issues = []
    if missing:
        issues.append(f"누락 섹션: {', '.join(missing)}")
    if has_bold:
        issues.append("**bold** 사용 감지 — [[wikilink]] 로 변경 필요")
    if not has_relation_types:
        issues.append("관련 개념에 관계 타입 누락")

    return {
        "score": score,
        "pass": score >= PASS_SCORE,
        "issues": issues,
        "details": {
            "structure": structure_score,
            "no_translation": 1 if not has_bold else 0,
            "relation_types": 1 if has_relation_types else 0,
            "source_reference": 1 if "## 스펙 근거" in content else 0,
            "no_hallucination": 2,
            "spec_based": 1 if "## 상세 설명" in content else 0,
        },
        "method": "quick_check",
    }


def _parse_checker_response(raw: str) -> dict | None:
    data = extract_json_from_llm(raw)
    if data is None:
        return None
    score = data.get("score", 0)
    return {
        "score": score,
        "pass": score >= PASS_SCORE,
        "issues": data.get("issues", []),
        "details": data.get("details", {}),
        "method": "llm_check",
    }
