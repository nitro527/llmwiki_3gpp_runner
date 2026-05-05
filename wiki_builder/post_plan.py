"""
post_plan.py — Phase 1.5: Plan 품질 검증

run_post_plan(plan, plan_path, call_llm, backend) -> dict
    1. 코드 검증: 동일 파일+섹션이 여러 페이지에 중복 배정된 경우 감지 (로그만)
    2. LLM 검증: 페이지 path/description과 배정 섹션의 의미적 불일치 감지 및 수정
    결과를 plan.json에 반영 (post_plan_done=True)
"""

import json
import logging
import re
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

BATCH_SIZE = 8  # LLM 호출당 검증 페이지 수


def run_post_plan(
    plan: dict,
    plan_path: str,
    call_llm,
    backend: str = "gemini",
) -> dict:
    """
    Phase 1.5 실행.

    Args:
        plan: plan.json dict
        plan_path: plan.json 저장 경로
        call_llm: call_simple 함수 참조
        backend: LLM 백엔드

    Returns:
        업데이트된 plan dict
    """
    if plan.get("post_plan_done"):
        logger.info("Post-Plan 이미 완료 — 스킵")
        return plan

    pages = plan.get("pages", [])
    logger.info(f"Post-Plan 시작: {len(pages)}개 페이지 검증")

    # Step 1: 코드 검증 — 중복 섹션 감지 (로그만)
    duplicate_issues = _check_duplicate_sections(pages)
    if duplicate_issues:
        logger.warning(f"중복 섹션 {len(duplicate_issues)}건 감지 (참고용 로그)")
    else:
        logger.info("중복 섹션 없음")

    # Step 2: LLM 검증 — 의미적 불일치 감지 및 수정
    llm_fixes = _check_semantic_mismatch(pages, call_llm, backend)

    # Step 3: 수정 적용
    if llm_fixes:
        _apply_fixes(pages, llm_fixes)

    # Step 4: 소스 0개 페이지 제거
    removed = _remove_empty_pages(pages)
    if removed:
        plan["pages"] = pages
        logger.info(f"소스 없는 페이지 {len(removed)}개 제거: {removed}")

    # Step 5: plan.json 저장
    plan["post_plan_done"] = True
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    logger.info(
        f"Post-Plan 완료 — "
        f"중복 감지: {len(duplicate_issues)}건, "
        f"LLM 수정: {len(llm_fixes)}건, "
        f"빈 페이지 제거: {len(removed)}건"
    )
    return plan


# ──────────────────────────────────────────────
# Step 1: 중복 섹션 감지
# ──────────────────────────────────────────────

def _check_duplicate_sections(pages: list) -> list:
    """동일 파일+섹션이 여러 페이지에 중복 배정된 경우 감지."""
    section_map = defaultdict(list)
    for page in pages:
        for src in page.get("sources", []):
            for sec in src.get("sections", []):
                key = (src["file"], sec)
                section_map[key].append(page["path"])

    issues = []
    for (file, sec), paths in sorted(section_map.items()):
        if len(paths) > 1:
            issues.append({"file": file, "section": sec, "pages": paths})
            logger.warning(
                f"  중복: {Path(file).name} §{sec} → "
                + ", ".join(paths)
            )

    return issues


# ──────────────────────────────────────────────
# Step 2: LLM 의미적 불일치 검증
# ──────────────────────────────────────────────

