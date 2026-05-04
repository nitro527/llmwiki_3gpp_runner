"""
generate.py — Phase 2: Generator
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 무료 티어 rate limit 대응: 요청 간 최소 간격 (초)
# gemini-2.5-flash-lite: 10 RPM → 6초/요청이면 안전
# workers=1 + delay=6 → 최대 10 RPM 유지
REQUEST_INTERVAL = float(os.getenv("WIKI_REQUEST_INTERVAL", "6"))

logger = logging.getLogger(__name__)


def run_generate(
    plan: dict,
    wiki_dir: str,
    plan_path: str,
    call_llm,
    extract_spec_fn,
    check_quality_fn,
    *,
    backend: str = "claude",
    max_workers: int = 3,
    feature_list: list | None = None,
) -> list[dict]:
    """
    Phase 2 실행.

    Returns:
        failed_pages: 품질 불합격 페이지 목록
    """
    from wiki_builder.prompts import GENERATOR_SYSTEM, GENERATOR_USER

    pages = plan.get("pages", [])
    todo = [p for p in pages if not p.get("generated", False) and not p["path"].startswith("features/")]
    logger.info(f"Generate 대상: {len(todo)}개 (전체 {len(pages)}개)")

    # 전체 wiki path 목록 (링크 참조용)
    wiki_page_list = "\n".join(p["path"] for p in pages)

    failed: list[dict] = []

    if max_workers == 1:
        # 순차 처리 — rate limit 안전 모드
        for i, page in enumerate(todo):
            if i > 0:
                time.sleep(REQUEST_INTERVAL)
            try:
                result = _generate_page(
                    page=page,
                    wiki_dir=wiki_dir,
                    wiki_page_list=wiki_page_list,
                    call_llm=call_llm,
                    extract_spec_fn=extract_spec_fn,
                    check_quality_fn=check_quality_fn,
                    backend=backend,
                    feature_list=feature_list,
                )
                if result.get("failed"):
                    page["failed_reason"] = result.get("reason", "unknown")
                    failed.append(result)
                    _save_plan(plan, plan_path)
                else:
                    page["generated"] = True
                    page.pop("failed_reason", None)
                    _save_plan(plan, plan_path)
            except Exception as e:
                logger.error(f"페이지 생성 예외 ({page['path']}): {e}")
                page["failed_reason"] = str(e)
                failed.append({"path": page["path"], "error": str(e)})
                _save_plan(plan, plan_path)
    else:
        # 병렬 처리
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_page = {
                executor.submit(
                    _generate_page,
                    page=page,
                    wiki_dir=wiki_dir,
                    wiki_page_list=wiki_page_list,
                    call_llm=call_llm,
                    extract_spec_fn=extract_spec_fn,
                    check_quality_fn=check_quality_fn,
                    backend=backend,
                    feature_list=feature_list,
                ): page
                for page in todo
            }
            for future in as_completed(future_to_page):
                page = future_to_page[future]
                try:
                    result = future.result()
                    if result.get("failed"):
                        page["failed_reason"] = result.get("reason", "unknown")
                        failed.append(result)
                        _save_plan(plan, plan_path)
                    else:
                        page["generated"] = True
                        page.pop("failed_reason", None)
                        _save_plan(plan, plan_path)
                except Exception as e:
                    logger.error(f"페이지 생성 예외 ({page['path']}): {e}")
                    page["failed_reason"] = str(e)
                    failed.append({"path": page["path"], "error": str(e)})
                    _save_plan(plan, plan_path)

    return failed


def _generate_page(
    page: dict,
    wiki_dir: str,
    wiki_page_list: str,
    call_llm,
    extract_spec_fn,
    check_quality_fn,
    backend: str,
    feature_list: list | None = None,
) -> dict:
    """단일 페이지 생성 (LLM 독립 호출)."""
    from wiki_builder.prompts import GENERATOR_SYSTEM, GENERATOR_USER

    path = page["path"]
    logger.info(f"  생성 중: {path}")

    from wiki_builder.api import MAX_CONTENT_CHARS, truncate_content
    from wiki_builder.parse_38822 import (
        find_relevant_features, format_feature_hint, _keywords_from_text
    )

    spec_content = extract_spec_fn(page)
    spec_content = truncate_content(spec_content, MAX_CONTENT_CHARS, label=path)

    # feature hint: page path + description 키워드 기반
    if feature_list:
        keywords = _keywords_from_text(path + " " + page.get("description", ""))
        relevant = find_relevant_features(feature_list, keywords, top_n=12)
        feature_hint = format_feature_hint(relevant)
    else:
        feature_hint = "(feature_priority.json 없음 — 38.822 파싱 필요)"

    user_msg = GENERATOR_USER.format(
        page_path=path,
        page_description=page.get("description", ""),
        feature_hint=feature_hint,
        spec_content=spec_content,
        wiki_page_list=wiki_page_list,
    )

    content = None
    for attempt in range(3):
        raw = call_llm(
            GENERATOR_SYSTEM,
            user_msg,
            temperature=0.3,
            backend=backend,
        )

        if raw.startswith("[LLM 호출 실패]"):
            logger.error(f"Generator LLM 실패 ({path}): {raw}")
            return {"path": path, "failed": True, "reason": raw}

        # hallucination 감지
        if _detect_hallucination(raw):
            logger.warning(f"Hallucination 감지 ({path}) — 재시도 {attempt + 1}/3")
            if attempt < 2:
                continue
            else:
                logger.error(f"Hallucination 3회 감지 — 스킵: {path}")
                return {"path": path, "failed": True, "reason": "hallucination"}

        content = raw
        break

    if content is None:
        return {"path": path, "failed": True, "reason": "생성 실패"}

    # 품질 체크
    result = check_quality_fn(content, spec_content, call_llm, backend=backend)
    if not result.get("pass", False):
        logger.warning(f"품질 불합격 ({path}) score={result.get('score')}: {result.get('issues')}")
        return {
            "path": path,
            "failed": True,
            "reason": "quality_fail",
            "score": result.get("score"),
            "issues": result.get("issues"),
            "content": content,
        }

    # 파일 저장
    out_path = Path(wiki_dir) / path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info(f"  저장 완료: {path} (score={result.get('score')})")
    return {"path": path, "failed": False}


def _detect_hallucination(text: str) -> bool:
    """
    3어절 이상 동일 구절이 5회 이상 반복 → hallucination.

    단, 섹션 헤더(## ...) 및 wiki 경로(entities/, concepts/)는 검사 제외.
    """
    # 헤더, wikilink, 파일 경로 라인 제거 후 본문만 검사
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):           # 마크다운 헤더
            continue
        if stripped.startswith("- [["):       # 관련 개념 링크 목록
            continue
        if "/" in stripped and stripped.endswith(".md"):  # 경로 목록
            continue
        lines.append(stripped)

    body = " ".join(lines)
    words = body.split()
    if len(words) < 15:
        return False

    for n in range(3, 8):  # 3~7어절 n-gram
        seen: dict[tuple, int] = {}
        for i in range(len(words) - n + 1):
            gram = tuple(words[i:i + n])
            seen[gram] = seen.get(gram, 0) + 1
            if seen[gram] >= 5:
                logger.debug(f"Hallucination n-gram 감지: {' '.join(gram)!r} x{seen[gram]}")
                return True
    return False


def _save_plan(plan: dict, plan_path: str) -> None:
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
