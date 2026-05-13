"""
generate.py — Phase 2: Generator
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

import wiki_builder.api
from wiki_builder.api import MAX_CONTENT_CHARS, truncate_content
from wiki_builder.prompt_loader import load_prompt
from wiki_builder.utils import save_plan

# 무료 티어 rate limit 대응: 요청 간 최소 간격 (초)
# gemini-2.5-flash-lite: 10 RPM → 6초/요청이면 안전
# workers=1 + delay=6 → 최대 10 RPM 유지
REQUEST_INTERVAL = float(os.getenv("WIKI_REQUEST_INTERVAL", "6"))

logger = logging.getLogger(__name__)


FAILURE_THRESHOLD = int(os.getenv("WIKI_FAILURE_THRESHOLD", "5"))
QUALITY_RETRY_MAX = int(os.getenv("WIKI_QUALITY_RETRY_MAX", "3"))


def run_generate(
    plan: dict,
    wiki_dir: str,
    plan_path: str,
    call_llm,
    extract_spec_fn,
    check_quality_fn,
    *,
    backend: str | None = None,
    max_workers: int = 3,
    feature_list: list | None = None,
    mid_eval_fn=None,  # (failed_pages: list) -> None, sequential 모드에서만 동작
) -> list[dict]:
    """
    Phase 2 실행.

    mid_eval_fn: 실패 FAILURE_THRESHOLD개 누적 시 호출되는 콜백.
                 evaluate + 패치 적용 담당. sequential 모드에서만 동작.

    Returns:
        failed_pages: 품질 불합격 페이지 목록
    """
    backend = backend or wiki_builder.api.BACKEND

    pages = plan.get("pages", [])
    wiki_page_list = "\n".join(p["path"] for p in pages)

    failed: list[dict] = []
    needs_eval: list[dict] = []

    while True:
        todo = [p for p in pages if not p.get("generated", False) and not p["path"].startswith("features/")]
        if not todo:
            break
        logger.info(f"Generate 대상: {len(todo)}개 (전체 {len(pages)}개)")

        mid_eval_fired = False

        if max_workers == 1:
            # 순차 처리
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
                    mid_eval_fired = _handle_page_result(
                        result, page, plan, plan_path,
                        failed, needs_eval, mid_eval_fn,
                    )
                except Exception as e:
                    logger.error(f"페이지 생성 예외 ({page['path']}): {e}")
                    page["failed_reason"] = str(e)
                    failed.append({"path": page["path"], "error": str(e)})
                    _save_plan(plan, plan_path)
                if mid_eval_fired:
                    break

            if mid_eval_fired:
                logger.info(f"실패 {len(failed)}개 누적 — mid_eval 실행 후 전체 재시작")
                mid_eval_fn(failed)
                failed = []

        else:
            # 병렬 처리
            def _submit(p):
                return executor.submit(
                    _generate_page,
                    page=p,
                    wiki_dir=wiki_dir,
                    wiki_page_list=wiki_page_list,
                    call_llm=call_llm,
                    extract_spec_fn=extract_spec_fn,
                    check_quality_fn=check_quality_fn,
                    backend=backend,
                    feature_list=feature_list,
                )

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                pending = {_submit(page): page for page in todo}

                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        page = pending.pop(future)
                        try:
                            result = future.result()
                            fired = _handle_page_result(
                                result, page, plan, plan_path,
                                failed, needs_eval, mid_eval_fn,
                            )
                            if fired:
                                # 아직 시작 안 한 future 취소 (시작된 것은 완료 대기)
                                for f in list(pending.keys()):
                                    f.cancel()
                                pending.clear()
                                mid_eval_fired = True
                                break
                        except Exception as e:
                            logger.error(f"페이지 생성 예외 ({page['path']}): {e}")
                            page["failed_reason"] = str(e)
                            failed.append({"path": page["path"], "error": str(e)})
                            _save_plan(plan, plan_path)
                    if mid_eval_fired:
                        break
                # executor 종료 시 실행 중인 스레드 완료까지 대기

            if mid_eval_fired:
                logger.info(f"실패 {len(failed)}개 누적 — mid_eval 실행 후 전체 재시작")
                mid_eval_fn(failed)
                failed = []

        if not mid_eval_fired:
            break

    if needs_eval:
        logger.info(f"LLM 검사 실패로 evaluate 대상 추가: {len(needs_eval)}개")
    return failed + needs_eval


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
    GENERATOR_SYSTEM, GENERATOR_USER = load_prompt("generator")

    path = page["path"]
    logger.info(f"  생성 중: {path}")

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

    logger.debug(
        f"  [{path}] system={len(GENERATOR_SYSTEM)}자 user={len(user_msg)}자 "
        f"(spec={len(spec_content)}자 wiki_list={len(wiki_page_list)}자)"
    )

    best_content: str | None = None
    best_score: int = -1
    best_llm_check_failed: bool = False

    for quality_attempt in range(QUALITY_RETRY_MAX):
        if quality_attempt > 0:
            logger.info(f"  품질 재시도 ({path}) {quality_attempt + 1}/{QUALITY_RETRY_MAX}")
            time.sleep(REQUEST_INTERVAL)

        # 생성 (hallucination 재시도 포함)
        content = None
        for attempt in range(3):
            raw = call_llm(
                GENERATOR_SYSTEM,
                user_msg,
                temperature=0.1,
                backend=backend,
            )

            if raw.startswith("[LLM 호출 실패]"):
                logger.error(f"Generator LLM 실패 ({path}): {raw}")
                return {"path": path, "failed": True, "reason": raw}

            # hallucination 감지 (2단계)
            suspicious_gram = _detect_hallucination(raw)
            if suspicious_gram and _verify_hallucination_with_llm(raw, suspicious_gram, call_llm, backend):
                logger.warning(f"Hallucination 감지 ({path}) — 재시도 {attempt + 1}/3: {suspicious_gram[:60]!r}")
                if attempt < 2:
                    continue
                # 3회 모두 hallucination 확인 — 복구 불가
                logger.error(f"Hallucination 3회 확인 — 스킵: {path}")
                log_hallucination(path, raw)
                return {"path": path, "failed": True, "reason": "hallucination", "content": raw}

            content = raw
            break

        if content is None:
            return {"path": path, "failed": True, "reason": "생성 실패"}

        # 품질 체크
        result = check_quality_fn(content, spec_content, call_llm, backend=backend, feature_hint=feature_hint)
        score = result.get("score", 0)

        if result.get("pass", False):
            # 합격 → 즉시 저장 후 리턴
            out_path = Path(wiki_dir) / path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"  저장 완료: {path} (score={score}, attempt={quality_attempt + 1})")
            return {"path": path, "failed": False, "llm_check_failed": result.get("llm_check_failed", False)}

        # 불합격 — 최고 점수 후보 갱신
        logger.warning(
            f"품질 불합격 ({path}) score={score} [{quality_attempt + 1}/{QUALITY_RETRY_MAX}]: "
            f"{result.get('issues')}"
        )
        if score > best_score:
            best_score = score
            best_content = content
            best_llm_check_failed = result.get("llm_check_failed", False)

    # QUALITY_RETRY_MAX 회 모두 불합격 → fail 처리
    logger.warning(
        f"  품질 기준 미달 {QUALITY_RETRY_MAX}회 — fail 처리: {path} (best_score={best_score})"
    )
    return {"path": path, "failed": True, "reason": f"품질 기준 미달 {QUALITY_RETRY_MAX}회 (best_score={best_score})"}


def _detect_hallucination(text: str) -> str | None:
    """
    5어절 이상 동일 구절이 4회 이상 반복 → 의심 구절 반환 (없으면 None).

    단, 섹션 헤더(## ...) 및 wiki 경로(entities/, concepts/)는 검사 제외.
    """
    lines = []
    in_source_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            in_source_section = stripped in ("## 소스", "## 스펙 근거")
        if in_source_section:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("- [["):
            continue
        if "/" in stripped and stripped.endswith(".md"):
            continue
        lines.append(stripped)

    body = " ".join(lines)
    words = body.split()
    if len(words) < 15:
        return None

    for n in range(5, 11):  # 5~10어절 n-gram
        seen: dict[tuple, int] = {}
        for i in range(len(words) - n + 1):
            gram = tuple(words[i:i + n])
            seen[gram] = seen.get(gram, 0) + 1
            if seen[gram] >= 4:
                gram_str = " ".join(gram)
                logger.debug(f"Hallucination 의심 n-gram: {gram_str!r} x{seen[gram]}")
                return gram_str
    return None


def _verify_hallucination_with_llm(text: str, suspicious_gram: str, call_llm, backend: str) -> bool:
    """
    n-gram 의심 구절 + 주변 컨텍스트를 LLM에게 보여주고 실제 hallucination인지 확인.
    """
    # 의심 구절 주변 컨텍스트 추출 (앞뒤 5줄)
    lines = text.splitlines()
    context_lines = []
    for i, line in enumerate(lines):
        if suspicious_gram.split()[0] in line:
            start = max(0, i - 5)
            end = min(len(lines), i + 6)
            context_lines = lines[start:end]
            break
    context = "\n".join(context_lines) if context_lines else text[:1000]

    system = "당신은 LLM 생성 텍스트의 품질을 검사하는 전문가입니다."
    user = (
        f"다음 텍스트에서 반복 구절이 감지되었습니다.\n\n"
        f"**의심 구절**: {suspicious_gram}\n\n"
        f"**주변 컨텍스트**:\n{context}\n\n"
        f"이 반복이 LLM 생성 루프 오류(같은 문장이 비정상적으로 반복)인가요, "
        f"아니면 기술 문서에서 자연스러운 반복인가요?\n"
        f"'YES'(루프 오류) 또는 'NO'(정상 반복) 중 하나만 답하세요."
    )

    result = call_llm(system, user, temperature=0.0, backend=backend)
    if result.startswith("[LLM 호출 실패]"):
        logger.warning(f"Hallucination LLM 검증 실패 — n-gram 기준으로 보수적 차단 (LLM 미확인): {suspicious_gram!r}")
        return True  # LLM 실패 시 보수적으로 hallucination 처리
    is_hallucination = result.strip().upper().startswith("YES")
    logger.debug(f"Hallucination LLM 검증: {result.strip()!r} → {'확인' if is_hallucination else '정상'}")
    return is_hallucination


def log_hallucination(path: str, content: str) -> None:
    """hallucination 발생 내역을 hallucination_log.jsonl에 기록."""
    from datetime import datetime
    log_path = Path(__file__).parent.parent / "hallucination_log.jsonl"
    entry = {
        "timestamp": datetime.now().isoformat(),
        "path": path,
        "content_preview": content[:3000],
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _save_plan(plan: dict, plan_path: str) -> None:
    save_plan(plan, plan_path)


def _handle_page_result(
    result: dict,
    page: dict,
    plan: dict,
    plan_path: str,
    failed: list,
    needs_eval: list,
    mid_eval_fn,
) -> bool:
    """
    단일 페이지 생성 결과를 처리한다.

    - 성공 시: page["generated"] = True, plan 저장
    - 실패 시: failed 리스트에 추가, plan 저장, mid_eval 임계값 도달 여부 반환

    Returns:
        True면 mid_eval이 발동해야 하므로 현재 배치를 중단할 것.
    """
    if result.get("failed"):
        page["failed_reason"] = result.get("reason", "unknown")
        failed.append(result)
        _save_plan(plan, plan_path)
        if mid_eval_fn and len(failed) >= FAILURE_THRESHOLD:
            return True  # 호출부에서 mid_eval_fn 호출 책임
    else:
        page["generated"] = True
        page.pop("failed_reason", None)
        if result.get("llm_check_failed"):
            needs_eval.append({"path": page["path"], "reason": "llm_check_failed"})
        _save_plan(plan, plan_path)
    return False
