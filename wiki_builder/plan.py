"""
plan.py — Phase 1: Planner

run_plan(sources_dir, wiki_dir, plan_path, call_llm, chunk_fn) -> dict
    소스 파일 청크별 LLM 호출 → wiki 페이지 계획 → plan.json 저장
    plan.json 존재 시: 새 소스만 증분 플래닝, 기존 pages/진행상황 유지
"""

import os
import json
import re
import time
import logging
from pathlib import Path

import wiki_builder.api
from wiki_builder.api import MAX_CHUNK_CHARS, truncate_content
from wiki_builder.prompt_loader import load_prompt
from wiki_builder.utils import save_plan

logger = logging.getLogger(__name__)


def run_plan(
    sources_dir: str,
    wiki_dir: str,
    plan_path: str,
    call_llm,
    chunk_fn,
    *,
    backend: str | None = None,
) -> dict:
    """
    Phase 1 실행.

    Args:
        sources_dir: 소스 파일 루트 (sources/)
        wiki_dir: wiki 출력 디렉토리
        plan_path: plan.json 경로
        call_llm: call_simple 함수 참조
        chunk_fn: chunk_file 함수 참조
        backend: LLM 백엔드. None이면 WIKI_BACKEND 환경변수 사용.

    Returns:
        plan dict {"planned_sources": [...], "pages": [...]}
    """
    backend = backend or wiki_builder.api.BACKEND

    # 소스 파일 수집 (.docx, .txt)
    source_files = _collect_sources(sources_dir)
    if not source_files:
        logger.warning(f"소스 파일 없음: {sources_dir}")
        return _empty_plan()

    # 기존 plan.json 로드 (있으면)
    if os.path.exists(plan_path):
        with open(plan_path, "r", encoding="utf-8") as f:
            existing_plan = json.load(f)
        already_planned = set(existing_plan.get("planned_sources", []))
        new_sources = [p for p in source_files if p not in already_planned]
        if not new_sources:
            logger.info("모든 소스가 이미 플래닝됨 — 스킵")
            return existing_plan
        logger.info(f"새 소스 {len(new_sources)}개 발견 — 증분 플래닝 시작")
        all_pages = existing_plan.get("pages", [])
    else:
        already_planned = set()
        new_sources = source_files
        all_pages = []

    existing_paths, existing_descriptions, page_index = _build_page_index(all_pages)

    for src_path in new_sources:
        logger.info(f"소스 파일 처리: {src_path}")
        try:
            # MAX_CHUNK_CHARS는 백엔드 context window 기준으로 api.py가 동적 계산
            max_c = MAX_CHUNK_CHARS
            min_c = int(max_c * 0.8)
            chunks = chunk_fn(src_path, min_size=min_c, max_size=max_c)
        except Exception as e:
            logger.error(f"청킹 실패 ({src_path}): {e}")
            # 실패한 소스도 planned에 기록해 무한 재시도 방지
            already_planned.add(src_path)
            _save_plan_incremental(plan_path, already_planned, all_pages)
            continue

        logger.info(f"  → {len(chunks)}개 청크")
        for i, c in enumerate(chunks):
            logger.info(f"    청크 {i}: {len(c['text'])}자")

        for chunk in chunks:
            # 503 등 서버 오류 시 성공할 때까지 무한 재시도
            while True:
                logger.info(f"  청크 {chunk['index']} 처리 중...")
                pages = _plan_chunk(
                    chunk=chunk,
                    source_file=os.path.relpath(src_path, os.path.dirname(sources_dir)),
                    existing_pages_info=existing_descriptions,
                    call_llm=call_llm,
                    backend=backend,
                )
                if pages is not None:
                    break
                logger.warning(f"  청크 {chunk['index']} 재시도 대기 30초...")
                time.sleep(30)

            for p in pages:
                if p["path"] not in existing_paths:
                    all_pages.append(p)
                    page_index[p["path"]] = len(all_pages) - 1
                    existing_paths.add(p["path"])
                    existing_descriptions[p["path"]] = p["description"]
                else:
                    # 이미 있는 페이지 → sources 머지 (멀티소스 지원)
                    idx = page_index[p["path"]]
                    existing_src_files = {s["file"] for s in all_pages[idx]["sources"]}
                    new_source_added = False
                    for new_src in p["sources"]:
                        if new_src["file"] not in existing_src_files:
                            all_pages[idx]["sources"].append(new_src)
                            new_source_added = True
                            logger.info(f"  멀티소스 머지: {p['path']} ← {new_src['file']}")
                    # 새 소스가 추가됐고 description이 더 넓으면 교체
                    if new_source_added and len(p["description"]) > len(all_pages[idx]["description"]):
                        old_desc = all_pages[idx]["description"]
                        all_pages[idx]["description"] = p["description"]
                        existing_descriptions[p["path"]] = p["description"]
                        logger.info(f"  description 업데이트: {p['path']} | {old_desc!r} → {p['description']!r}")

        # 소스 파일 완료 시 즉시 저장 (중간 crash 복구 지원)
        already_planned.add(src_path)
        _save_plan_incremental(plan_path, already_planned, all_pages)
        logger.info(f"  소스 완료 저장: {Path(src_path).name} (누적 {len(already_planned)}개)")

    plan = _save_plan_incremental(plan_path, already_planned, all_pages)
    logger.info(f"plan.json 저장 완료: {len(all_pages)}개 페이지 (소스 {len(already_planned)}개)")
    return plan


