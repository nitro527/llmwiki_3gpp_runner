"""
spec_reader.py — 3GPP 스펙 파일에서 섹션 내용 추출
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_spec_content(plan_page: dict, sources_dir: Path, root: Path, max_content_chars: int, truncate_fn) -> str:
    """
    plan 페이지의 sources 기반으로 스펙 내용 추출.
    소스 파일을 읽고 지정된 섹션 내용만 조합.
    """
    from wiki_builder.chunk_text import read_file_content

    parts = []
    for src in plan_page.get("sources", []):
        src_file = src.get("file", "")
        sections = src.get("sections", [])

        candidates = [
            root / src_file,
            sources_dir / src_file,
            sources_dir / "3gpp" / Path(src_file).name,
        ]
        full_path = None
        for c in candidates:
            if c.exists():
                full_path = c
                break

        if full_path is None:
            logger.warning(f"소스 파일 없음: {src_file}")
            continue

        try:
            content = read_file_content(str(full_path))
        except Exception as e:
            logger.error(f"파일 읽기 실패 ({src_file}): {e}")
            continue

        if sections:
            deduped = _dedup_sections(sections)
            for sec in deduped:
                extracted = _extract_section(content, sec)
                if extracted:
                    parts.append(f"[{src_file} §{sec}]\n{extracted}")
        else:
            parts.append(f"[{src_file}]\n{content[:max_content_chars]}")

    combined = "\n\n".join(parts) if parts else "(스펙 내용 없음)"
    return truncate_fn(combined, max_content_chars, label="spec_content")


def _extract_section(text: str, section_num: str) -> str:
    """
    텍스트에서 섹션 번호에 해당하는 내용 추출.

    패턴: \\n{섹션번호}\\t{제목} 로 시작,
    다음 같은 레벨 이상의 헤더까지.
    """
    escaped = re.escape(section_num)
    pattern = re.compile(rf'\n{escaped}\t')
    m = pattern.search(text)
    if not m:
        logger.debug(f"섹션 없음: §{section_num}")
        return ""

    start = m.start()
    next_pat = re.compile(r'\n\d+(?:\.\d+)*\t')
    m2 = next_pat.search(text, start + 1)
    end = m2.start() if m2 else len(text)

    result = text[start:end].strip()
    logger.debug(f"섹션 추출: §{section_num} ({len(result)}자)")
    return result


def _dedup_sections(sections: list[str]) -> list[str]:
    """
    parent 섹션이 있으면 child 섹션 제거.
    예: ["6", "6.1", "6.1.1"] → ["6"]
    """
    result = []
    for sec in sections:
        if any(sec.startswith(parent + ".") for parent in result):
            logger.debug(f"섹션 중복 제거: §{sec} (parent 이미 포함)")
            continue
        result.append(sec)
    return result