def _check_semantic_mismatch(pages: list, call_llm, backend: str) -> list:
    """LLM으로 페이지별 의미적 불일치 감지."""
    from wiki_builder.prompt_loader import load_prompt
    POST_PLAN_SYSTEM, POST_PLAN_USER = load_prompt("post_plan")

    all_fixes = []
    total_batches = (len(pages) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx in range(0, len(pages), BATCH_SIZE):
        batch = pages[batch_idx:batch_idx + BATCH_SIZE]
        batch_num = batch_idx // BATCH_SIZE + 1
        logger.info(f"  LLM 검증 배치 {batch_num}/{total_batches} ({len(batch)}개 페이지)")

        pages_text = _format_batch(batch)
        user_msg = POST_PLAN_USER.format(pages_text=pages_text)

        fixes = None
        for attempt in range(3):
            raw = call_llm(
                POST_PLAN_SYSTEM,
                user_msg,
                temperature=0.1,
                backend=backend,
                json_format=True,
            )
            if raw.startswith("[LLM 호출 실패]"):
                logger.warning(f"    LLM 실패 (배치 {batch_num}): {raw}")
                break

            fixes = _parse_fixes(raw)
            if fixes is not None:
                break
            logger.warning(f"    파싱 실패 (시도 {attempt + 1}/3)")

        if fixes:
            logger.info(f"    배치 {batch_num}: {len(fixes)}건 수정 감지")
            all_fixes.extend(fixes)

    return all_fixes


def _format_batch(batch: list) -> str:
    """배치 페이지 목록을 LLM용 텍스트로 변환."""
    lines = []
    for page in batch:
        src_lines = []
        for src in page.get("sources", []):
            secs = ", ".join(src["sections"][:15])  # 섹션 최대 15개
            src_lines.append(f"  - {Path(src['file']).name}: §{secs}")
        lines.append(
            f"path: {page['path']}\n"
            f"description: {page['description']}\n"
            f"sources:\n" + "\n".join(src_lines)
        )
    return "\n\n".join(lines)


def _parse_fixes(raw: str) -> list | None:
    """LLM 응답에서 수정 목록 파싱."""
    text = re.sub(r'```json\s*', '', raw)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()

    m = re.search(r'\[.*\]', text, re.DOTALL)
    if not m:
        return None

    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return None

    if not isinstance(data, list):
        return None

    return data


# ──────────────────────────────────────────────
# Step 4: 소스 없는 페이지 제거
# ──────────────────────────────────────────────

def _remove_empty_pages(pages: list) -> list:
    """소스가 하나도 없는 페이지를 제거하고 제거된 path 목록 반환."""
    to_remove = [
        p["path"] for p in pages
        if not p.get("sources") or all(len(s.get("sections", [])) == 0 for s in p["sources"])
    ]
    if to_remove:
        pages[:] = [p for p in pages if p["path"] not in to_remove]
    return to_remove


# ──────────────────────────────────────────────
# Step 3: 수정 적용
# ──────────────────────────────────────────────

def _apply_fixes(pages: list, fixes: list):
    """수정 목록을 pages에 적용."""
    page_index = {p["path"]: i for i, p in enumerate(pages)}

    for fix in fixes:
        path = fix.get("path", "")
        action = fix.get("action", "")
        file = fix.get("file", "")

        if path not in page_index:
            logger.warning(f"  수정 대상 없음: {path}")
            continue

        idx = page_index[path]
        page = pages[idx]

        if action == "remove_source":
            before = len(page["sources"])
            page["sources"] = [s for s in page["sources"] if s["file"] != file]
            after = len(page["sources"])
            logger.info(
                f"  [수정] remove_source: {path} ← "
                f"{Path(file).name} 제거 ({before}→{after}개 소스)"
            )

        elif action == "remove_sections":
            sections_to_remove = set(fix.get("sections_to_remove", []))
            for src in page["sources"]:
                if src["file"] == file:
                    before = src["sections"][:]
                    src["sections"] = [
                        s for s in src["sections"] if s not in sections_to_remove
                    ]
                    logger.info(
                        f"  [수정] remove_sections: {path} ← "
                        f"{Path(file).name} {before} → {src['sections']}"
                    )
                    # 섹션이 모두 제거된 소스 항목 정리
                    if not src["sections"]:
                        page["sources"] = [s for s in page["sources"] if s["file"] != file]
                        logger.info(f"    섹션 없음 — 소스 항목 전체 제거: {Path(file).name}")

        elif action == "ok":
            pass

        else:
            logger.warning(f"  알 수 없는 action '{action}': {path}")
