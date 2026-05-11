"""
link.py — Phase 3: Linker

run_link(plan, wiki_dir, plan_path, call_llm) -> None
    Python이 링크 맵 계산 → 파일별 LLM 호출 → 역방향 링크 추가
"""

import json
import logging
import re
from pathlib import Path

import wiki_builder.api
from wiki_builder.prompt_loader import load_prompt
from wiki_builder.utils import save_plan

logger = logging.getLogger(__name__)


def run_link(
    plan: dict,
    wiki_dir: str,
    plan_path: str,
    call_llm,
    *,
    backend: str | None = None,
) -> None:
    """Phase 3 실행."""
    backend = backend or wiki_builder.api.BACKEND

    LINKER_SYSTEM, LINKER_USER = load_prompt("linker")

    pages = plan.get("pages", [])
    generated = [p for p in pages if p.get("generated", False)]
    logger.info(f"Link 대상: {len(generated)}개")

    # 링크 맵 구성: {path: set of paths that link TO this path}
    link_map = _build_link_map(wiki_dir, generated)

    for page in generated:
        if page.get("linked", False):
            continue

        path = page["path"]
        file_path = Path(wiki_dir) / path

        if not file_path.exists():
            logger.warning(f"파일 없음, 스킵: {path}")
            page["linked"] = True
            continue

        inbound = link_map.get(path, set())
        if not inbound:
            # inbound 링크 없으면 LLM 호출 불필요
            page["linked"] = True
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        inbound_str = "\n".join(f"- {p}" for p in sorted(inbound))
        user_msg = LINKER_USER.format(
            file_content=content,
            inbound_links=inbound_str,
        )

        updated = call_llm(LINKER_SYSTEM, user_msg, temperature=0.1, backend=backend)

        if updated.startswith("[LLM 호출 실패]"):
            logger.error(f"Linker LLM 실패 ({path}): {updated}")
        elif updated.strip():
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(updated)
            logger.info(f"  링크 업데이트: {path}")

        page["linked"] = True

    save_plan(plan, plan_path)


def _build_link_map(wiki_dir: str, pages: list[dict]) -> dict[str, set]:
    """
    각 파일의 [[wikilink]] 를 파싱하여
    {target_path: set(source_paths)} 맵 반환.
    """
    link_map: dict[str, set] = {}
    wikilink_pat = re.compile(r'\[\[([^\]]+)\]\]')

    # path stem → full path 맵
    stem_to_path: dict[str, str] = {}
    for p in pages:
        stem = Path(p["path"]).stem
        stem_to_path[stem] = p["path"]

    for page in pages:
        src_path = page["path"]
        file_path = Path(wiki_dir) / src_path
        if not file_path.exists():
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        for m in wikilink_pat.finditer(content):
            link_text = m.group(1).strip()
            # 관계 타입 제거: "PUSCH (affects)" → "PUSCH"
            link_name = re.sub(r'\s*\([^)]+\)$', '', link_text).strip()

            if link_name in stem_to_path:
                target = stem_to_path[link_name]
                if target not in link_map:
                    link_map[target] = set()
                link_map[target].add(src_path)

    return link_map