def _plan_chunk(
    chunk: dict,
    source_file: str,
    existing_pages_info: dict[str, str],  # path → description
    call_llm,
    backend: str,
) -> list[dict] | None:
    """
    청크 하나에서 LLM으로 페이지 목록 추출.
    LLM 호출 실패(503 등) 시 None 반환 → 호출부에서 재시도.
    파싱 실패 3회 시 None 반환.
    정상 처리(페이지 없음 포함) 시 list 반환.
    """
    PLANNER_SYSTEM, PLANNER_USER = load_prompt("planner")  # noqa: N806 — 프롬프트 상수 관례

    existing_list = "\n".join(
        f"{path}: {desc}"
        for path, desc in sorted(existing_pages_info.items())
    ) if existing_pages_info else "(없음)"

    chunk_text = truncate_content(chunk["text"], MAX_CHUNK_CHARS, label=f"chunk_{chunk['index']}")

    user_msg = PLANNER_USER.format(
        existing_pages=existing_list,
        source_file=source_file,
        chunk_text=chunk_text,
    )

    for attempt in range(3):
        logger.info(f"  청크 {chunk['index']} LLM 입력 — system: {len(PLANNER_SYSTEM)}자, user: {len(user_msg)}자")
        raw = call_llm(
            PLANNER_SYSTEM,
            user_msg,
            temperature=0.1,
            backend=backend,
            json_format=True,
            max_tokens=16384,
        )

        if raw.startswith("[LLM 호출 실패]"):
            logger.error(f"Planner LLM 실패 (청크 {chunk['index']}): {raw}")
            return None  # 서버 오류 → 호출부에서 재시도

        pages = _parse_planner_response(raw, source_file)
        if pages is not None:
            return pages

        logger.warning(f"Planner 파싱 실패 (시도 {attempt + 1}/3) — raw 응답 (첫 500자): {raw[:500]!r}")
        logger.warning("재시도")

    logger.error(f"Planner 파싱 3회 모두 실패 (청크 {chunk['index']})")
    return None  # 파싱 실패도 재시도 대상


def _parse_planner_response(raw: str, source_file: str) -> list[dict] | None:
    """LLM 응답에서 JSON 배열 파싱. 파싱 불가 시 None 반환."""
    text = re.sub(r'```json\s*', '', raw)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()

    # JSON 파싱 시도: 배열 또는 {"pages":[...]} 형태 모두 허용
    data = None

    # 1) 전체 텍스트가 valid JSON인지 먼저 시도
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            data = parsed
        elif isinstance(parsed, dict):
            # {"pages": [...]} 또는 임의 키 아래 배열이 있는 경우
            for v in parsed.values():
                if isinstance(v, list) and v:
                    data = v
                    break
    except json.JSONDecodeError:
        pass

    # 2) 완전한 배열 추출 시도
    if data is None:
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                pass

    # 3) 불완전한 JSON 복구 (max_tokens로 잘린 경우)
    if data is None:
        m = re.search(r'\[.*', text, re.DOTALL)
        if m:
            partial = m.group()
            last_complete = partial.rfind('},')
            if last_complete != -1:
                try:
                    data = json.loads(partial[:last_complete + 1] + ']')
                    logger.warning(f"불완전한 JSON 복구 성공 (잘린 응답)")
                except json.JSONDecodeError:
                    pass

    if not isinstance(data, list):
        return None

    pages = []
    for item in data:
        if not isinstance(item, dict):
            continue
        path = item.get("path", "").strip()
        description = item.get("description", "").strip()
        sections = item.get("sections", [])

        if not path or not description:
            continue

        # path 유효성 검사
        if not path.startswith(("entities/", "concepts/", "internal/", "features/")):
            continue
        if not path.endswith(".md"):
            continue

        # concepts/ 동작어 검사
        if path.startswith("concepts/"):
            basename = Path(path).stem
            if "_" not in basename:
                logger.warning(f"concepts/ 동작어 없음 스킵: {path}")
                continue

        pages.append({
            "path": path,
            "description": description,
            "generated": False,
            "linked": False,
            "sources": [
                {
                    "file": source_file,
                    "sections": sections if isinstance(sections, list) else [],
                }
            ],
        })

    return pages


def _build_page_index(pages: list) -> tuple[set, dict, dict]:
    """
    pages 리스트에서 빠른 조회를 위한 인덱스 구조 3개를 한 번에 생성.

    Returns:
        (existing_paths, existing_descriptions, page_index)
        - existing_paths: path 집합 (중복 체크용)
        - existing_descriptions: path → description (LLM 컨텍스트 전달용)
        - page_index: path → pages 리스트 내 정수 인덱스 (멀티소스 머지용)
    """
    existing_paths: set[str] = set()
    existing_descriptions: dict[str, str] = {}
    page_index: dict[str, int] = {}
    for i, page in enumerate(pages):
        path = page["path"]
        existing_paths.add(path)
        existing_descriptions[path] = page["description"]
        page_index[path] = i
    return existing_paths, existing_descriptions, page_index


def _save_plan_incremental(plan_path: str, planned_sources: set, pages: list) -> dict:
    """소스 파일 처리 완료 시 plan.json에 즉시 저장. 저장된 plan dict 반환."""
    plan = {
        "post_plan_done": False,
        "planned_sources": sorted(planned_sources),
        "pages": pages,
    }
    save_plan(plan, plan_path)
    return plan


def _collect_sources(sources_dir: str) -> list[str]:
    """sources/ 하위 .docx, .txt 파일 수집. 3gpp_ref/ 는 reference 전용이므로 제외."""
    result = []
    for root, dirs, files in os.walk(sources_dir):
        # 3gpp_ref 디렉토리는 parse_38822 전용 — 일반 플래닝 대상 아님
        dirs[:] = [d for d in dirs if d != "3gpp_ref"]
        for f in sorted(files):
            if f.endswith((".docx", ".txt")):
                result.append(os.path.join(root, f))
    return result


def _empty_plan() -> dict:
    return {"pages": []}
